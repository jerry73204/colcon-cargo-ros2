# Licensed under the Apache License, Version 2.0

"""Build Rust ROS 2 packages with colcon, generating message bindings."""


def __getattr__(name):
    """Resolve ``__version__`` on access.

    Derived rather than stored: a literal here is a second place to bump, and
    the one that gets forgotten. It sat at 0.2.0 for several releases while
    pyproject.toml and Cargo.toml moved on.
    """
    if name == "__version__":
        from colcon_cargo_ros2._version import package_version

        return package_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
