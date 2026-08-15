# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for Cargo.toml <-> package.xml dependency validation.

An interface package used in Cargo.toml but not declared in package.xml gets no
bindings, so cargo resolves the name against the real crates.io and dies with an
unrelated 'yanked'/version error. These tests cover the warning that names the
real cause, and the noise controls around it.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2 import workspace_bindgen
from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    """Each test starts with nothing already reported.

    The suppression that keeps a warning to one line per build is process-wide,
    so it would otherwise leak between tests.
    """
    workspace_bindgen._REPORTED_MISMATCHES.clear()
    yield
    workspace_bindgen._REPORTED_MISMATCHES.clear()


@pytest.fixture
def notes_log():
    """Collect info-level messages the module logs."""
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collector(level=logging.INFO)
    workspace_bindgen.logger.addHandler(handler)
    previous = workspace_bindgen.logger.level
    workspace_bindgen.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        workspace_bindgen.logger.setLevel(previous)
        workspace_bindgen.logger.removeHandler(handler)


@pytest.fixture
def warnings_log():
    """Collect warnings logged by the module.

    Not caplog: colcon installs its own handler on the ``colcon`` logger and
    stops propagation, so records never reach the root handler caplog uses.
    """
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collector(level=logging.WARNING)
    workspace_bindgen.logger.addHandler(handler)
    try:
        yield records
    finally:
        workspace_bindgen.logger.removeHandler(handler)


def _dep(name):
    d = MagicMock()
    d.name = name
    return d


def _cargo_package(tmp_path: Path, name: str, cargo_deps, xml_deps):
    """Create a crate on disk and the descriptor colcon would have produced."""
    crate = tmp_path / "src" / name
    crate.mkdir(parents=True, exist_ok=True)
    deps = "\n".join(f'{d} = "*"' for d in cargo_deps)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n[dependencies]\n{deps}\n'
    )

    desc = MagicMock()
    desc.name = name
    desc.path = str(crate)
    desc.get_dependencies.return_value = [_dep(d) for d in xml_deps]
    return desc


def _interface_share(tmp_path: Path, name: str) -> Path:
    """Create a share/ directory that looks like an installed interface package."""
    share = tmp_path / "opt" / name
    (share / "msg").mkdir(parents=True, exist_ok=True)
    (share / "msg" / "Thing.msg").write_text("int32 value\n")
    return share


def _validate(tmp_path, descriptors, interface_packages, monkeypatch, known_interfaces=()):
    """Run validation with *descriptors* installed on the augmentation."""
    monkeypatch.setattr(RustBindingAugmentation, "_cargo_descriptors", descriptors)
    monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", set())
    monkeypatch.setattr(
        workspace_bindgen.WorkspaceBindingGenerator,
        "_looks_like_interface_package",
        lambda _self, name: name in known_interfaces,
    )
    gen = _make_generator(tmp_path)
    gen._validate_cargo_dependencies(interface_packages)


# ---------------------------------------------------------------------------
# Cargo.toml deps missing from package.xml
# ---------------------------------------------------------------------------


class TestUndeclaredInterfaceDependency:
    def test_warns_when_interface_dep_missing_from_package_xml(
        self, tmp_path, monkeypatch, warnings_log
    ):
        """The case that produces cargo's misleading 'yanked' error."""
        desc = _cargo_package(
            tmp_path, "pkg_b", cargo_deps=["std_msgs", "sensor_msgs"], xml_deps=["std_msgs"]
        )
        _validate(
            tmp_path,
            {"pkg_b": desc},
            {"std_msgs": tmp_path / "share" / "std_msgs"},
            monkeypatch,
            known_interfaces={"sensor_msgs"},
        )

        text = "\n".join(warnings_log)
        assert "sensor_msgs" in text
        assert "pkg_b" in text
        assert "<depend>sensor_msgs</depend>" in text

    def test_no_warning_for_ordinary_crates_io_dependency(
        self, tmp_path, monkeypatch, warnings_log
    ):
        """An ordinary crate is not a ROS interface package, so it needs no <depend> tag."""
        desc = _cargo_package(
            tmp_path, "pkg_b", cargo_deps=["std_msgs", "serde"], xml_deps=["std_msgs"]
        )
        _validate(
            tmp_path,
            {"pkg_b": desc},
            {"std_msgs": tmp_path / "share" / "std_msgs"},
            monkeypatch,
            known_interfaces={"sensor_msgs"},
        )

        assert "serde" not in "\n".join(warnings_log)

    def test_no_warning_when_declared_in_both(self, tmp_path, monkeypatch, warnings_log):
        desc = _cargo_package(
            tmp_path,
            "pkg_b",
            cargo_deps=["std_msgs", "sensor_msgs"],
            xml_deps=["std_msgs", "sensor_msgs"],
        )
        _validate(
            tmp_path,
            {"pkg_b": desc},
            {"std_msgs": tmp_path / "share" / "std_msgs"},
            monkeypatch,
            known_interfaces={"sensor_msgs"},
        )

        assert warnings_log == []

    def test_warns_once_across_repeated_validation(self, tmp_path, monkeypatch, warnings_log):
        """Validation reruns for every package build task; the user reads it once."""
        desc = _cargo_package(tmp_path, "pkg_b", cargo_deps=["sensor_msgs"], xml_deps=["std_msgs"])
        for _ in range(3):
            _validate(
                tmp_path,
                {"pkg_b": desc},
                {"std_msgs": tmp_path / "share" / "std_msgs"},
                monkeypatch,
                known_interfaces={"sensor_msgs"},
            )

        assert len([msg for msg in warnings_log if "sensor_msgs" in msg]) == 1


