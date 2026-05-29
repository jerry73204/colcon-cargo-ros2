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
import glob
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from colcon_core.logging import colcon_logger

# Import Rust library directly via PyO3 bindings
from colcon_cargo_ros2 import cargo_ros2_py

logger = colcon_logger.getChild(__name__)


def _load_toml_file(cargo_toml_path: Path) -> Optional[dict]:
    """Load a TOML file, returning ``None`` if it cannot be parsed."""
    if not cargo_toml_path.exists():
        return None
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        with open(cargo_toml_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def _cargo_toml_has_workspace(cargo_toml_path: Path) -> bool:
    """Check whether a Cargo.toml contains a ``[workspace]`` section.

    Uses simple TOML parsing (tomllib/tomli) to avoid false positives from
    string matching in comments or values.
    """
    data = _load_toml_file(cargo_toml_path)
    return bool(data and "workspace" in data)


def _collect_dependency_names_from_section(section: object) -> Set[str]:
    """Collect crate package names referenced by a Cargo dependency section."""
    names: Set[str] = set()
    if not isinstance(section, dict):
        return names

    for dep_name, dep_value in section.items():
        names.add(dep_name)
        if isinstance(dep_value, dict):
            package_name = dep_value.get("package")
            if isinstance(package_name, str):
                names.add(package_name)
    return names


def _collect_dependency_names_from_manifest(cargo_data: dict) -> Set[str]:
    """Collect dependency package names from the manifest sections Cargo resolves."""
    dependency_names: Set[str] = set()
    for section_name in ("dependencies", "build-dependencies", "dev-dependencies"):
        dependency_names.update(
            _collect_dependency_names_from_section(cargo_data.get(section_name))
        )

    workspace = cargo_data.get("workspace")
    if isinstance(workspace, dict):
        dependency_names.update(
            _collect_dependency_names_from_section(workspace.get("dependencies"))
        )

    return dependency_names


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

        for pkg_name, desc in cargo_descriptors.items():
            # Get build + run dependencies (interface packages needed at compile time)
            # desc.dependencies is populated by Colcon's RosPackageIdentification
            # from package.xml using catkin_pkg
            deps = desc.get_dependencies(categories=["build", "run"])
            dep_names = [d.name for d in deps]
            required_packages.update(dep_names)

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

        return interface_packages

    def _validate_cargo_dependencies(self, interface_packages: Dict[str, Path]):
        """Validate that Cargo.toml dependencies match package.xml interface packages.

        Prints warnings if there are mismatches between what's declared in package.xml
        and what's actually used in Cargo.toml.

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
                # Parse Cargo.toml to extract dependencies
                # Use tomllib (Python 3.11+) or tomli (Python 3.8-3.10)
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib

                with open(cargo_toml_path, "rb") as f:
                    cargo_data = tomllib.load(f)

                # Get all dependencies from Cargo.toml (regular + build-dependencies)
                cargo_deps = set()
                if "dependencies" in cargo_data:
                    cargo_deps.update(cargo_data["dependencies"].keys())
                if "build-dependencies" in cargo_data:
                    cargo_deps.update(cargo_data["build-dependencies"].keys())

                # Get interface packages from package.xml
                xml_deps = desc.get_dependencies(categories=["build", "run"])
                xml_interface_deps = set(d.name for d in xml_deps if d.name in interface_packages)

                # Check for interface packages in package.xml but not in Cargo.toml
                missing_in_cargo = xml_interface_deps - cargo_deps
                if missing_in_cargo:
                    logger.warning(
                        f"{pkg_name}: Interface packages in package.xml but not in Cargo.toml: "
                        f"{', '.join(sorted(missing_in_cargo))}"
                    )

                # Check for ROS packages in Cargo.toml but not in package.xml
                # (Only check packages that we generated bindings for)
                extra_in_cargo = cargo_deps & set(interface_packages.keys()) - xml_interface_deps
                if extra_in_cargo:
                    logger.warning(
                        f"{pkg_name}: Interface packages in Cargo.toml but not in package.xml: "
                        f"{', '.join(sorted(extra_in_cargo))}. "
                        "Add them to package.xml with <depend> tags."
                    )

            except Exception as e:
                logger.debug(f"Could not validate Cargo.toml for {pkg_name}: {e}")

    def _find_workspace_interface_packages(self, required_packages: set):
        """Find interface packages in the workspace from source directories.

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

            # Check if bindings already exist and are up-to-date
            # Generated structure is: build/<pkg_name>/rosidl_cargo/<pkg_name>/Cargo.toml
            binding_dir = pkg_build_dir / pkg_name
            if binding_dir.exists():
                # TODO: Add checksum-based cache validation
                logger.debug(f"Bindings already exist for {pkg_name}")
                continue

            # Generate bindings using cargo ros2 bindgen
            logger.info(f"Generating bindings for {pkg_name}")
            try:
                self._run_bindgen(pkg_name, pkg_share, pkg_build_dir, verbose)
                # Post-process generated Cargo.toml to remove path dependencies
                # NOTE: This only modifies GENERATED bindings, not user's Cargo.toml
                self._fixup_generated_cargo_toml(pkg_name, binding_dir)
            except RuntimeError as e:
                # Log warning for packages that can't be generated (e.g., unsupported IDL features)
                logger.warning(f"Skipping {pkg_name}: {e}")

    def _run_bindgen(self, pkg_name: str, pkg_share: Path, output_dir: Path, verbose: bool):
        """Generate Rust bindings for a single package using direct API call.

        Args:
            pkg_name: Name of the ROS package
            pkg_share: Path to the package's share/ directory
            output_dir: Path where bindings should be generated
            verbose: Enable verbose output
        """
        try:
            # Extract optional version override from colcon args
            version = getattr(self.args, "rosidl_runtime_rs_version", None)

            # Create configuration for binding generation
            config = cargo_ros2_py.BindgenConfig(
                package_name=pkg_name,
                output_dir=str(output_dir),
                package_path=str(pkg_share),
                verbose=verbose,
                rosidl_runtime_rs_version=version,
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

    def _collect_ide_config_targets(self) -> Dict[Path, List[Path]]:
        """Collect deduplicated mapping of config targets to crate paths.

        Returns:
            Dict mapping each directory that should receive
            ``.cargo/config.toml`` to the list of ROS Cargo crates it covers.
        """
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        cargo_descriptors = getattr(RustBindingAugmentation, "_cargo_descriptors", {})
        targets: Dict[Path, List[Path]] = {}

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
            targets.setdefault(target.resolve(), []).append(crate_path)

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
    def _merge_into_config(cls, existing_content: Optional[str], marker_block: str) -> str:
        """Merge *marker_block* into *existing_content* preserving user content.

        Handles three cases:
        1. Existing markers found → replace content between them.
        2. ``[patch.crates-io]`` section exists but no markers → append block
           before the next section header.
        3. No ``[patch.crates-io]`` section → append new section at end.

        Returns the full file content to be written.
        """
        if not existing_content:
            # Brand-new file
            return f"[patch.crates-io]\n{marker_block}\n"

        lines = existing_content.splitlines()

        # --- Case 1: markers already present ---
        begin_idx: Optional[int] = None
        end_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if line.strip() == cls._MARKER_BEGIN:
                begin_idx = i
            elif line.strip() == cls._MARKER_END and begin_idx is not None:
                end_idx = i
                break

        if begin_idx is not None and end_idx is not None:
            new_lines = lines[:begin_idx] + marker_block.splitlines() + lines[end_idx + 1 :]
            return "\n".join(new_lines) + "\n"

        # --- Case 2: [patch.crates-io] section exists but no markers ---
        patch_header_re = re.compile(r"^\[patch\.crates-io\]")
        next_section_re = re.compile(r"^\[(?!patch\.crates-io)")

        patch_header_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if patch_header_re.match(line.strip()):
                patch_header_idx = i
                break

        if patch_header_idx is not None:
            # Find the end of the [patch.crates-io] section
            insert_idx = len(lines)  # default: end of file
            for i in range(patch_header_idx + 1, len(lines)):
                if next_section_re.match(lines[i].strip()):
                    insert_idx = i
                    break

            new_lines = lines[:insert_idx] + [marker_block] + lines[insert_idx:]
            return "\n".join(new_lines) + "\n"

        # --- Case 3: no [patch.crates-io] section at all ---
        # Ensure a trailing newline before the new section
        content = existing_content.rstrip("\n") + "\n"
        content += f"\n[patch.crates-io]\n{marker_block}\n"
        return content

    def _compute_rustflags(self) -> List[str]:
        """Compute ``-L native=<path>`` linker search flags.

        Collects library directories from:
        1. Workspace install directory (per-package lib/ dirs)
        2. System ROS library paths from ``AMENT_PREFIX_PATH``

        Returns absolute paths (required because Cargo resolves rustflags
        relative to CWD, not the config file location).
        """
        rustflags: List[str] = []

        # Add workspace install directory lib paths
        if self.install_base.exists():
            for pkg_install in sorted(self.install_base.iterdir()):
                if not pkg_install.is_dir():
                    continue
                lib_dir = pkg_install / "lib"
                if lib_dir.exists():
                    rustflags.append(f'"-L", "native={lib_dir.absolute()}"')

        # Add system ROS library paths from AMENT_PREFIX_PATH
        if "AMENT_PREFIX_PATH" in os.environ:
            for prefix in os.environ["AMENT_PREFIX_PATH"].split(":"):
                lib_path = Path(prefix) / "lib"
                if lib_path.exists():
                    rustflags.append(f'"-L", "native={lib_path.absolute()}"')

        return rustflags

    @classmethod
    def _generate_build_marker_block(cls, rustflags: List[str]) -> str:
        """Produce the ``[build]`` marker block with rustflags.

        The block does **not** include a ``[build]`` header — the
        merge logic handles placement within an existing or new section.
        """
        lines = [
            cls._MARKER_BUILD_BEGIN,
            "# Auto-generated by colcon build. Do not edit between markers.",
            "# Re-run `colcon build` to regenerate.",
        ]
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
        lines = existing_content.splitlines()

        # --- Case 1: build markers already present ---
        begin_idx: Optional[int] = None
        end_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if line.strip() == cls._MARKER_BUILD_BEGIN:
                begin_idx = i
            elif line.strip() == cls._MARKER_BUILD_END and begin_idx is not None:
                end_idx = i
                break

        if begin_idx is not None and end_idx is not None:
            new_lines = lines[:begin_idx] + build_marker_block.splitlines() + lines[end_idx + 1 :]
            return "\n".join(new_lines) + "\n"

        # --- Case 2: [build] section exists but no markers ---
        build_header_re = re.compile(r"^\[build\]$")
        next_section_re = re.compile(r"^\[(?!build\])")

        build_header_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if build_header_re.match(line.strip()):
                build_header_idx = i
                break

        if build_header_idx is not None:
            # Find the end of the [build] section
            insert_idx = len(lines)  # default: end of file
            for i in range(build_header_idx + 1, len(lines)):
                if next_section_re.match(lines[i].strip()):
                    insert_idx = i
                    break

            new_lines = lines[:insert_idx] + [build_marker_block] + lines[insert_idx:]
            return "\n".join(new_lines) + "\n"

        # --- Case 3: no [build] section at all ---
        content = existing_content.rstrip("\n") + "\n"
        content += f"\n[build]\n{build_marker_block}\n"
        return content

    @staticmethod
    def _cargo_dependency_names(cargo_toml_path: Path) -> Set[str]:
        """Return dependency crate/package names from a Cargo.toml file."""
        cargo_data = _load_toml_file(cargo_toml_path)
        if cargo_data is None:
            logger.debug(f"Could not parse Cargo.toml dependency names from {cargo_toml_path}")
            return set()

        return _collect_dependency_names_from_manifest(cargo_data)

    @staticmethod
    def _workspace_member_manifests(workspace_root: Path) -> List[Path]:
        """Return Cargo.toml paths for members under a Cargo workspace root."""
        root_manifest = workspace_root / "Cargo.toml"
        cargo_data = _load_toml_file(root_manifest)
        if cargo_data is None:
            return []

        workspace = cargo_data.get("workspace")
        if not isinstance(workspace, dict):
            return []

        members = workspace.get("members", [])
        if not isinstance(members, list):
            return []

        excludes = workspace.get("exclude", [])
        if not isinstance(excludes, list):
            excludes = []

        excluded_paths = set()
        for pattern in excludes:
            if not isinstance(pattern, str):
                continue
            for excluded in glob.glob(str(workspace_root / pattern)):
                excluded_paths.add(Path(excluded).resolve())

        manifests: List[Path] = []
        seen: Set[Path] = set()
        for pattern in members:
            if not isinstance(pattern, str):
                continue
            for member in glob.glob(str(workspace_root / pattern)):
                member_path = Path(member).resolve()
                if member_path in excluded_paths:
                    continue
                manifest = member_path / "Cargo.toml"
                if manifest.exists() and manifest not in seen:
                    manifests.append(manifest)
                    seen.add(manifest)

        return manifests

    def _cargo_dependency_names_for_target(
        self, config_target: Path, crate_paths: List[Path]
    ) -> Set[str]:
        """Return dependency names used by the target Cargo workspace/crates."""
        manifests = [config_target / "Cargo.toml"]
        manifests.extend(self._workspace_member_manifests(config_target))
        manifests.extend(crate_path / "Cargo.toml" for crate_path in crate_paths)

        dependency_names: Set[str] = set()
        seen: Set[Path] = set()
        for manifest in manifests:
            manifest = manifest.resolve()
            if manifest in seen:
                continue
            seen.add(manifest)
            dependency_names.update(self._cargo_dependency_names(manifest))

        return dependency_names

    def _expand_generated_binding_dependencies(
        self, direct_names: Set[str], binding_dirs: Dict[str, Path]
    ) -> Set[str]:
        """Include generated binding crates required by selected bindings."""
        selected = set(direct_names)
        queue = list(direct_names)
        generated_names = set(binding_dirs.keys())

        while queue:
            pkg_name = queue.pop()
            cargo_toml = binding_dirs[pkg_name] / "Cargo.toml"
            for dep_name in self._cargo_dependency_names(cargo_toml) & generated_names:
                if dep_name not in selected:
                    selected.add(dep_name)
                    queue.append(dep_name)

        return selected

    def _filter_binding_dirs_for_target(
        self,
        config_target: Path,
        crate_paths: List[Path],
        binding_dirs: Dict[str, Path],
    ) -> Dict[str, Path]:
        """Keep generated binding crates referenced by this target Cargo graph."""
        generated_names = set(binding_dirs.keys())
        direct_names = (
            self._cargo_dependency_names_for_target(config_target, crate_paths) & generated_names
        )
        selected_names = self._expand_generated_binding_dependencies(direct_names, binding_dirs)
        return {name: binding_dirs[name] for name in sorted(selected_names)}

    def _write_cargo_configs(self, ros_packages: Dict[str, Path]):
        """Generate ``.cargo/config.toml`` for each Cargo workspace / standalone crate.

        Writes both ``[patch.crates-io]`` entries (for dependency resolution) and
        ``[build]`` rustflags (for linker search paths). This is the single config
        used by both ``cargo build`` and IDEs.
        """
        binding_dirs = self._collect_binding_dirs(ros_packages)
        if not binding_dirs:
            return

        targets = self._collect_ide_config_targets()
        if not targets:
            return

        rustflags = self._compute_rustflags()
        generated_count = 0

        for config_target, crate_paths in targets.items():
            target_binding_dirs = self._filter_binding_dirs_for_target(
                config_target, crate_paths, binding_dirs
            )
            patches = self._compute_relative_patches(config_target, target_binding_dirs)

            patch_marker_block = self._generate_marker_block(patches)
            build_marker_block = self._generate_build_marker_block(rustflags)

            config_dir = config_target / ".cargo"
            config_file = config_dir / "config.toml"

            # Read existing content (if any)
            existing_content = None
            if config_file.exists():
                existing_content = config_file.read_text()

            # Merge patches first, then build flags
            new_content = self._merge_into_config(existing_content, patch_marker_block)
            new_content = self._merge_build_into_config(new_content, build_marker_block)

            # Write the file
            config_dir.mkdir(parents=True, exist_ok=True)
            config_file.write_text(new_content)
            generated_count += 1

            crate_names = [p.name for p in crate_paths]
            logger.info(
                f"Wrote .cargo/config.toml with {len(patches)} patches "
                f"and {len(rustflags)} rustflags to {config_file} "
                f"(crates: {', '.join(crate_names)})"
            )

        if generated_count > 0:
            logger.info(
                f"Generated {generated_count} .cargo/config.toml file(s). "
                "Consider adding .cargo/config.toml to .gitignore (paths are machine-specific)."
            )


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
