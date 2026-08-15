# Licensed under the Apache License, Version 2.0

import os
import shutil
from pathlib import Path

from colcon_core.environment import create_environment_hooks, create_environment_scripts
from colcon_core.logging import colcon_logger
from colcon_core.plugin_system import satisfies_version
from colcon_core.shell import create_environment_hook
from colcon_core.task import TaskExtensionPoint, run

# Import Rust library directly via PyO3 bindings
from colcon_cargo_ros2 import cargo_ros2_py
from colcon_cargo_ros2._version import package_version
from colcon_cargo_ros2.workspace_bindgen import generate_workspace_bindings

logger = colcon_logger.getChild(__name__)


def find_cargo_executable():
    """Locate the cargo executable.

    :returns: Path to cargo, or None if it is not on PATH
    """
    return shutil.which("cargo")


def python_package_version(source_root=None):
    """Version of the Python code being executed; see :mod:`colcon_cargo_ros2._version`."""
    return package_version(source_root)


def check_version_skew(native_version, python_version):
    """Report a mismatch between the Python code and the native module it calls.

    The editable install layout makes this easy to hit: the ``.pth`` points at the
    source tree, so Python changes take effect immediately while
    ``cargo_ros2_py*.so`` stays whatever was last built. The symptom is a
    ``TypeError`` about an unexpected keyword argument from deep inside the build,
    which says nothing about rebuilding.

    :returns: A message describing the mismatch, or None when the versions agree
      or either is unknown (never fail a build over a version we cannot read)
    """
    if not native_version or not python_version:
        return None
    if native_version == python_version:
        return None
    return (
        f"\n\ncargo_ros2_py {native_version} does not match "
        f"colcon-cargo-ros2 {python_version}."
        "\n\nThe bundled native module is out of date with the Python code calling"
        " it. Rebuild it:"
        "\n  $ just build-python && just install\n"
    )


def detect_cargo_features(cargo_args):
    """Parse the feature selection out of cargo arguments.

    Mirrors what cargo itself accepts: ``--features``/``-F`` in both the
    space-separated and ``=`` forms, values split on commas or whitespace, and
    repeated flags accumulating.

    :param cargo_args: Arguments destined for cargo (may be None)
    :returns: (features, no_default_features, all_features)
    """
    features = []
    no_default_features = False
    all_features = False

    cargo_args = cargo_args or []
    index = 0
    while index < len(cargo_args):
        arg = cargo_args[index]

        if arg == "--no-default-features":
            no_default_features = True
        elif arg == "--all-features":
            all_features = True
        elif arg in ("--features", "-F"):
            # Value is the next argument, if there is one
            if index + 1 < len(cargo_args):
                features.extend(_split_feature_list(cargo_args[index + 1]))
                index += 1
        elif arg.startswith("--features=") or arg.startswith("-F="):
            features.extend(_split_feature_list(arg.split("=", 1)[1]))

        index += 1

    # Preserve order while dropping duplicates
    deduplicated = list(dict.fromkeys(features))

    return deduplicated, no_default_features, all_features


def _split_feature_list(value):
    """Split a --features value on commas and whitespace."""
    return [feature for feature in value.replace(",", " ").split() if feature]


def detect_cargo_target(cargo_args, env=None):
    """Determine the target triple a build was compiled for.

    ``--target`` wins over ``$CARGO_BUILD_TARGET``, matching cargo.

    Note that a ``[build] target`` entry in ``.cargo/config.toml`` is not
    consulted. We generate that file ourselves and never set ``target`` in it.

    :param cargo_args: Arguments destined for cargo (may be None)
    :param env: Environment mapping to read CARGO_BUILD_TARGET from
    :returns: The target triple, or None for a native build
    """
    cargo_args = cargo_args or []
    index = 0
    while index < len(cargo_args):
        arg = cargo_args[index]

        if arg == "--target":
            if index + 1 < len(cargo_args):
                return cargo_args[index + 1]
            return None
        if arg.startswith("--target="):
            return arg.split("=", 1)[1]

        index += 1

    if env is None:
        env = os.environ

    return env.get("CARGO_BUILD_TARGET") or None


