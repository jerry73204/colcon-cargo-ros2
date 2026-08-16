# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for noticing colcon-ros-cargo installed alongside this extension.

Both claim ``ament_cargo`` packages and colcon-ros-cargo wins: its package
identification registers at priority 160 against colcon-ros's 150, so every such
package is typed ``ament_cargo`` and built by ``cargo ament-build``. This
extension's build task never runs, no bindings are generated, and the only thing
the user sees is an ``argparse.ArgumentError`` about ``--cargo-args`` -- both
extensions register that option -- followed by a build that succeeds and
produces nothing.

Package augmentation still runs, because colcon calls every augmentation
extension for every descriptor regardless of type. That is the one place left to
say something.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from colcon_cargo_ros2 import package_augmentation
from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation


@pytest.fixture(autouse=True)
def _fresh_state():
    package_augmentation._REPORTED_COMPETITION.clear()
    yield
    package_augmentation._REPORTED_COMPETITION.clear()


@pytest.fixture
def warnings_log():
    """Collect warnings; colcon's logger does not propagate to caplog."""
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collector(level=logging.WARNING)
    package_augmentation.logger.addHandler(handler)
    try:
        yield records
    finally:
        package_augmentation.logger.removeHandler(handler)


def _cargo_package(tmp_path: Path, name: str = "pkg"):
    crate = tmp_path / name
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n')
    desc = MagicMock()
    desc.name = name
    desc.path = str(crate)
    return desc


def _augment(tmp_path, monkeypatch, *, competitor_installed, descs=None):
    monkeypatch.setattr(
        package_augmentation,
        "_competing_extension_installed",
        lambda: competitor_installed,
    )
    extension = RustBindingAugmentation()
    extension.augment_packages(descs if descs is not None else [_cargo_package(tmp_path)])


class TestCompetingExtensionWarning:
    def test_warns_when_colcon_ros_cargo_is_installed(self, tmp_path, monkeypatch, warnings_log):
        _augment(tmp_path, monkeypatch, competitor_installed=True)

        text = "\n".join(warnings_log)
        assert "colcon-ros-cargo" in text
        assert "160" in text and "150" in text, "say why it wins, not just that it does"
        assert "no bindings" in text.lower() or "never runs" in text
        assert "pip uninstall" in text, "the message has to carry the way out"

    def test_silent_when_it_is_not_installed(self, tmp_path, monkeypatch, warnings_log):
        _augment(tmp_path, monkeypatch, competitor_installed=False)

        assert not warnings_log, warnings_log

    def test_silent_when_the_workspace_has_no_cargo_packages(
        self, tmp_path, monkeypatch, warnings_log
    ):
        """A Python-only workspace is nobody's conflict."""
        plain = MagicMock()
        plain.name = "python_pkg"
        plain.path = str(tmp_path / "python_pkg")
        (tmp_path / "python_pkg").mkdir()

        _augment(tmp_path, monkeypatch, competitor_installed=True, descs=[plain])

        assert not warnings_log, warnings_log

    def test_reported_once_per_process(self, tmp_path, monkeypatch, warnings_log):
        """Augmentation runs per discovery pass; the warning is not a per-pass tax."""
        for _ in range(3):
            _augment(tmp_path, monkeypatch, competitor_installed=True)

        assert len(warnings_log) == 1, warnings_log


class TestDetection:
    def test_detects_an_importable_colcon_ros_cargo(self, monkeypatch):
        monkeypatch.setattr(
            package_augmentation.importlib.util,
            "find_spec",
            lambda name: object() if name == "colcon_ros_cargo" else None,
        )
        assert package_augmentation._competing_extension_installed() is True

    def test_absent_when_not_importable(self, monkeypatch):
        monkeypatch.setattr(package_augmentation.importlib.util, "find_spec", lambda name: None)
        assert package_augmentation._competing_extension_installed() is False

    def test_a_broken_install_is_not_a_crash(self, monkeypatch):
        """find_spec raises for a half-removed distribution; that is not our problem."""

        def explode(name):
            raise ValueError("bad module state")

        monkeypatch.setattr(package_augmentation.importlib.util, "find_spec", explode)
        assert package_augmentation._competing_extension_installed() is False
