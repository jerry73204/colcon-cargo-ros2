# Licensed under the Apache License, Version 2.0

"""Command-line access to the operations the colcon build task performs.

The wheel ships the PyO3 extension module and no binaries, so the `cargo ros2`
subcommands exist only for people who build this repository from source. This
console script gives everyone else the same operations, calling the same
functions the build task does.

    colcon-cargo-ros2 bindgen --package std_msgs --output build/bindings
    colcon-cargo-ros2 install --install-base install/my_node
    colcon-cargo-ros2 clean
    colcon-cargo-ros2 doctor

Normal use needs none of these: `colcon build` runs them. They are here for
generating bindings outside a colcon workspace, for inspecting what a build
would produce, and for diagnosing one that misbehaved.
"""

import argparse
import os
import sys

from colcon_cargo_ros2 import cargo_ros2_py


def _add_bindgen(subparsers):
    parser = subparsers.add_parser(
        "bindgen",
        help="generate Rust bindings for one ROS interface package",
        description=(
            "Generate Rust bindings for a ROS interface package. `colcon build` "
            "does this for every dependency it discovers; this is for generating "
            "one on its own."
        ),
    )
    parser.add_argument("--package", required=True, help="ROS package name")
    parser.add_argument("--output", required=True, help="directory to generate into")
    parser.add_argument(
        "--package-path",
        default=None,
        help="path to the package's share directory (default: ask the ament index)",
    )
    parser.add_argument(
        "--rosidl-runtime-rs-version",
        default=None,
        help=(
            "version the generated crate should depend on. It has to match what "
            "your rclrs pulls in; colcon build derives it from the workspace"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(func=_run_bindgen)


def _run_bindgen(args):
    config = cargo_ros2_py.BindgenConfig(
        package_name=args.package,
        output_dir=args.output,
        package_path=args.package_path,
        verbose=args.verbose,
        rosidl_runtime_rs_version=args.rosidl_runtime_rs_version,
    )
    cargo_ros2_py.generate_bindings(config)
    print(f"✓ Generated bindings for {args.package} in {args.output}")
    return 0


def _add_install(subparsers):
    parser = subparsers.add_parser(
        "install",
        help="install built artifacts into an ament layout",
        description=(
            "Copy binaries, libraries and [package.metadata.ros] entries into "
            "install/<pkg>/ and write the ament markers, as the build task does "
            "after cargo build."
        ),
    )
    parser.add_argument("--install-base", required=True, help="install/<package> directory")
    parser.add_argument(
        "--project-root", default=None, help="crate directory (default: current directory)"
    )
    parser.add_argument(
        "--build-base", default=None, help="colcon build directory (default: project root)"
    )
    parser.add_argument("--profile", default="debug", help="cargo profile the build used")
    parser.add_argument("--target", default=None, help="target triple, for a cross build")
    parser.add_argument("--features", default="", help="comma-separated features that were enabled")
    parser.add_argument("--no-default-features", action="store_true")
    parser.add_argument("--all-features", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(func=_run_install)


def _run_install(args):
    project_root = args.project_root or os.getcwd()
    config = cargo_ros2_py.InstallConfig(
        project_root=project_root,
        install_base=args.install_base,
        build_base=args.build_base or project_root,
        profile=args.profile,
        verbose=args.verbose,
        arch=args.target,
        features=[f for f in args.features.replace(",", " ").split() if f],
        no_default_features=args.no_default_features,
        all_features=args.all_features,
    )
    cargo_ros2_py.install_to_ament(config)
    print(f"✓ Installed to {args.install_base}")
    return 0


def _add_clean(subparsers):
    parser = subparsers.add_parser(
        "clean",
        help="remove generated bindings and cache",
        description="Remove the generated bindings and cache for a crate.",
    )
    parser.add_argument("--path", default=None, help="crate directory (default: current directory)")
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(func=_run_clean)


def _run_clean(args):
    cargo_ros2_py.clean_bindings(args.path or os.getcwd(), args.verbose)
    print("✓ Cleaned bindings and cache")
    return 0


def _add_doctor(subparsers):
    parser = subparsers.add_parser(
        "doctor",
        help="explain why a plain cargo invocation fails here",
        description=(
            "Walk the chain a cargo build depends on -- ROS environment, "
            "generated .cargo/config.toml, patched crates, binding freshness, "
            "package.xml declarations -- and print the fix for the first thing "
            "that is wrong."
        ),
    )
    parser.add_argument(
        "path", nargs="?", default=None, help="crate directory (default: current directory)"
    )
    parser.set_defaults(func=_run_doctor)


def _run_doctor(args):
    healthy = cargo_ros2_py.doctor(args.path)
    if healthy:
        print("✓ Workspace looks healthy")
    return 0 if healthy else 1


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="colcon-cargo-ros2",
        description=(
            "Operations the colcon build task performs, available on their own. "
            "Equivalent to the `cargo ros2` subcommands, which are only built "
            "from source."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_bindgen(subparsers)
    _add_install(subparsers)
    _add_clean(subparsers)
    _add_doctor(subparsers)
    return parser


def main(argv=None):
    """Run a subcommand; returns the process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_usage(sys.stderr)
        print("error: a subcommand is required", file=sys.stderr)
        return 2

    try:
        return args.func(args)
    except RuntimeError as e:
        # The PyO3 layer reports failures as RuntimeError. A traceback would
        # bury a message that already says what went wrong.
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
