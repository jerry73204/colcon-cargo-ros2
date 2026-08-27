# Licensed under the Apache License, Version 2.0

"""Workspace-level ROS 2 binding generation for Rust.

This module provides centralized binding generation for an entire colcon workspace.
Instead of each package generating bindings independently (causing race conditions),
this module generates ALL bindings once before any packages are built.

Architecture:
1. Discover all ROS package dependencies in the workspace
2. Generate all bindings to build/<pkg>/rosidl_cargo/
3. Generate per-crate .cargo/config.toml with [patch.crates-io] and [build] rustflags
4. Individual packages run plain `cargo build` (no --config needed)
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from colcon_core.logging import colcon_logger

# Import Rust library directly via PyO3 bindings
from colcon_cargo_ros2 import cargo_ros2_py

logger = colcon_logger.getChild(__name__)

# Written beside each package's generated bindings; records a digest of the
# interface definitions they were generated from, so a later build can tell
# "already generated" from "still current".
STAMP_FILENAME = ".bindgen_stamp"

# Written inside each generated crate: the interface directory it came from,
# followed by one record per definition file. The crate's own build.rs re-derives
# these records and refuses to compile when they no longer match, which is what
# a plain `cargo build` needs -- it never consults the stamp above.
MANIFEST_FILENAME = ".bindgen_manifest"

# Interface definition suffixes whose content affects the generated crate.
INTERFACE_SUFFIXES = (".msg", ".srv", ".action", ".idl")

# Subdirectories whose presence marks a package as an interface package.
INTERFACE_SUBDIRS = ("msg", "srv", "action")

# Content digests, keyed by (path, size, mtime_ns). Binding generation runs once
# per package build task, so the same definition files are digested repeatedly
# within a build; the memo makes everything after the first pass cost a stat.
_FILE_DIGESTS: Dict[Tuple[str, int, int], str] = {}


def _fnv1a64(data: bytes) -> str:
    """FNV-1a, 64-bit, as 16 hex digits.

    Not a cryptographic hash and not meant to be: this detects edits, and the
    generated crates' build.rs has to compute the identical value with no
    dependencies at all, which rules out anything from a crate.
    """
    digest = 0xCBF29CE484222325
    for byte in data:
        digest ^= byte
        digest = (digest * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


def _file_digest(path: Path, size: int, mtime_ns: int) -> str:
    """Digest of *path*'s contents, memoised on its stat signature."""
    key = (str(path), size, mtime_ns)
    cached = _FILE_DIGESTS.get(key)
    if cached is not None:
        return cached

    try:
        digest = _fnv1a64(path.read_bytes())
    except OSError:
        # Unreadable: fall back to the stat signature for this file alone, which
        # is no worse than what freshness used to be for all of them.
        digest = f"mtime{mtime_ns:x}"

    _FILE_DIGESTS[key] = digest
    return digest


# Sentinel for "not computed yet", since None is a meaningful answer.
_UNSET = object()

# Which rosidl_runtime_rs each published rclrs depends on.
#
# Generated crates must ask for the same one: cargo treats 0.5 and 0.6 as
# incompatible, so a mismatch leaves two copies in the graph and the `Message`
# trait a generated crate implements is not the one rclrs requires.
RCLRS_RUNTIME_VERSIONS = {
    "0.6": "0.5",
    "0.7": "0.6",
}

# Dependency mismatches already reported, so the user reads each one once.
# Binding generation reruns for every package build task in a build, which
# would otherwise repeat every warning once per Cargo package in the workspace.
_REPORTED_MISMATCHES: Set[str] = set()


def _package_share_directory(pkg_name: str) -> Optional[Path]:
    """Locate an installed package's share directory, or None if not found."""
    from ament_index_python.packages import get_package_share_directory

    try:
        return Path(get_package_share_directory(pkg_name))
    except Exception:
        return None


def _has_interface_definitions(pkg_dir: Path) -> bool:
    """True when *pkg_dir* holds msg/, srv/ or action/ definitions."""
    return any((pkg_dir / subdir).exists() for subdir in INTERFACE_SUBDIRS)


