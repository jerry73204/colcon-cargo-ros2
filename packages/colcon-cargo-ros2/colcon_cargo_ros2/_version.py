# Licensed under the Apache License, Version 2.0

"""Where the package's version comes from.

One resolution, used by ``colcon_cargo_ros2.__version__`` and by the build
task's skew check. It used to be a literal in ``__init__.py`` that
``just bump-version`` did not rewrite, so it reported 0.2.0 while the rest of
the project was on 0.4.1.
"""

from pathlib import Path

#: Directory holding pyproject.toml in a source checkout.
_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def package_version(source_root=None):
    """The version of the code being executed, or None if it cannot be read.

    Prefers ``pyproject.toml`` from the source tree over installed distribution
    metadata. Under an editable install the two disagree routinely -- the
    ``.pth`` runs the source tree while the recorded metadata is from whenever a
    wheel was last installed -- and it is the source tree that is paired with the
    native module built alongside it.

    :param source_root: Directory holding pyproject.toml; defaults to the
      package's own source root
    """
    root = Path(source_root) if source_root is not None else _SOURCE_ROOT

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib

            with open(pyproject, "rb") as f:
                version = tomllib.load(f).get("project", {}).get("version")
            if version:
                return version
        except Exception:
            # A malformed or unreadable manifest is not worth failing over;
            # installed metadata may still answer.
            pass

    try:
        from importlib.metadata import version as distribution_version

        return distribution_version("colcon-cargo-ros2")
    except Exception:
        return None
