# Licensed under the Apache License, Version 2.0

import importlib.util
from pathlib import Path

from colcon_core.logging import colcon_logger
from colcon_core.package_augmentation import PackageAugmentationExtensionPoint
from colcon_core.plugin_system import satisfies_version

logger = colcon_logger.getChild(__name__)

# Augmentation runs once per discovery pass, so the notice needs suppressing.
_REPORTED_COMPETITION = set()


def _competing_extension_installed() -> bool:
    """Whether colcon-ros-cargo is installed in this interpreter.

    It is the other colcon extension for ``ament_cargo`` packages, and having
    both installed is not a supported arrangement -- see
    :func:`_warn_about_competition`.
    """
    try:
        return importlib.util.find_spec("colcon_ros_cargo") is not None
    except (ImportError, ValueError):
        # A half-removed distribution can leave find_spec raising. Whatever it
        # is, it is not something to fail a build over.
        return False


def _warn_about_competition():
    """Say that another extension has taken the packages this one handles.

    Nothing else will say it. colcon-ros-cargo registers its package
    identification at priority 160 and colcon-ros registers at 150, so with both
    installed every ``ament_cargo`` package is typed ``ament_cargo`` rather than
    ``ros.ament_cargo``, and colcon dispatches it to colcon-ros-cargo's build
    task. This extension's task never runs, so no bindings are generated -- and
    the build still reports success, because ``cargo ament-build`` did its part.

    Both extensions also register ``--cargo-args``, which is where the
    ``argparse.ArgumentError: conflicting option string`` in the build output
    comes from.

    Package augmentation is the last place with a voice: colcon calls every
    augmentation extension for every descriptor, whatever its type.
    """
    if "colcon-ros-cargo" in _REPORTED_COMPETITION:
        return
    _REPORTED_COMPETITION.add("colcon-ros-cargo")

    logger.warning(
        "colcon-ros-cargo is installed alongside colcon-cargo-ros2. Both build "
        "ament_cargo packages, and colcon-ros-cargo takes them: its package "
        "identification runs at priority 160 against colcon-ros's 150, so every "
        "Rust package is dispatched to `cargo ament-build` and this extension's "
        "build task never runs.\n"
        "  No message bindings are generated, and the build still reports "
        "success -- a package that depends on ROS interfaces then fails to "
        "resolve them, or silently builds against whatever crates.io has.\n"
        "  Both extensions also register --cargo-args, which is the "
        "`argparse.ArgumentError: conflicting option string` above.\n"
        "  Keep one:\n"
        "    pip uninstall colcon-ros-cargo cargo-ament-build   # use this extension\n"
        "    pip uninstall colcon-cargo-ros2                    # use colcon-ros-cargo"
    )


class RustBindingAugmentation(PackageAugmentationExtensionPoint):
    """Generate workspace-level ROS 2 Rust bindings during package augmentation phase.

    This extension runs AFTER package discovery but BEFORE any build tasks start.
    It receives ALL discovered packages and generates bindings once for the entire workspace.

    This is the architecturally correct way to handle workspace-level operations in colcon,
    avoiding fragile directory scanning and respecting colcon's package selection flags.
    """

    PRIORITY = 90  # Run after most other augmentations

    def __init__(self):
        """Initialize the RustBindingAugmentation extension."""
        super().__init__()
        satisfies_version(PackageAugmentationExtensionPoint.EXTENSION_POINT_VERSION, "^1.0")
        self._bindings_generated = False

    def augment_packages(self, descs, *, additional_argument_names=None):
        """Collect all Cargo packages for dependency-aware binding generation.

        Args:
            descs: Collection of ALL package descriptors discovered by colcon
            additional_argument_names: Additional argument names (unused)
        """
        # Only collect packages once for the entire workspace
        if self._bindings_generated:
            return

        # Collect ALL Cargo packages (both application and interface packages)
        # We need to discover their ROS dependencies to know which bindings to generate
        cargo_descriptors = {}
        for desc in descs:
            pkg_path = Path(desc.path)

            # Check if package has a Cargo.toml file
            if (pkg_path / "Cargo.toml").exists():
                # Store the FULL descriptor (includes parsed dependencies from package.xml)
                cargo_descriptors[desc.name] = desc
                logger.debug(f"Found Cargo package: {desc.name} at {pkg_path}")

        if not cargo_descriptors:
            logger.debug("No Cargo packages found in workspace")
            return

        # Only now: a workspace with no Rust packages is nobody's conflict.
        if _competing_extension_installed():
            _warn_about_competition()

        logger.info(f"Discovered {len(cargo_descriptors)} Cargo packages via colcon")

        # Store Cargo package descriptors for dependency discovery during build phase
        # Each descriptor includes dependencies parsed from package.xml by Colcon
        # We'll use these to discover which ROS interface packages need bindings
        RustBindingAugmentation._cargo_descriptors = cargo_descriptors

        # Also store all descriptors for potential recursive dependency resolution
        RustBindingAugmentation._all_descriptors = set(descs)

        self._bindings_generated = True

        # Note: We don't call super().augment_packages() because we're doing
        # workspace-level operations, not per-package augmentation


# Class variables to share discovered packages with build tasks
RustBindingAugmentation._cargo_descriptors = {}
RustBindingAugmentation._all_descriptors = set()