def _git_succeeds(args: List[str], cwd: Path) -> bool:
    """Run a git command in *cwd*, reporting whether it exited successfully."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _warn_once(key: str, message: str):
    """Log *message* as a warning the first time *key* is seen in this process."""
    if key in _REPORTED_MISMATCHES:
        return
    _REPORTED_MISMATCHES.add(key)
    logger.warning(message)


def _note_once(key: str, message: str):
    """Log *message* at info level the first time *key* is seen in this process.

    For things worth being able to look up but not worth interrupting over --
    where the build is correct and the user may have had a good reason.
    """
    if key in _REPORTED_MISMATCHES:
        return
    _REPORTED_MISMATCHES.add(key)
    logger.info(message)


def _read_toml(path: Path) -> Dict:
    """Parse a TOML file, returning an empty mapping when it cannot be read."""
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _dependency_tables(manifest: Dict):
    """Yield every dependency table a Cargo manifest can declare.

    Includes the platform-specific ``[target.<cfg>.*]`` tables, which carry real
    dependencies that a scan of the top-level tables alone would miss.
    """
    kinds = ("dependencies", "build-dependencies", "dev-dependencies")
    for kind in kinds:
        table = manifest.get(kind)
        if isinstance(table, dict):
            yield table

    targets = manifest.get("target")
    if isinstance(targets, dict):
        for target_table in targets.values():
            if not isinstance(target_table, dict):
                continue
            for kind in kinds:
                table = target_table.get(kind)
                if isinstance(table, dict):
                    yield table


def _cargo_dependency_requirements(cargo_toml_path: Path) -> Dict[str, str]:
    """Map dependency name → declared version requirement, where one is given.

    Covers the plain-string form (``rclrs = "0.7"``) and the table form
    (``rclrs = { version = "0.7", features = [...] }``); a path or git dependency
    contributes nothing, having no version to report.
    """
    manifest = _read_toml(cargo_toml_path)
    requirements: Dict[str, str] = {}
    for table in _dependency_tables(manifest):
        for name, spec in table.items():
            if isinstance(spec, str):
                requirements[name] = spec
            elif isinstance(spec, dict) and isinstance(spec.get("version"), str):
                requirements[name] = spec["version"]
    return requirements


def _is_unbounded(requirement: str) -> bool:
    """True for a requirement that names no version at all, like ``*``."""
    return requirement.strip() in {"*", ""}


def _major_minor(requirement: Optional[str]) -> Optional[str]:
    """Reduce a version requirement to ``major.minor``.

    ``"0.7"``, ``"0.7.1"`` and ``"^0.7"`` all identify the same 0.x compatibility
    bucket, which is the granularity that decides whether cargo unifies.
    """
    if not requirement:
        return None
    cleaned = requirement.strip().lstrip("^~=><* ")
    parts = cleaned.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return f"{parts[0]}.{parts[1]}"


def _version_key(version: str):
    """Sort key for ``major.minor`` strings."""
    return tuple(int(part) for part in version.split("."))


def _cargo_toml_has_workspace(cargo_toml_path: Path) -> bool:
    """Check whether a Cargo.toml contains a ``[workspace]`` section.

    Uses simple TOML parsing (tomllib/tomli) to avoid false positives from
    string matching in comments or values.
    """
    if not cargo_toml_path.exists():
        return False
    return "workspace" in _read_toml(cargo_toml_path)


class WorkspaceBindingGenerator:
    """Generates ROS 2 Rust bindings for an entire colcon workspace."""

    def __init__(self, workspace_root: Path, build_base: Path, install_base: Path, args):
        """Initialize the workspace binding generator.

        Args:
            workspace_root: Root directory of the colcon workspace
            build_base: Base directory for build artifacts (workspace/build/)
            install_base: Base directory for installed packages (workspace/install/)
            args: Colcon command line arguments
        """
        self.workspace_root = workspace_root
        self.build_base = build_base
        self.install_base = install_base
        self.args = args
        self.lock_file = build_base / ".colcon" / "bindgen.lock"
        self._lock_fd = None

        # package name -> its direct package.xml dependencies, accumulated as
        # dependencies are walked. Lets a single traversal answer "what does
        # THIS package need" as well as "what does the workspace need".
        self._dep_graph: Dict[str, Set[str]] = {}

        # colcon Cargo package name -> interface packages it actually needs
        # (transitively). Empty until _discover_ros_packages() has run; callers
        # must treat "absent" as "unknown", never as "needs nothing".
        self._package_interface_deps: Dict[str, Set[str]] = {}

        # colcon Cargo package name -> every package it depends on transitively,
        # interface or not. Linker search paths come from this rather than from
        # the interface subset: a crate may link a C library from any ROS package
        # it declares. Same "absent means unknown" rule.
        self._package_all_deps: Dict[str, Set[str]] = {}

        # Resolved once per build; _UNSET distinguishes "not yet worked out"
        # from "the workspace expressed no opinion".
        self._runtime_version = _UNSET

        # Interface packages some package in the workspace asks for by version
        # rather than `*`. None until worked out; see :meth:`_detect_pinned_packages`.
        self._pinned_packages: Optional[Set[str]] = None

    def try_acquire_lock(self) -> bool:
        """Try to acquire the binding generation lock.

        Uses fcntl advisory locking so the lock is automatically released
        when the process exits (even on crash or SIGKILL). This prevents
        stale lock files from blocking subsequent builds.

        Returns:
            True if the lock was acquired (this process should generate bindings).
            False if another process holds the lock.
        """
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = open(self.lock_file, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd.write(str(os.getpid()))
            self._lock_fd.flush()
            return True
        except OSError:
            logger.info("Binding generation lock held by another process")
            self._lock_fd.close()
            self._lock_fd = None
            return False

    def release_lock(self):
        """Release the binding generation lock."""
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
            self._lock_fd = None
            try:
                self.lock_file.unlink()
            except OSError:
                pass

    def generate_all_bindings(self, verbose: bool = False):
        """Generate all ROS 2 bindings for the workspace.

        This is the main entry point that:
        1. Discovers all ROS dependencies
        2. Generates bindings for all packages
        3. Writes per-crate .cargo/config.toml with patches and build flags
        """
        logger.info("Starting workspace-level binding generation")

        # Step 1: Discover all ROS dependencies from ament_index and workspace
        ros_packages = self._discover_ros_packages()
        logger.info(f"Discovered {len(ros_packages)} ROS packages")

        # Step 1.5: Validate Cargo.toml dependencies match package.xml
        self._validate_cargo_dependencies(ros_packages)

        # Step 2: Generate bindings for all discovered packages
        self._generate_bindings(ros_packages, verbose)

        # Step 3: Write .cargo/config.toml with patches + build flags
        self._write_cargo_configs(ros_packages)

        logger.info("Workspace-level binding generation complete")

    def _discover_ros_packages(self) -> Dict[str, Path]:
        """Discover ROS interface packages that are dependencies of workspace Cargo packages.

        This implements dependency-aware binding generation:
        1. Get Cargo packages from augmentation (with parsed dependencies from package.xml)
        2. Extract direct ROS dependencies from Colcon
        3. Resolve transitive dependencies using catkin_pkg
        4. Filter to only interface packages (have msg/srv/action)

        Returns:
            Dict mapping package names to their share/ directory paths
        """
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        # Get Cargo package descriptors (includes parsed dependencies from package.xml)
        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})

        if not cargo_descriptors:
            logger.info("No Cargo packages found in workspace")
            return {}

        logger.info(f"Discovering dependencies for {len(cargo_descriptors)} Cargo packages")

        # Step 1: Get direct ROS dependencies from Colcon-parsed package.xml
        required_packages = set()

        # Keep the per-package attribution, not just the union. _write_cargo_configs()
        # needs to know which Cargo target needs which interface package, so that a
        # crate depending on std_msgs alone does not get patches for the 100+ message
        # packages some other package in the same colcon workspace happens to need.
        direct_deps_by_package: Dict[str, Set[str]] = {}

        for pkg_name, desc in cargo_descriptors.items():
            # Get build + run dependencies (interface packages needed at compile time)
            # desc.dependencies is populated by Colcon's RosPackageIdentification
            # from package.xml using catkin_pkg
            try:
                deps = desc.get_dependencies(categories=["build", "run"])
                dep_names = [d.name for d in deps]
            except Exception as e:
                # Leave this package out of the attribution map entirely: absent
                # means "unknown", which makes _write_cargo_configs() fall back to
                # patching everything rather than silently patching too little.
                logger.warning(f"Could not read dependencies for {pkg_name}: {e}")
                continue

            required_packages.update(dep_names)
            direct_deps_by_package[pkg_name] = set(dep_names)

            if dep_names:
                logger.info(f"{pkg_name} has {len(dep_names)} direct dependencies: {dep_names}")

        logger.info(f"Total direct dependencies: {len(required_packages)}")

        # Step 2: Resolve transitive dependencies using catkin_pkg
        # This handles: my_pkg -> geometry_msgs -> std_msgs -> builtin_interfaces
        required_packages = self._resolve_transitive_dependencies(required_packages)

        logger.info(f"Total after transitive resolution: {len(required_packages)}")

        # Step 3: Check workspace packages for interfaces (from source directories)
        # This also discovers their dependencies
        workspace_interface_packages, workspace_deps = self._find_workspace_interface_packages(
            required_packages
        )

        # Add dependencies of workspace packages to required set
        required_packages.update(workspace_deps)

        # Re-resolve transitive dependencies including workspace package dependencies
        if workspace_deps:
            logger.info(f"Adding {len(workspace_deps)} dependencies from workspace packages")
            required_packages = self._resolve_transitive_dependencies(required_packages)
            logger.info(
                f"Total after resolving workspace package dependencies: {len(required_packages)}"
            )

        # Step 4: Filter remaining packages to interface packages (from ament_index)
        remaining_packages = required_packages - set(workspace_interface_packages.keys())
        interface_packages = self._filter_interface_packages(remaining_packages)

        # Merge workspace and system interface packages
        interface_packages.update(workspace_interface_packages)

        logger.info(f"Final interface packages to generate: {len(interface_packages)}")

        # Step 5: Narrow the per-package attribution to interface packages, using
        # the dependency edges recorded during the walks above.
        interface_names = set(interface_packages)
        closures = {
            pkg_name: self._transitive_closure(direct)
            for pkg_name, direct in direct_deps_by_package.items()
        }
        attribution = {
            pkg_name: closure & interface_names for pkg_name, closure in closures.items()
        }

        # The unfiltered closures drive linker search paths, which are not limited
        # to interface packages. Unlike the patch attribution below, an incomplete
        # entry here costs an extra -L, never a missing one, so it needs no
        # all-or-nothing fallback.
        self._package_all_deps = closures

        # Every package being generated should be claimed by at least one Cargo
        # package, because that is how it entered the required set in the first
        # place. If something is unclaimed, an edge went unrecorded -- a package
        # whose package.xml could not be parsed, for instance -- and the per-package
        # view is then narrower than the truth. Narrowing on an incomplete map could
        # omit a needed patch, so drop back to patching everything.
        claimed: Set[str] = set()
        for deps in attribution.values():
            claimed |= deps

        unclaimed = interface_names - claimed
        if unclaimed:
            logger.warning(
                "Not attributing bindings per package: "
                + ", ".join(sorted(unclaimed))
                + " could not be traced back to a Cargo package. "
                "Every Cargo target will be patched with every generated binding."
            )
            self._package_interface_deps = {}
        else:
            self._package_interface_deps = attribution

        return interface_packages

    def _transitive_closure(self, seeds: Set[str]) -> Set[str]:
        """Expand *seeds* over the recorded package.xml dependency edges.

        Uses ``self._dep_graph``, populated while dependencies were being walked,
        so this costs no additional package.xml parsing. Packages with no recorded
        edges simply contribute themselves.
        """
        reached = set(seeds)
        queue = list(seeds)
        while queue:
            pkg_name = queue.pop()
            for dep in self._dep_graph.get(pkg_name, ()):
                if dep not in reached:
                    reached.add(dep)
                    queue.append(dep)
        return reached

    def _looks_like_interface_package(self, pkg_name: str) -> bool:
        """True when *pkg_name* names a ROS interface package.

        Checks the colcon workspace source tree first, because a workspace-local
        messages package is not in the ament index until it has been installed,
        then falls back to installed packages.
        """
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        all_descriptors = getattr(RustBindingAugmentation, "_all_descriptors", set())
        for desc in all_descriptors:
            if desc.name == pkg_name:
                return _has_interface_definitions(Path(desc.path))

        pkg_share = _package_share_directory(pkg_name)
        if pkg_share is None:
            return False
        return _has_interface_definitions(pkg_share)

    def _detect_runtime_version(self) -> Optional[str]:
        """The rosidl_runtime_rs version generated crates should depend on.

        Derived from the workspace's own packages, because it has to match what
        their `rclrs` already pulls in. Getting it wrong is not a warning: cargo
        keeps both versions and the build fails with a trait mismatch that names
        the runtime crate rather than anything the user wrote.

        A package that declares `rosidl_runtime_rs` says so directly; one that
        only declares `rclrs` implies it through :data:`RCLRS_RUNTIME_VERSIONS`.

        Returns None when the workspace expresses no opinion, leaving the
        generator's own default in place.
        """
        if self._runtime_version is not _UNSET:
            return self._runtime_version

        self._runtime_version = self._compute_runtime_version()
        return self._runtime_version

    def _compute_runtime_version(self) -> Optional[str]:
        """Work out the version; see :meth:`_detect_runtime_version`."""
        override = getattr(self.args, "rosidl_runtime_rs_version", None)
        if override:
            return override

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})

        # runtime version -> packages asking for it, for a legible conflict report
        wanted: Dict[str, List[str]] = {}
        for pkg_name, desc in cargo_descriptors.items():
            requirements = _cargo_dependency_requirements(Path(desc.path) / "Cargo.toml")

            declared = requirements.get("rosidl_runtime_rs")
            rclrs = requirements.get("rclrs")

            if declared is not None:
                version = _major_minor(declared)
            elif rclrs is not None:
                version = RCLRS_RUNTIME_VERSIONS.get(_major_minor(rclrs))
                if version is None and _is_unbounded(rclrs):
                    # `rclrs = "*"` resolves to whatever is newest, which may not
                    # be the version the rest of the workspace is built for. The
                    # symptom is a trait mismatch inside this package alone.
                    _warn_once(
                        f"unbounded-rclrs:{pkg_name}",
                        f'{pkg_name} declares rclrs = "{rclrs}", so cargo resolves it to '
                        "whatever version is newest.\n"
                        "  Generated bindings cannot be matched to an unbounded requirement; "
                        'pin a version (e.g. rclrs = "0.7") to have them agree.',
                    )
            else:
                version = None

            if version:
                wanted.setdefault(version, []).append(pkg_name)

        if not wanted:
            return None
        if len(wanted) == 1:
            return next(iter(wanted))

        # More than one, and one shared set of generated crates cannot satisfy
        # both. Say which packages disagree; cargo's own error names neither.
        chosen = max(wanted, key=_version_key)
        detail = "; ".join(
            f"{version} ({', '.join(sorted(pkgs))})" for version, pkgs in sorted(wanted.items())
        )
        _warn_once(
            "runtime-version-conflict:" + detail,
            "Packages in this workspace need different rosidl_runtime_rs versions: "
            f"{detail}.\n"
            "  Bindings are generated once and shared, so only one can be satisfied; "
            f"using {chosen}.\n"
            "  Align the packages' rclrs versions, or pick one explicitly with "
            "--rosidl-runtime-rs-version.",
        )
        return chosen

    @staticmethod
    def _cargo_dependency_names(
        cargo_toml_path: Path, cargo_workspace_root: Optional[Path] = None
    ) -> Set[str]:
        """Every package name *cargo_toml_path* depends on, in any form.

        Reading the table keys alone misses three forms, each of which hides a
        missing ``<depend>`` tag from validation:

        - ``msgs = { package = "sensor_msgs" }`` -- the key is the rename, the
          package name is in ``package``
        - ``[target.'cfg(unix)'.dependencies]`` -- a whole extra set of tables
        - ``sensor_msgs = { workspace = true }`` -- the requirement lives in the
          Cargo workspace root's ``[workspace.dependencies]``

        Args:
            cargo_toml_path: Manifest to read.
            cargo_workspace_root: Directory holding the Cargo workspace manifest,
                needed only to resolve ``workspace = true`` entries.
        """
        data = _read_toml(cargo_toml_path)
        if not data:
            return set()

        inherited: Dict[str, object] = {}
        if cargo_workspace_root is not None:
            root_manifest = cargo_workspace_root / "Cargo.toml"
            if root_manifest != cargo_toml_path:
                root_data = _read_toml(root_manifest)
            else:
                root_data = data
            workspace_table = root_data.get("workspace", {})
            if isinstance(workspace_table, dict):
                inherited = workspace_table.get("dependencies", {}) or {}

        def resolve(key: str, spec) -> str:
            if isinstance(spec, dict):
                if spec.get("workspace") is True:
                    parent = inherited.get(key)
                    if isinstance(parent, dict):
                        return parent.get("package", key)
                return spec.get("package", key)
            return key

        names: Set[str] = set()
        for table in _dependency_tables(data):
            for key, spec in table.items():
                names.add(resolve(key, spec))
        return names

    @staticmethod
    def _locally_sourced_dependencies(
        cargo_toml_path: Path, cargo_workspace_root: Optional[Path] = None
    ) -> Dict[str, str]:
        """Package name -> the ``path``/``git`` source it is taken from.

        ``[patch.crates-io]`` only redirects registry dependencies. A dependency
        with its own source is resolved from there and the generated crate is
        never consulted, so an interface package pinned this way silently
        bypasses every binding this extension produces.

        Args:
            cargo_toml_path: Manifest to read.
            cargo_workspace_root: Directory holding the Cargo workspace manifest,
                needed only to resolve ``workspace = true`` entries.
        """
        data = _read_toml(cargo_toml_path)
        if not data:
            return {}

        inherited: Dict[str, object] = {}
        if cargo_workspace_root is not None:
            root_manifest = cargo_workspace_root / "Cargo.toml"
            root_data = data if root_manifest == cargo_toml_path else _read_toml(root_manifest)
            workspace_table = root_data.get("workspace", {})
            if isinstance(workspace_table, dict):
                inherited = workspace_table.get("dependencies", {}) or {}

        sources: Dict[str, str] = {}
        for table in _dependency_tables(data):
            for key, spec in table.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("workspace") is True:
                    parent = inherited.get(key)
                    spec = parent if isinstance(parent, dict) else {}
                name = spec.get("package", key)
                for kind in ("path", "git"):
                    value = spec.get(kind)
                    if isinstance(value, str):
                        sources[name] = value
                        break
        return sources

    @staticmethod
    def _cargo_version_requirements(
        cargo_toml_path: Path, cargo_workspace_root: Optional[Path] = None
    ) -> Dict[str, str]:
        """Package name -> the version requirement declared for it, where bounded.

        Follows the same forms as :meth:`_cargo_dependency_names` -- renames,
        ``[target.<cfg>]`` tables, ``workspace = true`` -- so the key is the
        package name rather than whatever the manifest calls it.

        Dependencies with their own ``path`` or ``git`` source are left out:
        ``[patch.crates-io]`` never redirects those, so the generated crate's
        version cannot matter to them. ``*`` is left out too, being the case the
        fixed version exists for.

        Args:
            cargo_toml_path: Manifest to read.
            cargo_workspace_root: Directory holding the Cargo workspace manifest,
                needed only to resolve ``workspace = true`` entries.
        """
        data = _read_toml(cargo_toml_path)
        if not data:
            return {}

        inherited: Dict[str, object] = {}
        if cargo_workspace_root is not None:
            root_manifest = cargo_workspace_root / "Cargo.toml"
            root_data = data if root_manifest == cargo_toml_path else _read_toml(root_manifest)
            workspace_table = root_data.get("workspace", {})
            if isinstance(workspace_table, dict):
                inherited = workspace_table.get("dependencies", {}) or {}

        requirements: Dict[str, str] = {}
        for table in _dependency_tables(data):
            for key, spec in table.items():
                if isinstance(spec, dict) and spec.get("workspace") is True:
                    parent = inherited.get(key)
                    spec = parent if isinstance(parent, (str, dict)) else {}

                if isinstance(spec, str):
                    name, requirement = key, spec
                elif isinstance(spec, dict):
                    if spec.get("path") is not None or spec.get("git") is not None:
                        continue
                    requirement = spec.get("version")
                    if not isinstance(requirement, str):
                        continue
                    name = spec.get("package", key)
                else:
                    continue

                if not _is_unbounded(requirement):
                    requirements[name] = requirement
        return requirements

    def _detect_pinned_packages(self) -> Set[str]:
        """Packages a workspace package requires by version rather than ``*``.

        Only consulted for interface packages, which are the only ones this
        extension generates a crate for; the rest of the set is harmless.

        Generated crates carry a fixed ``0.0.0`` so that a committed
        ``Cargo.lock`` stops recording which ROS installation produced them. That
        only works while every requirement is ``*``: a ``[patch.crates-io]`` entry
        redirects where a crate comes from, but cargo still checks it against the
        requirement, and ``0.0.0`` answers none of them::

            error: failed to select a version for the requirement
                   `rclrs_example_msgs = "^0.5"`
            candidate versions found which didn't match: 0.0.0

        This project documents ``*``; third-party code written against ``rclrs``
        pins. Both work if the crates that are pinned -- and only those -- carry
        the ROS package version instead.

        Resolved once per build.
        """
        if self._pinned_packages is not None:
            return self._pinned_packages

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})

        pinned = set()
        for desc in cargo_descriptors.values():
            pkg_path = Path(desc.path)
            cargo_toml_path = pkg_path / "Cargo.toml"
            if not cargo_toml_path.exists():
                continue
            try:
                requirements = self._cargo_version_requirements(
                    cargo_toml_path,
                    self._detect_cargo_workspace_root(pkg_path, self.workspace_root),
                )
            except Exception as e:  # a manifest we cannot read pins nothing
                logger.debug(f"Could not read version requirements from {cargo_toml_path}: {e}")
                continue
            pinned.update(requirements)

        self._pinned_packages = pinned
        return pinned

    @staticmethod
    def _resolves_to(base: Path, source: str, target: Path) -> bool:
        """True when *source*, read from a manifest in *base*, is *target*.

        Pointing a path dependency straight at the generated crate is unusual but
        not wrong, and warning about it would be telling the user to replace a
        working setup with an equivalent one.
        """
        try:
            candidate = Path(source)
            if not candidate.is_absolute():
                candidate = base / candidate
            return candidate.resolve() == target.resolve()
        except OSError:
            return False

    def _validate_cargo_dependencies(self, interface_packages: Dict[str, Path]):
        """Validate that Cargo.toml dependencies match package.xml interface packages.

        Warns about mismatches in both directions. The one that matters is an
        interface package used in Cargo.toml but never declared in package.xml:
        package.xml is the only source of binding generation, so that package gets
        no bindings and no ``[patch.crates-io]`` entry, cargo resolves the name
        against the real crates.io, and the build dies with an error that names
        crates.io rather than the missing ``<depend>`` tag -- e.g.
        ``failed to select a version for the requirement `sensor_msgs = "*"` ...
        version 4.2.3 is yanked``.

        Args:
            interface_packages: Dict of discovered interface packages from package.xml
        """
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})
        logger.debug(f"Validating Cargo.toml dependencies for {len(cargo_descriptors)} packages")

        for pkg_name, desc in cargo_descriptors.items():
            pkg_path = Path(desc.path)
            cargo_toml_path = pkg_path / "Cargo.toml"

            if not cargo_toml_path.exists():
                continue

            try:
                cargo_deps = self._cargo_dependency_names(
                    cargo_toml_path,
                    self._detect_cargo_workspace_root(pkg_path, self.workspace_root),
                )

                # Get dependencies from package.xml
                xml_deps = desc.get_dependencies(categories=["build", "run"])
                xml_dep_names = set(d.name for d in xml_deps)
                xml_interface_deps = xml_dep_names & set(interface_packages)

                # Interface packages declared in package.xml that this crate never
                # compiles against.
                #
                # Not a warning: declaring one is often right. A launch file in
                # this package may start a node that publishes the type, or the
                # dependency may be there for the ament environment. Neither
                # appears in Cargo.toml, and warning would ask the user to delete
                # a correct declaration. What it does cost is binding generation,
                # so say that, at a level someone can go looking for.
                missing_in_cargo = xml_interface_deps - cargo_deps
                if missing_in_cargo:
                    names = ", ".join(sorted(missing_in_cargo))
                    _note_once(
                        f"{pkg_name}:missing-in-cargo:{names}",
                        f"{pkg_name}: bindings generated for {names}, which package.xml "
                        "declares but Cargo.toml does not use. Correct if the dependency "
                        "is only needed at runtime; otherwise dropping the <depend> tag "
                        "saves generating them.",
                    )

                # Check for interface packages in Cargo.toml but not in package.xml.
                # Resolving each unknown dependency name matters here: an undeclared
                # package is by definition absent from interface_packages, which is
                # exactly why the old check could never see the case that breaks the
                # build.
                undeclared = {
                    dep
                    for dep in cargo_deps - xml_dep_names
                    if dep in interface_packages or self._looks_like_interface_package(dep)
                }
                if undeclared:
                    names = ", ".join(sorted(undeclared))
                    tags = "\n".join(f"    <depend>{dep}</depend>" for dep in sorted(undeclared))
                    _warn_once(
                        f"{pkg_name}:undeclared:{names}",
                        f"{pkg_name}: ROS interface packages are used in Cargo.toml but "
                        f"not declared in package.xml: {names}.\n"
                        "  No bindings are generated for them, so cargo will look them up "
                        "on crates.io and fail with an unrelated version or 'yanked' error.\n"
                        f"  Add to {pkg_name}/package.xml:\n{tags}",
                    )

                # An interface package cargo will not take from the bindings at
                # all, because the manifest gives it its own source. The failure
                # this produces names a directory the user never typed --
                # upstream safe_drive_tutorial hardcodes /tmp paths from whatever
                # machine generated its messages -- and nothing in cargo's error
                # points back here.
                sourced = self._locally_sourced_dependencies(
                    cargo_toml_path,
                    self._detect_cargo_workspace_root(pkg_path, self.workspace_root),
                )
                for dep, source in sorted(sourced.items()):
                    if not (dep in interface_packages or self._looks_like_interface_package(dep)):
                        continue
                    generated = self.build_base / dep / "rosidl_cargo" / dep
                    if self._resolves_to(cargo_toml_path.parent, source, generated):
                        continue
                    _warn_once(
                        f"{pkg_name}:locally-sourced:{dep}",
                        f"{pkg_name}: {dep} is taken from {source}, not from the "
                        "generated bindings.\n"
                        "  [patch.crates-io] cannot redirect a path or git dependency, so "
                        f"the crate generated at {generated} is unused and cargo reports "
                        "whatever is (or is not) at that source.\n"
                        f"  In {pkg_name}/Cargo.toml, drop the source key:\n"
                        f'    {dep} = "*"',
                    )

            except Exception as e:
                logger.debug(f"Could not validate Cargo.toml for {pkg_name}: {e}")

    def _find_workspace_interface_packages(self, required_packages: set):
        """Find interface packages in the workspace from source directories.

        Reading the *source* tree rather than install/ is deliberate. colcon
        builds in topological order, so an interface package is installed before
        anything that depends on it -- but binding generation runs from the first
        Rust package's build task, which may be before that package's own build
        has finished. The sources are there either way.

        This handles workspace-local packages that haven't been installed yet.
        Also discovers their dependencies to ensure complete binding generation.

        Args:
            required_packages: Set of package names to check

        Returns:
            Tuple of (workspace_interface_packages, workspace_dependencies):
            - workspace_interface_packages: Dict mapping package names to paths
            - workspace_dependencies: Set of dependency names from workspace packages
        """
        from catkin_pkg.package import parse_package

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        workspace_interface_packages = {}
        workspace_dependencies = set()

        # Get all package descriptors discovered by colcon
        all_descriptors = getattr(RustBindingAugmentation, "_all_descriptors", set())

        # Create a mapping of package name -> descriptor
        descriptors_by_name = {desc.name: desc for desc in all_descriptors}

        for pkg_name in required_packages:
            if pkg_name in descriptors_by_name:
                desc = descriptors_by_name[pkg_name]
                pkg_path = Path(desc.path)

                # Check if package has interface definitions in source directory
                has_interfaces = any(
                    [
                        (pkg_path / "msg").exists(),
                        (pkg_path / "srv").exists(),
                        (pkg_path / "action").exists(),
                    ]
                )

                if has_interfaces:
                    # For workspace packages, we use the source directory as the "share" path
                    # because the package hasn't been installed yet
                    workspace_interface_packages[pkg_name] = pkg_path
                    logger.info(f"Found workspace interface package: {pkg_name} at {pkg_path}")

                    # Parse package.xml to discover dependencies of workspace package
                    try:
                        pkg = parse_package(str(pkg_path))
                        condition_context = {**os.environ}
                        pkg.evaluate_conditions(condition_context)

                        # Get all build + run dependencies
                        deps = set()
                        for d in pkg.build_depends:
                            if d.evaluated_condition:
                                deps.add(d.name)
                        for d in pkg.build_export_depends:
                            if d.evaluated_condition:
                                deps.add(d.name)
                        for d in pkg.exec_depends:
                            if d.evaluated_condition:
                                deps.add(d.name)

                        # Workspace-local packages are not in ament_index yet, so
                        # _resolve_transitive_dependencies() cannot see their edges.
                        # Record them here instead.
                        self._dep_graph[pkg_name] = deps

                        if deps:
                            logger.debug(f"{pkg_name} (workspace) added deps: {deps}")
                            workspace_dependencies.update(deps)

                    except Exception as e:
                        logger.debug(
                            f"Could not parse package.xml for workspace package {pkg_name}: {e}"
                        )

        return workspace_interface_packages, workspace_dependencies

    def _resolve_transitive_dependencies(self, initial_packages: set) -> set:
        """Resolve transitive ROS dependencies using catkin_pkg.

        This is the official ROS 2 method for parsing package.xml files.
        Despite the name, catkin_pkg is used by ROS 2 (see colcon-ros documentation).

        Args:
            initial_packages: Set of direct dependency package names

        Returns:
            Set of all packages (direct + transitive)
        """
        from ament_index_python.packages import get_package_share_directory
        from catkin_pkg.package import parse_package

        # Add workspace install directory to AMENT_PREFIX_PATH so we can find
        # workspace-local packages
        original_ament_prefix = os.environ.get("AMENT_PREFIX_PATH", "")
        if self.install_base.exists():
            if original_ament_prefix:
                os.environ["AMENT_PREFIX_PATH"] = f"{self.install_base}:{original_ament_prefix}"
            else:
                os.environ["AMENT_PREFIX_PATH"] = str(self.install_base)

        all_packages = set(initial_packages)
        visited = set()
        queue = set(initial_packages)

        while queue:
            pkg_name = queue.pop()
            if pkg_name in visited:
                continue
            visited.add(pkg_name)

            try:
                # Get package share directory from ament_index
                pkg_share = Path(get_package_share_directory(pkg_name))

                # Parse package.xml using catkin_pkg (official ROS 2 method)
                pkg = parse_package(str(pkg_share))

                # Evaluate conditional dependencies (ROS_VERSION, etc.)
                # This is required - evaluated_condition is None before this call
                condition_context = {**os.environ}
                pkg.evaluate_conditions(condition_context)

                # Get all build + run dependencies (matching Colcon's logic)
                # This follows RosPackageIdentification in colcon-ros
                deps = set()

                # Add build dependencies
                for d in pkg.build_depends:
                    if d.evaluated_condition:  # Respect conditional dependencies
                        deps.add(d.name)

                # Add build export dependencies (transitive build deps)
                for d in pkg.build_export_depends:
                    if d.evaluated_condition:
                        deps.add(d.name)

                # Add exec dependencies (runtime deps)
                for d in pkg.exec_depends:
                    if d.evaluated_condition:
                        deps.add(d.name)

                # Record the edges so per-package closures can be computed later
                # without re-walking package.xml files.
                self._dep_graph[pkg_name] = deps

                # Add new dependencies to the queue
                new_deps = deps - visited
                if new_deps:
                    logger.debug(f"{pkg_name} added transitive deps: {new_deps}")
                    queue.update(new_deps)
                    all_packages.update(new_deps)

            except Exception as e:
                logger.debug(f"Could not resolve dependencies for {pkg_name}: {e}")

        # Restore original AMENT_PREFIX_PATH
        if original_ament_prefix:
            os.environ["AMENT_PREFIX_PATH"] = original_ament_prefix
        elif "AMENT_PREFIX_PATH" in os.environ:
            del os.environ["AMENT_PREFIX_PATH"]

        return all_packages

    def _filter_interface_packages(self, packages: set) -> Dict[str, Path]:
        """Filter packages to only those with msg/srv/action interfaces.

        Args:
            packages: Set of package names

        Returns:
            Dict mapping interface package names to their share/ directory paths
        """
        from ament_index_python.packages import get_package_share_directory

        # Add workspace install directory to AMENT_PREFIX_PATH so we can find
        # workspace-local packages
        original_ament_prefix = os.environ.get("AMENT_PREFIX_PATH", "")
        if self.install_base.exists():
            if original_ament_prefix:
                os.environ["AMENT_PREFIX_PATH"] = f"{self.install_base}:{original_ament_prefix}"
            else:
                os.environ["AMENT_PREFIX_PATH"] = str(self.install_base)

        interface_packages = {}

        for pkg_name in packages:
            try:
                pkg_share = Path(get_package_share_directory(pkg_name))

                # Check if package has interface definitions
                has_interfaces = any(
                    [
                        (pkg_share / "msg").exists(),
                        (pkg_share / "srv").exists(),
                        (pkg_share / "action").exists(),
                    ]
                )

                if has_interfaces:
                    interface_packages[pkg_name] = pkg_share
                    logger.debug(f"Interface package: {pkg_name}")
                else:
                    logger.debug(f"Skipping non-interface package: {pkg_name}")

            except Exception as e:
                logger.debug(f"Could not check {pkg_name}: {e}")

        # Restore original AMENT_PREFIX_PATH
        if original_ament_prefix:
            os.environ["AMENT_PREFIX_PATH"] = original_ament_prefix
        elif "AMENT_PREFIX_PATH" in os.environ:
            del os.environ["AMENT_PREFIX_PATH"]

        return interface_packages

    def _generate_bindings(self, ros_packages: Dict[str, Path], verbose: bool):
        """Generate Rust bindings for all ROS packages.

        Each package's bindings are generated to build/<pkg_name>/rosidl_cargo/

        Args:
            ros_packages: Dict mapping package names to share/ directories
            verbose: Enable verbose output
        """
        # Generate bindings for each package that has interfaces
        for pkg_name, pkg_share in ros_packages.items():
            # Check if package has interfaces (msg/, srv/, action/ directories)
            has_interfaces = any(
                [
                    (pkg_share / "msg").exists(),
                    (pkg_share / "srv").exists(),
                    (pkg_share / "action").exists(),
                ]
            )

            if not has_interfaces:
                continue

            # Output directory: build/<pkg_name>/rosidl_cargo/
            pkg_build_dir = self.build_base / pkg_name / "rosidl_cargo"

            # Reuse existing bindings only when the interface definitions they
            # were generated from have not changed. Existence alone is not
            # enough: a package whose .msg gains a field leaves a stale crate
            # behind, and the build then fails inside CONSUMER code ("no field
            # `pid` on type `ComponentEvent`") with nothing pointing at the
            # stale artifact.
            # Generated structure is: build/<pkg_name>/rosidl_cargo/<pkg_name>/Cargo.toml
            binding_dir = pkg_build_dir / pkg_name
            stamp_file = pkg_build_dir / STAMP_FILENAME
            # A package that someone requires by version is stamped with the ROS
            # version rather than the fixed one, so the requirement has something
            # to match; see :meth:`_detect_pinned_packages`.
            use_ros_package_version = pkg_name in self._detect_pinned_packages()
            stamp = self._interface_stamp(
                pkg_share, self._detect_runtime_version(), use_ros_package_version
            )

            if binding_dir.exists():
                # A matching stamp beside a crate that lost its Cargo.toml is not
                # "up to date": cargo would resolve the patch to a directory it
                # cannot read, and report that against crates.io rather than here.
                if self._stamp_matches(stamp_file, stamp) and (binding_dir / "Cargo.toml").exists():
                    logger.debug(f"Bindings up to date for {pkg_name}")
                    continue
                # Remove rather than regenerate in place: bindgen does not
                # delete outputs for interfaces that no longer exist, so a
                # merged tree can keep a crate referring to a deleted message.
                logger.info(f"Interface definitions changed for {pkg_name}; regenerating bindings")
                shutil.rmtree(pkg_build_dir, ignore_errors=True)

            # Generate bindings using cargo ros2 bindgen
            logger.info(f"Generating bindings for {pkg_name}")
            try:
                self._run_bindgen(
                    pkg_name, pkg_share, pkg_build_dir, verbose, use_ros_package_version
                )
                # Post-process generated Cargo.toml to remove path dependencies
                # NOTE: This only modifies GENERATED bindings, not user's Cargo.toml
                self._fixup_generated_cargo_toml(pkg_name, binding_dir)
            except RuntimeError as e:
                # Log warning for packages that can't be generated (e.g., unsupported IDL features)
                # The stamp is deliberately NOT written, so the next build retries
                # instead of caching a partial result.
                logger.warning(f"Skipping {pkg_name}: {e}")
            else:
                self._write_stamp(stamp_file, stamp)
                self._write_manifest(binding_dir, pkg_share)

    @staticmethod
    def _interface_records(pkg_share: Path) -> str:
        """One line per interface definition: ``<relative path>:<size>:<digest>``.

        The digest is of the file's *contents*. Stat metadata was cheaper but
        answered the wrong question: a fresh ``git clone``, a ``cp -r``, or a
        container mount rewrites mtimes without changing a single definition, and
        every one of those made good bindings look stale. Measured on the 69
        interface packages a stock Humble install ships -- 1732 files, 1.2 MiB --
        digesting them costs 66 ms against 3 ms for stat, and a typical build
        touches four packages, or 7 ms. Repeat passes within a build hit the memo
        in :func:`_file_digest`.

        This text is the single source for both freshness checks: colcon compares
        its digest (:meth:`_interface_stamp`), and a generated crate's build.rs
        re-derives the same lines from the manifest written beside it.
        """
        lines: List[str] = []
        for subdir in ("msg", "srv", "action"):
            root = pkg_share / subdir
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.suffix not in INTERFACE_SUFFIXES or not path.is_file():
                    continue
                stat = path.stat()
                rel = path.relative_to(pkg_share)
                digest = _file_digest(path, stat.st_size, stat.st_mtime_ns)
                lines.append(f"{rel}:{stat.st_size}:{digest}")
        return "".join(f"{line}\n" for line in lines)

    @classmethod
    def _interface_stamp(
        cls,
        pkg_share: Path,
        runtime_version: Optional[str] = None,
        use_ros_package_version: bool = False,
    ) -> str:
        """Digest of a package's interface definitions and how they were generated.

        The runtime version is part of it because it lands in the generated
        Cargo.toml: change it without changing a `.msg`, and a digest over the
        definitions alone would report the stale crates as up to date. The crate
        version lands there for the same reason -- a consumer that changes
        ``std_msgs = "*"`` to ``std_msgs = "5.3"`` needs the crate regenerated,
        having touched no interface definition at all.

        Only the ROS-version case contributes, so the stamps of the ``*``
        workspaces this extension is normally used with are unchanged and their
        bindings are not regenerated on upgrade.
        """
        digest = hashlib.sha256(cls._interface_records(pkg_share).encode())
        if runtime_version:
            digest.update(f"rosidl_runtime_rs={runtime_version}\n".encode())
        if use_ros_package_version:
            digest.update(b"crate_version=ros_package\n")
        return digest.hexdigest()

    @classmethod
    def _write_manifest(cls, binding_dir: Path, pkg_share: Path):
        """Record what a generated crate was generated from, inside the crate.

        The crate's build.rs reads this to fail a plain ``cargo build`` against
        stale bindings, which is the one freshness check colcon cannot perform:
        cargo never consults the stamp file.
        """
        if not binding_dir.is_dir():
            # Generation produced nothing to annotate; not worth failing over.
            return
        try:
            content = f"{pkg_share.resolve()}\n{cls._interface_records(pkg_share)}"
            (binding_dir / MANIFEST_FILENAME).write_text(content)
        except OSError as e:
            # The build.rs check treats a missing manifest as "cannot tell" and
            # proceeds, so this costs a check, not a build.
            logger.warning(f"Could not write {binding_dir / MANIFEST_FILENAME}: {e}")

    @staticmethod
    def _stamp_matches(stamp_file: Path, expected: str) -> bool:
        """True when a previously written stamp equals `expected`."""
        try:
            return stamp_file.read_text().strip() == expected
        except OSError:
            # Missing (bindings predate stamping) or unreadable: treat as stale.
            return False

    @staticmethod
    def _write_stamp(stamp_file: Path, stamp: str):
        """Record the stamp, after generation has fully succeeded."""
        try:
            stamp_file.parent.mkdir(parents=True, exist_ok=True)
            stamp_file.write_text(stamp + "\n")
        except OSError as e:
            # Not fatal: the only consequence is regenerating next time.
            logger.warning(f"Could not write {stamp_file}: {e}")

    def _run_bindgen(
        self,
        pkg_name: str,
        pkg_share: Path,
        output_dir: Path,
        verbose: bool,
        use_ros_package_version: bool = False,
    ):
        """Generate Rust bindings for a single package using direct API call.

        Args:
            pkg_name: Name of the ROS package
            pkg_share: Path to the package's share/ directory
            output_dir: Path where bindings should be generated
            verbose: Enable verbose output
            use_ros_package_version: Stamp the crate with the ROS package's own
                version instead of the fixed one, for a package some consumer
                requires by version.
        """
        try:
            # The CLI override if given, else whatever the workspace's own
            # packages imply, else None to leave the generator's default alone.
            version = self._detect_runtime_version()

            # Create configuration for binding generation
            config = cargo_ros2_py.BindgenConfig(
                package_name=pkg_name,
                output_dir=str(output_dir),
                package_path=str(pkg_share),
                verbose=verbose,
                rosidl_runtime_rs_version=version,
                use_ros_package_version=use_ros_package_version,
            )

            # Call Rust function directly (no subprocess!)
            cargo_ros2_py.generate_bindings(config)

            if verbose:
                logger.info(f"✓ Generated bindings for {pkg_name}")

        except RuntimeError as e:
            logger.error(f"Failed to generate bindings for {pkg_name}: {e}")
            raise

    def _fixup_generated_cargo_toml(self, pkg_name: str, binding_dir: Path):
        """Post-process GENERATED Cargo.toml to convert path dependencies to version requirements.

        This is necessary because rosidl-bindgen generates Cargo.toml with local
        path dependencies (e.g., `std_msgs = { path = "../std_msgs" }`), but we want
        to use the .cargo/config.toml patches instead.

        NOTE: This ONLY modifies generated binding Cargo.toml files, NOT user's Cargo.toml files.
        Users are responsible for maintaining their own Cargo.toml dependencies.

        Args:
            pkg_name: Name of the ROS package
            binding_dir: Directory containing the generated bindings
        """
        # Find the Cargo.toml (nested structure: binding_dir/pkg_name/Cargo.toml)
        cargo_toml = binding_dir / pkg_name / "Cargo.toml"
        if not cargo_toml.exists():
            # Try top-level
            cargo_toml = binding_dir / "Cargo.toml"
            if not cargo_toml.exists():
                # This is expected for packages without interfaces (msg/srv/action)
                logger.debug(f"No Cargo.toml found for {pkg_name} (package has no interfaces)")
                return

        # Read the Cargo.toml
        content = cargo_toml.read_text()
        lines = content.split("\n")

        # Process each line to convert path dependencies to version requirements
        new_lines = []
        in_dependencies = False
        for line in lines:
            # Track when we're in [dependencies] or [build-dependencies] section
            if line.strip().startswith("[dependencies]") or line.strip().startswith(
                "[build-dependencies]"
            ):
                in_dependencies = True
                new_lines.append(line)
                continue
            elif line.strip().startswith("[") and in_dependencies:
                in_dependencies = False
                new_lines.append(line)
                continue

            # If we're in dependencies section and line has a path dependency, convert it
            if in_dependencies and "{ path =" in line:
                # Extract package name from line like: `std_msgs = { path = "../std_msgs" }`
                if "=" in line:
                    dep_name = line.split("=")[0].strip()
                    # Convert all path dependencies to version requirements
                    # including rosidl_runtime_rs (will be patched to shared location)
                    new_lines.append(f'{dep_name} = "*"')
                    continue

            new_lines.append(line)

        # Write back the modified Cargo.toml
        cargo_toml.write_text("\n".join(new_lines))
        logger.debug(f"Fixed up generated Cargo.toml for {pkg_name}")

    # -------------------------------------------------------------------------
    # Per-crate .cargo/config.toml generation (patches + build flags)
    # -------------------------------------------------------------------------

    # Marker comments delimiting the auto-generated patch region
    _MARKER_BEGIN = "# BEGIN colcon-cargo-ros2 generated patches"
    _MARKER_END = "# END colcon-cargo-ros2"

    # Marker comments delimiting the auto-generated build flags region
    _MARKER_BUILD_BEGIN = "# BEGIN colcon-cargo-ros2 generated build flags"
    _MARKER_BUILD_END = "# END colcon-cargo-ros2 build flags"

    # Marker comments delimiting the auto-generated environment region
    _MARKER_ENV_BEGIN = "# BEGIN colcon-cargo-ros2 generated environment"
    _MARKER_ENV_END = "# END colcon-cargo-ros2 environment"

    def _detect_cargo_workspace_root(self, crate_path: Path, colcon_ws_root: Path) -> Path:
        """Find the Cargo workspace root for a given crate.

        Walks up from *crate_path* toward *colcon_ws_root* looking for a
        ``Cargo.toml`` that contains a ``[workspace]`` section.

        Returns the directory that should receive ``.cargo/config.toml``.
        If no workspace is found, returns *crate_path* itself (standalone crate).
        """
        # 1. Check the crate's own Cargo.toml first
        if _cargo_toml_has_workspace(crate_path / "Cargo.toml"):
            return crate_path

        # 2. Walk up toward colcon workspace root
        current = crate_path.parent
        while current != colcon_ws_root and current != current.parent:
            cargo_toml = current / "Cargo.toml"
            if cargo_toml.exists() and _cargo_toml_has_workspace(cargo_toml):
                return current
            current = current.parent

        # 3. Also check the colcon workspace root itself
        cargo_toml = colcon_ws_root / "Cargo.toml"
        if cargo_toml.exists() and _cargo_toml_has_workspace(cargo_toml):
            return colcon_ws_root

        # 4. No workspace found — standalone crate
        return crate_path

    def _collect_ide_config_targets(self) -> Dict[Path, List[Tuple[str, Path]]]:
        """Collect deduplicated mapping of config targets to the crates they cover.

        Returns:
            Dict mapping each directory that should receive
            ``.cargo/config.toml`` to a list of ``(package name, crate path)``
            for the ROS Cargo crates it covers. The name is needed to look up
            which interface packages that crate actually depends on.
        """
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})
        targets: Dict[Path, List[Tuple[str, Path]]] = {}

        for _pkg_name, desc in cargo_descriptors.items():
            crate_path = Path(desc.path).resolve()
            colcon_ws_root = self.workspace_root.resolve()

            # Skip crates that are outside the colcon workspace
            try:
                crate_path.relative_to(colcon_ws_root)
            except ValueError:
                logger.warning(
                    f"Skipping IDE config for {_pkg_name}: "
                    f"crate path {crate_path} is outside colcon workspace {colcon_ws_root}"
                )
                continue

            target = self._detect_cargo_workspace_root(crate_path, colcon_ws_root)
            targets.setdefault(target.resolve(), []).append((_pkg_name, crate_path))

        return targets

    def _collect_binding_dirs(self, ros_packages: Dict[str, Path]) -> Dict[str, Path]:
        """Return a mapping of package name → binding directory that contains Cargo.toml.

        This mirrors the logic in ``_write_cargo_config_file`` to find where
        the generated Cargo.toml lives for each package.
        """
        binding_dirs: Dict[str, Path] = {}
        for pkg_name in sorted(ros_packages.keys()):
            pkg_build_dir = self.build_base / pkg_name / "rosidl_cargo"
            if not pkg_build_dir.exists():
                continue

            nested = pkg_build_dir / pkg_name
            if nested.exists() and (nested / "Cargo.toml").exists():
                binding_dirs[pkg_name] = nested
            elif (pkg_build_dir / "Cargo.toml").exists():
                binding_dirs[pkg_name] = pkg_build_dir

        return binding_dirs

    @staticmethod
    def _drop_missing_bindings(binding_dirs: Dict[str, Path]) -> Dict[str, Path]:
        """Drop entries whose crate is no longer readable, naming what was dropped.

        Collection and writing are separated by every package's build task, so a
        directory can go away in between -- a parallel ``colcon build --clean``,
        a hand-run ``rm -rf build/``. A patch to a directory with no Cargo.toml
        makes cargo report ``unable to update <path>`` against the *consumer*, so
        it is better to omit the patch and let the missing-bindings check speak.
        """
        kept: Dict[str, Path] = {}
        dropped: List[str] = []
        for pkg_name, binding_dir in binding_dirs.items():
            if (binding_dir / "Cargo.toml").is_file():
                kept[pkg_name] = binding_dir
            else:
                dropped.append(pkg_name)

        if dropped:
            _warn_once(
                "vanished-bindings:" + ",".join(sorted(dropped)),
                "Generated bindings disappeared between generation and config "
                "writing for: " + ", ".join(sorted(dropped)) + ". Re-run `colcon build`.",
            )
        return kept

    @staticmethod
    def _assert_no_missing_bindings(ros_packages: Dict[str, Path], binding_dirs: Dict[str, Path]):
        """Fail when a required interface package has no generated bindings.

        Without this, a package that failed to generate simply gets no
        ``[patch.crates-io]`` entry, cargo resolves it against the real
        registry instead, and the build dies somewhere unrelated -- e.g.
        ``failed to select a version for the requirement `lifecycle_msgs = "*"`
        ... version 1.2.1 is yanked``. That error names crates.io, not the
        package whose bindings are missing, so the real cause (a bindgen
        failure, or a stale lock suppressing generation entirely) stays hidden.

        Raises:
            RuntimeError: naming every package that is missing bindings.
        """
        missing = sorted(set(ros_packages) - set(binding_dirs))
        if not missing:
            return
        raise RuntimeError(
            "No Rust bindings were generated for: "
            + ", ".join(missing)
            + ".\nThese interface packages are required by this workspace, so cargo "
            "would fall back to crates.io and fail with an unrelated version or "
            "'yanked' error. Check the bindgen warnings above for the underlying "
            "failure, then re-run the build."
        )

    def _select_bindings_for_target(
        self, crates: List[Tuple[str, Path]], binding_dirs: Dict[str, Path]
    ) -> Dict[str, Path]:
        """Pick the bindings a single Cargo workspace / crate actually needs.

        Every Cargo target used to receive a ``[patch.crates-io]`` entry for every
        interface package anything in the colcon workspace depended on. Cargo warns
        once per unused patch ("patch `X` was not used in the crate graph"), which
        on a large workspace buries real diagnostics under a hundred warnings.

        Selection is driven by ``package.xml``, not by parsing the consumer's
        ``Cargo.toml``. Re-deriving Cargo's own dependency resolution is a losing
        game: it has to account for ``[target.'cfg(...)'.dependencies]``, workspace
        members implied by path dependencies rather than listed in ``members``,
        renamed packages, git and out-of-tree path dependencies, and more. Any gap
        drops a needed patch, and a dropped patch is not a warning -- cargo silently
        resolves that name against the real crates.io and fails somewhere unrelated
        (see _assert_no_missing_bindings). package.xml is this project's declared
        source of truth for ROS dependencies and cannot have those gaps.

        Over-inclusion is the deliberate failure direction: a package declared in
        package.xml but unused in Cargo.toml keeps its patch and costs one warning.
        A package whose attribution is unknown disables narrowing altogether for
        that target, degrading to the previous patch-everything behaviour.
        """
        selected: Set[str] = set()

        for pkg_name, _crate_path in crates:
            deps = self._package_interface_deps.get(pkg_name)
            if deps is None:
                # Unknown, not empty. Narrowing here would risk omitting a patch.
                logger.debug(
                    f"No dependency attribution for {pkg_name}; "
                    "using all bindings for this Cargo target"
                )
                return dict(binding_dirs)
            selected |= deps

        # Invariant: selected comes from the interface packages that were also fed
        # to binding generation, so the global check has already covered these. Kept
        # as a guard in case a future change feeds the attribution from elsewhere.
        missing = sorted(selected - set(binding_dirs))
        if missing:
            raise RuntimeError(
                "No Rust bindings were generated for: "
                + ", ".join(missing)
                + ".\nThese interface packages are required by "
                + ", ".join(sorted(name for name, _ in crates))
                + ", so cargo would fall back to crates.io and fail with an "
                "unrelated version or 'yanked' error."
            )

        return {name: binding_dirs[name] for name in sorted(selected)}

    @staticmethod
    def _compute_relative_patches(config_target: Path, binding_dirs: Dict[str, Path]) -> List[str]:
        """Compute ``[patch.crates-io]`` entries with paths relative to *config_target*.

        Args:
            config_target: Directory that will contain ``.cargo/config.toml``.
            binding_dirs: Mapping of package name → absolute binding directory.

        Returns:
            Sorted list of TOML lines like ``std_msgs = { path = "../../build/..." }``.
        """
        patches: List[str] = []
        for pkg_name in sorted(binding_dirs.keys()):
            binding_dir = binding_dirs[pkg_name].resolve()
            rel = os.path.relpath(binding_dir, config_target.resolve())
            # Use forward slashes for cross-platform TOML compatibility
            rel = rel.replace(os.sep, "/")
            patches.append(f'{pkg_name} = {{ path = "{rel}" }}')
        return patches

    @classmethod
    def _generate_marker_block(cls, patches: List[str]) -> str:
        """Produce the text block delimited by BEGIN/END markers.

        The block does **not** include a ``[patch.crates-io]`` header — the
        merge logic handles placement within an existing or new section.
        """
        lines = [
            cls._MARKER_BEGIN,
            "# Auto-generated by colcon build. Do not edit between markers.",
            "# Re-run `colcon build` to regenerate.",
        ]
        lines.extend(patches)
        lines.append(cls._MARKER_END)
        return "\n".join(lines)

    @classmethod
    def _merge_section(
        cls,
        existing_content: Optional[str],
        section: str,
        marker_begin: str,
        marker_end: str,
        marker_block: str,
    ) -> str:
        """Merge *marker_block* into the ``[section]`` table of *existing_content*.

        Handles three cases:
        1. Existing markers found → replace content between them.
        2. The section exists but has no markers → append the block before the
           next section header.
        3. The section does not exist → append it at the end.

        Everything outside the markers is preserved byte for byte, which a TOML
        round-trip would not do.

        Returns the full file content to be written.
        """
        if not existing_content:
            # Brand-new file
            return f"[{section}]\n{marker_block}\n"

        lines = existing_content.splitlines()

        # --- Case 1: markers already present ---
        begin_idx: Optional[int] = None
        end_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if line.strip() == marker_begin:
                begin_idx = i
            elif line.strip() == marker_end and begin_idx is not None:
                end_idx = i
                break

        if begin_idx is not None and end_idx is not None:
            new_lines = lines[:begin_idx] + marker_block.splitlines() + lines[end_idx + 1 :]
            return "\n".join(new_lines) + "\n"

        # --- Case 2: section exists but no markers ---
        header = f"[{section}]"
        header_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if line.strip() == header:
                header_idx = i
                break

        if header_idx is not None:
            # Find the end of the section
            insert_idx = len(lines)  # default: end of file
            for i in range(header_idx + 1, len(lines)):
                stripped = lines[i].strip()
                if stripped.startswith("[") and stripped != header:
                    insert_idx = i
                    break

            new_lines = lines[:insert_idx] + [marker_block] + lines[insert_idx:]
            return "\n".join(new_lines) + "\n"

        # --- Case 3: no such section at all ---
        # Ensure a trailing newline before the new section
        content = existing_content.rstrip("\n") + "\n"
        content += f"\n[{section}]\n{marker_block}\n"
        return content

    @classmethod
    def _merge_into_config(cls, existing_content: Optional[str], marker_block: str) -> str:
        """Merge the ``[patch.crates-io]`` marker block into a config file."""
        return cls._merge_section(
            existing_content,
            "patch.crates-io",
            cls._MARKER_BEGIN,
            cls._MARKER_END,
            marker_block,
        )

    @staticmethod
    def _has_library_files(lib_dir: Path) -> bool:
        """True when *lib_dir* holds something a linker could actually use.

        A Rust package that installs only executables still gets an
        ``install/<pkg>/lib`` directory (its binaries live in ``lib/<pkg>/``),
        so directory existence alone says nothing about linkability.
        """
        suffixes = (".so", ".dylib", ".a", ".dll", ".lib")
        try:
            entries = list(lib_dir.iterdir())
        except OSError:
            return False
        for entry in entries:
            if not entry.is_file():
                continue
            # `.so.1` and friends are versioned sonames, still linkable targets.
            if entry.name.endswith(suffixes) or ".so." in entry.name:
                return True
        return False

    def _select_lib_packages_for_target(self, crates: List[Tuple[str, Path]]) -> Optional[Set[str]]:
        """Packages whose installed libraries a Cargo target may need to link.

        Unlike patch selection this is not limited to interface packages: a crate
        can link a C library from any ROS package it declares.

        Returns None when any crate's dependencies are unknown, which means
        "fall back to every install prefix" rather than "needs nothing".
        """
        selected: Set[str] = set()
        for pkg_name, _crate_path in crates:
            deps = self._package_all_deps.get(pkg_name)
            if deps is None:
                logger.debug(
                    f"No dependency attribution for {pkg_name}; "
                    "using all install prefixes for this Cargo target"
                )
                return None
            selected |= deps
        return selected

    def _library_search_dirs(self, lib_packages: Optional[Set[str]]) -> List[Path]:
        """Absolute library directories to put on the linker search path.

        Args:
            lib_packages: Workspace packages this Cargo target depends on, or
                None to consider every installed package (unknown attribution).
        """
        dirs: List[Path] = []

        if self.install_base.exists():
            if lib_packages is None:
                prefixes = [p for p in sorted(self.install_base.iterdir()) if p.is_dir()]
            else:
                prefixes = [self.install_base / name for name in sorted(lib_packages)]
            for prefix in prefixes:
                lib_dir = prefix / "lib"
                if lib_dir.is_dir() and self._has_library_files(lib_dir):
                    dirs.append(lib_dir.absolute())

        install_base = self.install_base.resolve()
        for prefix in os.environ.get("AMENT_PREFIX_PATH", "").split(":"):
            if not prefix:
                continue
            # Skip this workspace's own prefixes. They arrive here only when the
            # user happened to source install/setup.bash before rebuilding, and
            # taking them would silently widen the search path on the second
            # build -- the generated config must not depend on whether the
            # workspace was sourced. The narrowed selection above already covers
            # them.
            try:
                Path(prefix).resolve().relative_to(install_base)
                continue
            except ValueError:
                pass
            lib_dir = Path(prefix) / "lib"
            if lib_dir.is_dir() and self._has_library_files(lib_dir):
                lib_dir = lib_dir.absolute()
                if lib_dir not in dirs:
                    dirs.append(lib_dir)

        return dirs

    def _rpath_flags(self, lib_dir: Path) -> List[str]:
        """Link arguments baking *lib_dir* into the binary's runtime search path.

        Without these, a binary built by a bare ``cargo run`` cannot find the ROS
        shared libraries: cargo overwrites ``LD_LIBRARY_PATH`` for the processes it
        launches, so an ``[env]`` entry cannot supply them.

        ``--disable-new-dtags`` is required on Linux. The default emits ``RUNPATH``,
        which the loader does not apply to *transitive* libraries -- and ROS
        typesupport libraries are exactly that: the executable needs
        ``libstd_msgs__rosidl_typesupport_c.so``, which itself needs
        ``librosidl_typesupport_c.so``. ``RPATH`` does apply transitively.

        Workspace-internal directories additionally get ``$ORIGIN``-relative
        entries, so that a workspace which is moved, renamed or copied elsewhere
        keeps working; see :meth:`_relative_rpaths`.
        """
        if getattr(self.args, "no_rpath", False):
            return []
        if sys.platform.startswith("win"):
            # No rpath concept; the loader uses PATH.
            return []

        flags = []
        for path in [str(lib_dir)] + self._relative_rpaths(lib_dir):
            if sys.platform == "darwin":
                # Mach-O has a single rpath notion, and no dtags to disable.
                flags.append(f'"-C", "link-arg=-Wl,-rpath,{path}"')
            else:
                flags.append(f'"-C", "link-arg=-Wl,-rpath,{path},--disable-new-dtags"')
        return flags

    def _relative_rpaths(self, lib_dir: Path) -> List[str]:
        """``$ORIGIN``-relative forms of *lib_dir*, for binaries that may move.

        Only for directories inside this workspace's install base. A system
        prefix such as ``/opt/ros/humble/lib`` does not travel with the
        workspace, so an absolute entry is the correct thing for it.

        The distance from a binary to the install tree depends on where the
        binary is, and the same binary exists in two places: cargo's target
        directory and the installed copy. Both are emitted, plus the
        cross-compilation variant with a target triple in the path. Entries that
        do not resolve cost nothing -- the loader simply tries the next one.
        """
        try:
            relative = lib_dir.resolve().relative_to(self.install_base.resolve())
        except ValueError:
            return []

        # macOS spells the token differently but resolves it the same way.
        token = "@loader_path" if sys.platform == "darwin" else "$ORIGIN"
        install_dir = self.install_base.name

        return [
            # install/<pkg>/lib/<pkg>/<binary>
            f"{token}/../../../{relative}",
            # build/.cargo_target/<slug>/<profile>/<binary>
            f"{token}/../../../../{install_dir}/{relative}",
            # ... with a target triple, one level deeper
            f"{token}/../../../../../{install_dir}/{relative}",
        ]

    def _compute_rustflags(self, lib_packages: Optional[Set[str]]) -> List[str]:
        """Compute ``-L native=<path>`` search flags and matching rpath arguments.

        Paths are absolute: Cargo resolves rustflags relative to the invocation
        directory, not to the config file's location.
        """
        rustflags: List[str] = []
        for lib_dir in self._library_search_dirs(lib_packages):
            rustflags.append(f'"-L", "native={lib_dir}"')
            rustflags.extend(self._rpath_flags(lib_dir))
        return rustflags

    def _compute_target_dir(self, config_target: Path) -> Path:
        """Where cargo should put build artifacts for *config_target*.

        Keeping this under the colcon build base rather than in ``src/`` leaves
        the source tree clean, while both colcon and a manual ``cargo build``
        keep sharing one cache: they read the same ``.cargo/config.toml``.
        """
        try:
            relative = config_target.resolve().relative_to(self.workspace_root.resolve())
            slug = "_".join(relative.parts) or config_target.name
        except ValueError:
            slug = config_target.name
        return (self.build_base / ".cargo_target" / slug).absolute()

    @classmethod
    def _generate_build_marker_block(
        cls, rustflags: List[str], target_dir: Optional[str] = None
    ) -> str:
        """Produce the ``[build]`` marker block with rustflags and target-dir.

        The block does **not** include a ``[build]`` header — the
        merge logic handles placement within an existing or new section.
        """
        lines = [
            cls._MARKER_BUILD_BEGIN,
            "# Auto-generated by colcon build. Do not edit between markers.",
            "# Re-run `colcon build` to regenerate.",
        ]
        if target_dir is not None:
            lines.append(f'target-dir = "{target_dir}"')
        if rustflags:
            lines.append("rustflags = [")
            for i, flag in enumerate(rustflags):
                comma = "," if i < len(rustflags) - 1 else ""
                lines.append(f"    {flag}{comma}")
            lines.append("]")
        else:
            lines.append("rustflags = []")
        lines.append(cls._MARKER_BUILD_END)
        return "\n".join(lines)

    @classmethod
    def _has_user_target_dir(cls, content: str) -> bool:
        """True when the user set their own ``target-dir`` outside our markers."""
        inside = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == cls._MARKER_BUILD_BEGIN:
                inside = True
            elif stripped == cls._MARKER_BUILD_END:
                inside = False
            elif not inside and stripped.startswith("target-dir"):
                return True
        return False

    @classmethod
    def _merge_build_into_config(cls, existing_content: str, build_marker_block: str) -> str:
        """Merge *build_marker_block* into *existing_content* for the ``[build]`` section.

        Handles three cases:
        1. Build markers already present → replace content between them.
        2. ``[build]`` section exists but no markers → append block
           before the next section header.
        3. No ``[build]`` section → append new ``[build]`` section at end.

        Args:
            existing_content: Current file content (must not be empty/None).
            build_marker_block: The marker block to merge.

        Returns the full file content to be written.
        """
        return cls._merge_section(
            existing_content,
            "build",
            cls._MARKER_BUILD_BEGIN,
            cls._MARKER_BUILD_END,
            build_marker_block,
        )

    # -------------------------------------------------------------------------
    # [env]: the environment generated-crate build scripts need
    # -------------------------------------------------------------------------

    def _compute_env(self) -> Dict[str, str]:
        """Environment entries to bake into the config.

        ``rosidl_runtime_rs`` and the generated crates read ``AMENT_PREFIX_PATH``
        from their build scripts and panic without it, which is what makes a
        ``cargo build`` in an unsourced shell fail. Baking the value in -- workspace
        install prefixes ahead of whatever the generating shell had -- lets those
        build scripts run unaided.
        """
        prefixes: List[str] = []
        if self.install_base.exists():
            for pkg_install in sorted(self.install_base.iterdir()):
                if pkg_install.is_dir():
                    prefixes.append(str(pkg_install.absolute()))

        for prefix in os.environ.get("AMENT_PREFIX_PATH", "").split(":"):
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)

        if not prefixes:
            return {}
        return {"AMENT_PREFIX_PATH": ":".join(prefixes)}

    @classmethod
    def _generate_env_marker_block(cls, env: Dict[str, str]) -> str:
        """Produce the ``[env]`` marker block.

        Entries use ``force = false`` (cargo's default) so that a sourced
        environment still wins: a user working in an overlay workspace must not
        have it shadowed by a value baked in at build time.
        """
        lines = [
            cls._MARKER_ENV_BEGIN,
            "# Auto-generated by colcon build. Do not edit between markers.",
            "# Re-run `colcon build` to regenerate.",
        ]
        for name in sorted(env):
            lines.append(f'{name} = {{ value = "{env[name]}", force = false }}')
        lines.append(cls._MARKER_ENV_END)
        return "\n".join(lines)

    @classmethod
    def _merge_env_into_config(cls, existing_content: str, env_marker_block: str) -> str:
        """Merge the ``[env]`` marker block into a config file."""
        return cls._merge_section(
            existing_content,
            "env",
            cls._MARKER_ENV_BEGIN,
            cls._MARKER_ENV_END,
            env_marker_block,
        )

    # -------------------------------------------------------------------------
    # .gitignore hygiene
    # -------------------------------------------------------------------------

    _GITIGNORE_BEGIN = "# BEGIN colcon-cargo-ros2"
    _GITIGNORE_END = "# END colcon-cargo-ros2"

    def _ensure_gitignored(self, config_target: Path):
        """Ignore the generated config, which cannot live outside the source tree.

        Cargo finds ``.cargo/config.toml`` by walking up from the crate, so the
        file has to sit next to the sources. Its contents are machine-specific
        absolute paths, so every workspace using this tool would otherwise carry a
        permanently dirty file.
        """
        if getattr(self.args, "no_gitignore", False):
            return
        if not _git_succeeds(["rev-parse", "--is-inside-work-tree"], config_target):
            return

        entry = ".cargo/config.toml"
        if _git_succeeds(["check-ignore", "-q", entry], config_target):
            return

        gitignore = config_target / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        block = "\n".join([self._GITIGNORE_BEGIN, entry, self._GITIGNORE_END])

        lines = existing.splitlines()
        begin_idx = end_idx = None
        for i, line in enumerate(lines):
            if line.strip() == self._GITIGNORE_BEGIN:
                begin_idx = i
            elif line.strip() == self._GITIGNORE_END and begin_idx is not None:
                end_idx = i
                break

        if begin_idx is not None and end_idx is not None:
            merged = "\n".join(lines[:begin_idx] + block.splitlines() + lines[end_idx + 1 :])
        elif existing.strip():
            merged = existing.rstrip("\n") + "\n\n" + block
        else:
            merged = block

        gitignore.write_text(merged + "\n")
        logger.debug(f"Ignoring {entry} in {gitignore}")

    def _write_cargo_configs(self, ros_packages: Dict[str, Path]):
        """Generate ``.cargo/config.toml`` for each Cargo workspace / standalone crate.

        Writes ``[patch.crates-io]`` entries (dependency resolution), ``[build]``
        rustflags and target-dir (linking and artifact placement), and ``[env]``
        (what generated build scripts read). This is the single config used by
        ``colcon build``, a bare ``cargo`` invocation, and IDEs alike.
        """
        binding_dirs = self._drop_missing_bindings(self._collect_binding_dirs(ros_packages))
        self._assert_no_missing_bindings(ros_packages, binding_dirs)
        if not binding_dirs:
            return

        targets = self._collect_ide_config_targets()
        if not targets:
            return

        env = self._compute_env()
        generated_count = 0

        for config_target, crates in targets.items():
            target_binding_dirs = self._select_bindings_for_target(crates, binding_dirs)
            patches = self._compute_relative_patches(config_target, target_binding_dirs)
            rustflags = self._compute_rustflags(self._select_lib_packages_for_target(crates))

            config_dir = config_target / ".cargo"
            config_file = config_dir / "config.toml"

            # Read existing content (if any)
            existing_content = None
            if config_file.exists():
                existing_content = config_file.read_text()

            # A target-dir the user set themselves stays authoritative; ours is a
            # default for workspaces that express no preference.
            target_dir: Optional[str] = str(self._compute_target_dir(config_target))
            if existing_content and self._has_user_target_dir(existing_content):
                logger.info(
                    f"Keeping the target-dir already set in {config_file}; "
                    "cargo artifacts stay where that points."
                )
                target_dir = None

            # Written even when there are no patches: the [build] rustflags are
            # still needed for linking, and rewriting the marker block is what
            # clears patches left over from a previous build.
            patch_marker_block = self._generate_marker_block(patches)
            build_marker_block = self._generate_build_marker_block(rustflags, target_dir)
            env_marker_block = self._generate_env_marker_block(env)

            # Merge patches first, then build flags, then environment
            new_content = self._merge_into_config(existing_content, patch_marker_block)
            new_content = self._merge_build_into_config(new_content, build_marker_block)
            new_content = self._merge_env_into_config(new_content, env_marker_block)

            # Write the file
            config_dir.mkdir(parents=True, exist_ok=True)
            config_file.write_text(new_content)
            self._ensure_gitignored(config_target)
            generated_count += 1

            crate_names = [crate_path.name for _name, crate_path in crates]
            logger.info(
                f"Wrote .cargo/config.toml with {len(patches)} patches "
                f"and {len(rustflags)} rustflags to {config_file} "
                f"(crates: {', '.join(crate_names)})"
            )

        if generated_count > 0:
            logger.debug(f"Generated {generated_count} .cargo/config.toml file(s)")


def generate_workspace_bindings(
    workspace_root: Path,
    build_base: Path,
    install_base: Path,
    args,
    verbose: bool = False,
):
    """Generate bindings for an entire workspace (convenience function).

    Args:
        workspace_root: Root directory of the colcon workspace
        build_base: Base directory for build artifacts
        install_base: Base directory for installed packages
        args: Colcon command line arguments
        verbose: Enable verbose output
    """
    generator = WorkspaceBindingGenerator(workspace_root, build_base, install_base, args)

    # Only generate if we're the first process to acquire the lock.
    # fcntl advisory locking ensures the lock is released automatically
    # if the process is killed, preventing stale locks.
    if generator.try_acquire_lock():
        try:
            generator.generate_all_bindings(verbose)
        finally:
            generator.release_lock()
    else:
        logger.info("Binding generation already handled by another process")
