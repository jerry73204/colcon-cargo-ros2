# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for choosing the rosidl_runtime_rs version generated crates depend on.

The version is not ours to pick. `rclrs` 0.6 depends on `rosidl_runtime_rs` 0.5
and `rclrs` 0.7 on 0.6, and cargo treats those as incompatible: generate the
wrong one and the graph carries both, after which the `Message` trait a
generated crate implements is not the `Message` trait rclrs requires.

    error[E0277]: the trait bound `std_msgs::msg::String: MessageIDL` is not satisfied
    note: there are multiple different versions of crate `rosidl_runtime_rs`
          in the dependency graph

So it is derived from what the workspace's own packages already declare.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2 import workspace_bindgen
from colcon_cargo_ros2.workspace_bindgen import RCLRS_RUNTIME_VERSIONS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _crate(tmp_path: Path, name: str, dependencies: str):
    """Create a Cargo package and the descriptor colcon would have produced."""
    crate = tmp_path / "src" / name
    crate.mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\n\n[dependencies]\n{dependencies}\n'
    )
    desc = MagicMock()
    desc.name = name
    desc.path = str(crate)
    return desc


def _detect(tmp_path, monkeypatch, descriptors, override=None):
    from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

    monkeypatch.setattr(
        RustBindingAugmentation,
        "_cargo_descriptors",
        {desc.name: desc for desc in descriptors},
    )
    gen = _make_generator(tmp_path)
    gen.args.rosidl_runtime_rs_version = override
    return gen._detect_runtime_version()


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    workspace_bindgen._REPORTED_MISMATCHES.clear()
    yield
    workspace_bindgen._REPORTED_MISMATCHES.clear()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetectRuntimeVersion:
    def test_explicit_declaration_wins(self, tmp_path, monkeypatch):
        desc = _crate(tmp_path, "node", 'rclrs = "0.7"\nrosidl_runtime_rs = "0.5"\n')

        assert _detect(tmp_path, monkeypatch, [desc]) == "0.5"

    def test_derived_from_rclrs(self, tmp_path, monkeypatch):
        """A workspace that only names rclrs still gets a matching runtime."""
        desc = _crate(tmp_path, "node", 'rclrs = "0.6"\n')

        assert _detect(tmp_path, monkeypatch, [desc]) == RCLRS_RUNTIME_VERSIONS["0.6"]

    def test_newer_rclrs_maps_to_newer_runtime(self, tmp_path, monkeypatch):
        desc = _crate(tmp_path, "node", 'rclrs = "0.7"\n')

        assert _detect(tmp_path, monkeypatch, [desc]) == RCLRS_RUNTIME_VERSIONS["0.7"]

    def test_no_declaration_means_no_opinion(self, tmp_path, monkeypatch):
        """Nothing to go on: the generator keeps its own default."""
        desc = _crate(tmp_path, "node", 'std_msgs = "*"\n')

        assert _detect(tmp_path, monkeypatch, [desc]) is None

    def test_unbounded_rclrs_requirement_is_reported(self, tmp_path, monkeypatch):
        """`rclrs = "*"` resolves to whatever is newest, so nothing can match it."""
        desc = _crate(tmp_path, "loose_node", 'rclrs = "*"\n')
        warnings = []
        monkeypatch.setattr(workspace_bindgen.logger, "warning", warnings.append)

        assert _detect(tmp_path, monkeypatch, [desc]) is None

        text = "\n".join(warnings)
        assert "loose_node" in text
        assert "pin a version" in text

    def test_unknown_rclrs_version_means_no_opinion(self, tmp_path, monkeypatch):
        desc = _crate(tmp_path, "node", 'rclrs = "9.9"\n')

        assert _detect(tmp_path, monkeypatch, [desc]) is None

    def test_agreeing_packages(self, tmp_path, monkeypatch):
        a = _crate(tmp_path, "a", 'rclrs = "0.6"\n')
        b = _crate(tmp_path, "b", 'rosidl_runtime_rs = "0.5"\n')

        assert _detect(tmp_path, monkeypatch, [a, b]) == "0.5"

    def test_cli_override_wins(self, tmp_path, monkeypatch):
        desc = _crate(tmp_path, "node", 'rclrs = "0.6"\n')

        assert _detect(tmp_path, monkeypatch, [desc], override="0.6") == "0.6"


class TestConflictingRequirements:
    def test_conflict_is_reported_and_highest_chosen(self, tmp_path, monkeypatch):
        """One shared binding set cannot satisfy two incompatible requirements.

        Saying so beats letting cargo fail later with a trait mismatch that names
        neither package.
        """
        old = _crate(tmp_path, "old_node", 'rclrs = "0.6"\n')
        new = _crate(tmp_path, "new_node", 'rclrs = "0.7"\n')

        warnings = []
        monkeypatch.setattr(workspace_bindgen.logger, "warning", warnings.append)

        chosen = _detect(tmp_path, monkeypatch, [old, new])

        text = "\n".join(warnings)
        assert "old_node" in text and "new_node" in text
        assert "0.5" in text and "0.6" in text
        assert "--rosidl-runtime-rs-version" in text
        assert chosen == "0.6"

    def test_conflict_is_reported_once(self, tmp_path, monkeypatch):
        old = _crate(tmp_path, "old_node", 'rclrs = "0.6"\n')
        new = _crate(tmp_path, "new_node", 'rclrs = "0.7"\n')

        warnings = []
        monkeypatch.setattr(workspace_bindgen.logger, "warning", warnings.append)

        for _ in range(3):
            _detect(tmp_path, monkeypatch, [old, new])

        assert len([w for w in warnings if "rosidl_runtime_rs" in w]) == 1
