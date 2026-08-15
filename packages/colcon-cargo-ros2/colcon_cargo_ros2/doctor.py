# Licensed under the Apache License, Version 2.0

"""Command-line entry point for the workspace diagnosis.

The wheel ships the PyO3 extension module and no binaries, so ``cargo ros2
doctor`` is only available to people who built this repository. This wrapper
gives everyone else the same checks under a console script.
"""

import argparse
import sys

from colcon_cargo_ros2 import cargo_ros2_py


def main(argv=None):
    """Diagnose the crate in *argv*'s path, or the current directory."""
    parser = argparse.ArgumentParser(
        prog="colcon-cargo-ros2-doctor",
        description=(
            "Explain why a plain cargo invocation fails in this workspace: "
            "ROS environment, generated .cargo/config.toml, patched crates, "
            "binding freshness, and package.xml declarations."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="crate directory to diagnose (default: current directory)",
    )
    args = parser.parse_args(argv)

    healthy = cargo_ros2_py.doctor(args.path)
    if healthy:
        print("✓ Workspace looks healthy")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
