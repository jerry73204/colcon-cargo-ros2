# Licensed under the Apache License, Version 2.0

"""Console script for the workspace diagnosis.

A direct name for the one subcommand people reach for while a build is failing.
``colcon-cargo-ros2 doctor`` does the same thing; both call
:func:`colcon_cargo_ros2.cli.main`.
"""

import sys


def main(argv=None):
    """Diagnose the crate in *argv*'s path, or the current directory."""
    from colcon_cargo_ros2.cli import main as cli_main

    return cli_main(["doctor", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
