# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for which version a generated binding crate is stamped with.

Generated crates carry a fixed ``0.0.0`` so that a committed ``Cargo.lock``
stops recording which ROS installation produced them. That only holds while
every consumer writes ``std_msgs = "*"``: a ``[patch.crates-io]`` entry
redirects where a crate comes from, but cargo still checks it against the
requirement, and ``0.0.0`` satisfies none of them::

    error: failed to select a version for the requirement
           `rclrs_example_msgs = "^0.5"`
    candidate versions found which didn't match: 0.0.0

So a package someone requires by version is stamped with the ROS package
version instead, and only that package.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation
from colcon_cargo_ros2.workspace_bindgen import WorkspaceBindingGenerator


def _crate(tmp_path: Path, name: str, body: str) -> MagicMock:
    """Write a crate whose manifest body is *body*, and its colcon descriptor."""
    crate = tmp_path / "src" / name
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n{body}')

    desc = MagicMock()
    desc.name = name
    desc.path = str(crate)
    return desc


def _pinned(tmp_path, descriptors, monkeypatch):
    monkeypatch.setattr(RustBindingAugmentation, "_cargo_descriptors", descriptors)
    return _make_generator(tmp_path)._detect_pinned_packages()


# ---------------------------------------------------------------------------
# _cargo_version_requirements
# ---------------------------------------------------------------------------


class TestCargoVersionRequirements:
    def test_plain_string_requirement(self, tmp_path):
        desc = _crate(tmp_path, "consumer", '[dependencies]\nstd_msgs = "5.3"\n')
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            Path(desc.path) / "Cargo.toml"
        ) == {"std_msgs": "5.3"}

    def test_table_requirement(self, tmp_path):
        desc = _crate(
            tmp_path,
            "consumer",
            '[dependencies]\nstd_msgs = { version = "5.3", features = ["serde"] }\n',
        )
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            Path(desc.path) / "Cargo.toml"
        ) == {"std_msgs": "5.3"}

    @pytest.mark.parametrize("requirement", ['"*"', '""', '{ version = "*" }'])
    def test_unbounded_is_not_a_requirement(self, tmp_path, requirement):
        """``*`` is the case the fixed version exists for."""
        desc = _crate(tmp_path, "consumer", f"[dependencies]\nstd_msgs = {requirement}\n")
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            Path(desc.path) / "Cargo.toml"
        ) == {}

    def test_rename_reports_the_package_name(self, tmp_path):
        """The key is the rename; the crate that gets generated is the package."""
        desc = _crate(
            tmp_path,
            "consumer",
            '[dependencies]\nmsgs = { package = "sensor_msgs", version = "4.2" }\n',
        )
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            Path(desc.path) / "Cargo.toml"
        ) == {"sensor_msgs": "4.2"}

    def test_platform_table(self, tmp_path):
        desc = _crate(
            tmp_path,
            "consumer",
            '[target.\'cfg(unix)\'.dependencies]\nstd_msgs = "5.3"\n',
        )
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            Path(desc.path) / "Cargo.toml"
        ) == {"std_msgs": "5.3"}

    def test_path_and_git_sources_are_skipped(self, tmp_path):
        """[patch.crates-io] never redirects these, so no version of ours applies."""
        desc = _crate(
            tmp_path,
            "consumer",
            "[dependencies]\n"
            'std_msgs = { path = "../std_msgs", version = "5.3" }\n'
            'geometry_msgs = { git = "https://example.invalid/x", version = "4.2" }\n',
        )
        assert (
            WorkspaceBindingGenerator._cargo_version_requirements(Path(desc.path) / "Cargo.toml")
            == {}
        )

    def test_workspace_inherited_requirement(self, tmp_path):
        """``std_msgs = { workspace = true }`` keeps its version in the root."""
        root = tmp_path / "src" / "ws"
        member = root / "member"
        member.mkdir(parents=True)
        (root / "Cargo.toml").write_text(
            "[workspace]\nmembers = [\"member\"]\n\n"
            '[workspace.dependencies]\nstd_msgs = "5.3"\n'
        )
        (member / "Cargo.toml").write_text(
            '[package]\nname = "member"\nversion = "0.1.0"\n\n'
            "[dependencies]\nstd_msgs = { workspace = true }\n"
        )
        assert WorkspaceBindingGenerator._cargo_version_requirements(
            member / "Cargo.toml", root
        ) == {"std_msgs": "5.3"}

    def test_unreadable_manifest(self, tmp_path):
        assert (
            WorkspaceBindingGenerator._cargo_version_requirements(tmp_path / "absent" / "C.toml")
            == {}
        )