# ---------------------------------------------------------------------------
# package.xml deps missing from Cargo.toml (pre-existing direction)
# ---------------------------------------------------------------------------


class TestDeclaredButUnusedDependency:
    """A package may declare an interface package it never compiles against.

    A launch file that starts a node publishing that type, or a dependency
    inherited for the ament environment, are both correct and neither shows up
    in Cargo.toml. Warning about them asks the user to delete a right answer, so
    this direction is reported as a note about the cost, not a problem.
    """

    def test_does_not_warn(self, tmp_path, monkeypatch, warnings_log):
        desc = _cargo_package(tmp_path, "pkg_c", cargo_deps=[], xml_deps=["geometry_msgs"])

        _validate(
            tmp_path,
            {"pkg_c": desc},
            {"geometry_msgs": tmp_path / "share" / "geometry_msgs"},
            monkeypatch,
        )

        assert warnings_log == []

    def test_is_noted_once(self, tmp_path, monkeypatch, notes_log):
        desc = _cargo_package(tmp_path, "pkg_c", cargo_deps=[], xml_deps=["geometry_msgs"])

        for _ in range(3):
            _validate(
                tmp_path,
                {"pkg_c": desc},
                {"geometry_msgs": tmp_path / "share" / "geometry_msgs"},
                monkeypatch,
            )

        mentions = [msg for msg in notes_log if "geometry_msgs" in msg]
        assert len(mentions) == 1
        # Phrased as what it costs, not as something done wrong.
        assert "bindings" in mentions[0].lower()

    def test_undeclared_direction_still_warns(self, tmp_path, monkeypatch, warnings_log):
        """The direction that breaks the build keeps its warning."""
        desc = _cargo_package(
            tmp_path, "pkg_d", cargo_deps=["sensor_msgs"], xml_deps=["geometry_msgs"]
        )

        _validate(
            tmp_path,
            {"pkg_d": desc},
            {"geometry_msgs": tmp_path / "share" / "geometry_msgs"},
            monkeypatch,
            known_interfaces={"sensor_msgs"},
        )

        assert any("sensor_msgs" in msg for msg in warnings_log)


# ---------------------------------------------------------------------------
# _looks_like_interface_package
# ---------------------------------------------------------------------------


class TestLooksLikeInterfacePackage:
    def test_installed_interface_package(self, tmp_path, monkeypatch):
        share = _interface_share(tmp_path, "sensor_msgs")
        monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", set())
        monkeypatch.setattr(
            workspace_bindgen,
            "_package_share_directory",
            lambda name: share if name == "sensor_msgs" else None,
        )
        gen = _make_generator(tmp_path)
        assert gen._looks_like_interface_package("sensor_msgs") is True

    def test_installed_package_without_interfaces(self, tmp_path, monkeypatch):
        share = tmp_path / "opt" / "rclcpp"
        share.mkdir(parents=True)
        monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", set())
        monkeypatch.setattr(workspace_bindgen, "_package_share_directory", lambda name: share)
        gen = _make_generator(tmp_path)
        assert gen._looks_like_interface_package("rclcpp") is False

    def test_unknown_package(self, tmp_path, monkeypatch):
        monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", set())
        monkeypatch.setattr(workspace_bindgen, "_package_share_directory", lambda name: None)
        gen = _make_generator(tmp_path)
        assert gen._looks_like_interface_package("serde") is False

    def test_workspace_source_package_not_yet_installed(self, tmp_path, monkeypatch):
        """A workspace-local msgs package is only visible in the source tree."""
        src = tmp_path / "src" / "my_msgs"
        (src / "msg").mkdir(parents=True)
        desc = MagicMock()
        desc.name = "my_msgs"
        desc.path = str(src)
        monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", {desc})
        monkeypatch.setattr(workspace_bindgen, "_package_share_directory", lambda name: None)
        gen = _make_generator(tmp_path)
        assert gen._looks_like_interface_package("my_msgs") is True