class AmentCargoBuildTask(TaskExtensionPoint):
    """A build task for Rust ROS 2 packages using workspace-level binding generation.

    This task implements a two-phase approach:
    1. Workspace-level binding generation (done once before all builds)
    2. Per-package cargo build using .cargo/config.toml

    The workspace-level binding generation:
    - Discovers all ROS dependencies from ament_index and workspace
    - Generates ALL bindings to build/<pkg>/rosidl_cargo/
    - Writes .cargo/config.toml with [patch.crates-io] and [build] rustflags
    - Uses lock file to ensure only one process does generation

    Each package build then runs plain cargo build (patches and rustflags
    are picked up automatically from .cargo/config.toml).
    """

    def __init__(self):  # noqa: D107
        super().__init__()
        satisfies_version(TaskExtensionPoint.EXTENSION_POINT_VERSION, "^1.0")
        self._build_base = None  # Set during workspace binding generation (used by install)

    def add_arguments(self, *, parser):  # noqa: D102
        parser.add_argument(
            "--cargo-args",
            nargs="*",
            metavar="*",
            type=str.lstrip,
            help="Pass arguments to Cargo. "
            "Arguments matching other options must be prefixed by a space,\n"
            'e.g. --cargo-args " --help"',
        )
        parser.add_argument(
            "--rosidl-runtime-rs-version",
            type=str,
            default=None,
            help="Override rosidl_runtime_rs version in generated bindings (default: 0.6)",
        )
        parser.add_argument(
            "--no-rpath",
            action="store_true",
            help="Do not bake ROS library directories into built binaries as an rpath. "
            "Binaries then need LD_LIBRARY_PATH (i.e. a sourced ROS environment) to run.",
        )
        parser.add_argument(
            "--no-gitignore",
            action="store_true",
            help="Do not add the generated .cargo/config.toml to .gitignore",
        )

    async def build(self, *, additional_hooks=None):  # noqa: D102
        """Build the Rust ROS 2 package using workspace-level binding generation."""
        additional_hooks = [] if additional_hooks is None else additional_hooks

        # Step 1: Generate workspace-level bindings (done once for entire workspace)
        rc = await self._prepare_workspace_bindings()
        if rc:
            return rc

        # Step 2: Create environment hooks and scripts
        await self._create_environment_scripts(additional_hooks)

        # Step 3: Build this package with cargo
        args = self.context.args
        cmd = self._build_cmd(args.cargo_args if hasattr(args, "cargo_args") else [])

        # Execute cargo build from the package source directory so Cargo
        # discovers .cargo/config.toml (walks CWD upward to find it).
        pkg_dir = str(Path(self.context.pkg.path).resolve())
        result = await run(self.context, cmd, cwd=pkg_dir, env=None)
        if result and result.returncode != 0:
            return result.returncode

        # Step 4: Install binaries and create package markers
        rc = self._install_package()
        if rc:
            return rc

        # Return the exit code
        return 0

    async def _prepare_workspace_bindings(self):
        """Generate workspace-level ROS 2 bindings (done once for entire workspace)."""
        # Fail early and clearly if the Rust toolchain is missing, rather than
        # letting the build blow up part-way through
        if find_cargo_executable() is None:
            logger.error(
                "\n\nCould not find the 'cargo' executable on PATH."
                "\n\nRust ROS 2 packages are built with cargo. Install the Rust"
                " toolchain and make sure it is on PATH:"
                "\n  $ curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\n"
            )
            return 1

        # Check that cargo_ros2_py module is available
        try:
            # Quick check that the module loaded correctly
            native_version = cargo_ros2_py.__version__
            logger.debug(f"cargo_ros2_py {native_version} loaded")

            skew = check_version_skew(native_version, python_package_version())
            if skew:
                logger.error(skew)
                return 1
        except (ImportError, AttributeError) as e:
            logger.error(
                f"\n\ncargo_ros2_py Rust bindings not found: {e}"
                "\n\nPlease ensure colcon-cargo-ros2 is installed correctly:"
                "\n  $ pip install colcon-cargo-ros2\n"
            )
            return 1

        # Derive workspace paths from install_base
        args = self.context.args
        workspace_root = Path(os.path.abspath(os.path.join(args.install_base, "../..")))
        build_base = Path(os.path.abspath(os.path.join(args.build_base, "..")))
        install_base = Path(args.install_base).parent  # install/ directory

        # Store paths for use in build
        self._workspace_root = workspace_root
        self._build_base = build_base

        # Generate workspace-level bindings
        # This uses a lock file, so only the first package will actually generate
        # All other packages will see the lock and skip generation
        try:
            verbose = getattr(args, "verbose", False)
            generate_workspace_bindings(workspace_root, build_base, install_base, args, verbose)
        except Exception as e:
            logger.error(f"Workspace binding generation failed: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return 1

        return 0

    async def _create_environment_scripts(self, additional_hooks):
        """Create environment hooks and scripts for ROS 2 integration.

        This creates:
        1. Individual hook scripts (e.g., ament_prefix_path.sh)
        2. Package scripts that source all hooks (package.sh, package.bash, etc.)
        3. Ensures ROS 2 compliance so CMake packages can find our packages
        """
        args = self.context.args
        pkg = self.context.pkg

        # Create additional hooks (e.g., ament_prefix_path)
        additional_hooks.extend(
            create_environment_hook(
                "ament_prefix_path",
                Path(args.install_base),
                pkg.name,
                "AMENT_PREFIX_PATH",
                "",
                mode="prepend",
            )
        )

        # Create default environment hooks (PATH, PYTHONPATH, etc.) from environment extensions
        default_hooks = create_environment_hooks(args.install_base, pkg.name)

        # Create package scripts (package.sh, package.bash, etc.) that source all hooks
        # This is what makes our Rust packages compatible with CMake packages
        create_environment_scripts(
            pkg, args, default_hooks=default_hooks, additional_hooks=additional_hooks
        )

    def _build_cmd(self, cargo_args):
        """Build the cargo build command.

        Uses --manifest-path since cargo is invoked from workspace root.
        Patches and rustflags are picked up from .cargo/config.toml
        (generated during workspace binding generation).

        Adds --quiet flag by default to suppress cargo progress output (matching
        CMake/Python build behavior), unless verbose mode is enabled.
        """
        cmd = ["cargo", "build"]

        # Add --manifest-path to specify which package to build
        manifest_path = Path(self.context.pkg.path).resolve() / "Cargo.toml"
        cmd.extend(["--manifest-path", str(manifest_path)])

        # Handle None cargo_args
        if cargo_args is None:
            cargo_args = []

        # Add --quiet flag unless verbose mode is enabled or user explicitly passed --verbose
        # This suppresses "Compiling..." and "Finished..." messages but shows errors
        args = self.context.args
        verbose = getattr(args, "verbose", False)
        has_verbose_flag = "--verbose" in cargo_args or "-v" in cargo_args
        has_quiet_flag = "--quiet" in cargo_args or "-q" in cargo_args

        if not verbose and not has_verbose_flag and not has_quiet_flag:
            cmd.append("--quiet")

        # Add all cargo arguments
        cmd.extend(cargo_args)

        return cmd

    def _detect_cargo_profile(self, cargo_args, args):
        """Detect the cargo build profile from command-line arguments.

        Supports:
        - --release flag → "release"
        - --profile NAME → NAME (custom profile)
        - --profile=NAME → NAME (custom profile)
        - dev profile → "debug" (special case: dev outputs to target/debug/)
        - default → "debug"
        """
        # Check colcon-level --release flag first
        if hasattr(args, "release") and args.release:
            return "release"

        # Parse cargo arguments
        i = 0
        while i < len(cargo_args):
            arg = cargo_args[i]

            # Check for --release flag
            if arg == "--release":
                return "release"

            # Check for --profile=NAME syntax
            if arg.startswith("--profile="):
                profile_name = arg.split("=", 1)[1]
                # Special case: dev profile outputs to debug directory
                return "debug" if profile_name == "dev" else profile_name

            # Check for --profile NAME syntax (two separate args)
            if arg == "--profile" and i + 1 < len(cargo_args):
                profile_name = cargo_args[i + 1]
                # Special case: dev profile outputs to debug directory
                return "debug" if profile_name == "dev" else profile_name

            i += 1

        # Default to debug (dev profile)
        return "debug"

    def _install_package(self):
        """Install package binaries and create ament markers using direct API call."""
        args = self.context.args

        # Determine build profile from cargo arguments
        # Supports: --release, --profile NAME, --profile=NAME
        cargo_args = getattr(args, "cargo_args", []) or []
        profile = self._detect_cargo_profile(cargo_args, args)
        verbose = getattr(args, "verbose", False)

        # Cross-compiled builds put artifacts under an extra target-triple
        # directory, and feature-gated targets are only built when their
        # features were enabled
        arch = detect_cargo_target(cargo_args)
        features, no_default_features, all_features = detect_cargo_features(cargo_args)

        # Execute installation via direct API call
        try:
            # Create configuration for installation
            # Ensure project_root is an absolute path
            project_root = Path(self.context.pkg.path).resolve()

            config = cargo_ros2_py.InstallConfig(
                project_root=str(project_root),
                install_base=str(args.install_base),
                build_base=str(self._build_base),
                profile=profile,
                verbose=verbose,
                arch=arch,
                features=features,
                no_default_features=no_default_features,
                all_features=all_features,
            )

            # Call Rust function directly (no subprocess!)
            cargo_ros2_py.install_to_ament(config)

            logger.info("✓ Package installed successfully")
            return 0

        except RuntimeError as e:
            logger.error(f"Installation failed: {e}")
            return 1
