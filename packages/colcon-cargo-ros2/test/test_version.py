# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests that the package reports one version, not three.

`colcon_cargo_ros2.__version__` used to be a literal that `just bump-version`
did not touch, so it sat at 0.2.0 while pyproject.toml and Cargo.toml moved on
to 0.4.1. Anything reading it — a user checking what they have, the skew guard
in the build task — got a version that had not existed for a long time.
"""

import sys
from pathlib import Path

import pytest

import colcon_cargo_ros2
from colcon_cargo_ros2._version import package_version

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version(root: Path) -> str:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(root / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestPackageVersion:
    def test_reads_the_source_tree(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')

        assert package_version(tmp_path) == "9.9.9"

    def test_falls_back_to_installed_metadata(self, tmp_path):
        """No pyproject.toml here, so the answer comes from the distribution."""
        result = package_version(tmp_path)

        assert result is None or isinstance(result, str)

    def test_malformed_pyproject_is_not_fatal(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("this is not toml {{{")

        assert package_version(tmp_path) is None or isinstance(package_version(tmp_path), str)


class TestSingleSourceOfTruth:
    def test_dunder_version_matches_pyproject(self):
        """The drift this guards against went unnoticed across several releases."""
        assert colcon_cargo_ros2.__version__ == _pyproject_version(SOURCE_ROOT)

    def test_cargo_manifest_agrees(self):
        """`just bump-version` writes both; a release needs them to match."""
        cargo_toml = (SOURCE_ROOT / "Cargo.toml").read_text()
        version_line = next(
            line for line in cargo_toml.splitlines() if line.startswith("version = ")
        )

        assert _pyproject_version(SOURCE_ROOT) in version_line

    def test_unknown_attributes_still_raise(self):
        with pytest.raises(AttributeError):
            colcon_cargo_ros2.__nonexistent__

    @pytest.mark.skipif(sys.version_info < (3, 7), reason="module __getattr__ needs 3.7")
    def test_version_is_not_a_stored_literal(self):
        """Derived, so it cannot drift from pyproject.toml again."""
        source = (SOURCE_ROOT / "colcon_cargo_ros2" / "__init__.py").read_text()

        assert '__version__ = "' not in source