# ---------------------------------------------------------------------------
# _detect_pinned_packages
# ---------------------------------------------------------------------------


class TestDetectPinnedPackages:
    def test_workspace_of_unbounded_requirements_pins_nothing(self, tmp_path, monkeypatch):
        """The documented form, and the one that keeps a reproducible lock."""
        descriptors = {
            "a": _crate(tmp_path, "a", '[dependencies]\nstd_msgs = "*"\n'),
            "b": _crate(tmp_path, "b", '[dependencies]\ngeometry_msgs = "*"\n'),
        }
        assert _pinned(tmp_path, descriptors, monkeypatch) == set()

    def test_one_pinning_package_is_enough(self, tmp_path, monkeypatch):
        """Bindings are shared, so one consumer's requirement decides the crate."""
        descriptors = {
            "a": _crate(tmp_path, "a", '[dependencies]\nstd_msgs = "*"\n'),
            "b": _crate(tmp_path, "b", '[dependencies]\nstd_msgs = "5.3"\n'),
        }
        assert "std_msgs" in _pinned(tmp_path, descriptors, monkeypatch)

    def test_only_the_pinned_package(self, tmp_path, monkeypatch):
        """A pin on one crate must not push the rest off the fixed version."""
        descriptors = {
            "a": _crate(
                tmp_path,
                "a",
                '[dependencies]\nstd_msgs = "5.3"\ngeometry_msgs = "*"\n',
            ),
        }
        pinned = _pinned(tmp_path, descriptors, monkeypatch)
        assert "std_msgs" in pinned
        assert "geometry_msgs" not in pinned

    def test_missing_manifest_is_not_an_error(self, tmp_path, monkeypatch):
        desc = MagicMock()
        desc.name = "a"
        desc.path = str(tmp_path / "src" / "gone")
        assert _pinned(tmp_path, {"a": desc}, monkeypatch) == set()

    def test_resolved_once(self, tmp_path, monkeypatch):
        """Called per interface package during generation; reading manifests once."""
        descriptors = {"a": _crate(tmp_path, "a", '[dependencies]\nstd_msgs = "5.3"\n')}
        monkeypatch.setattr(RustBindingAugmentation, "_cargo_descriptors", descriptors)
        gen = _make_generator(tmp_path)
        first = gen._detect_pinned_packages()
        (Path(descriptors["a"].path) / "Cargo.toml").unlink()
        assert gen._detect_pinned_packages() is first


# ---------------------------------------------------------------------------
# The freshness stamp
# ---------------------------------------------------------------------------


class TestStampCoversTheCrateVersion:
    @staticmethod
    def _share(tmp_path: Path) -> Path:
        share = tmp_path / "opt" / "std_msgs"
        (share / "msg").mkdir(parents=True)
        (share / "msg" / "Thing.msg").write_text("int32 value\n")
        return share

    def test_changing_the_requirement_restamps(self, tmp_path):
        """A consumer editing "*" to "5.3" touches no .msg, but the crate changes."""
        share = self._share(tmp_path)
        unpinned = WorkspaceBindingGenerator._interface_stamp(share, "0.6", False)
        pinned = WorkspaceBindingGenerator._interface_stamp(share, "0.6", True)
        assert unpinned != pinned

    def test_the_unpinned_stamp_is_unchanged(self, tmp_path):
        """Upgrading must not regenerate every binding in every `*` workspace."""
        share = self._share(tmp_path)
        assert WorkspaceBindingGenerator._interface_stamp(
            share, "0.6"
        ) == WorkspaceBindingGenerator._interface_stamp(share, "0.6", False)
