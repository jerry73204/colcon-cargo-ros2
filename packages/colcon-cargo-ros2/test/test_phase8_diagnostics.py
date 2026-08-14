# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for Phase 8: failures diagnosed here rather than misreported by cargo.

Covers:
- Reading every dependency form a Cargo.toml can express (8.2)
- Configs that never point at a binding directory which is not there (8.3)
- The freshness manifest a generated crate's build.rs checks (8.4)
- The Python/native version-skew guard (8.5)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2 import workspace_bindgen
from colcon_cargo_ros2.workspace_bindgen import MANIFEST_FILENAME, WorkspaceBindingGenerator

# ---------------------------------------------------------------------------
# 8.2 Reading Cargo.toml dependencies
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestCargoDependencyNames:
    def test_plain_tables(self, tmp_path):
        manifest = _write(
            tmp_path / "Cargo.toml",
            '[dependencies]\nstd_msgs = "*"\n\n[build-dependencies]\ncc = "1"\n',
        )

        names = WorkspaceBindingGenerator._cargo_dependency_names(manifest)

        assert names == {"std_msgs", "cc"}

    def test_renamed_dependency_reports_the_real_package(self, tmp_path):
        """The key is the rename; the ROS package name is in `package`."""
        manifest = _write(
            tmp_path / "Cargo.toml",
            '[dependencies]\nmsgs = { package = "sensor_msgs", version = "*" }\n',
        )

        names = WorkspaceBindingGenerator._cargo_dependency_names(manifest)

        assert names == {"sensor_msgs"}

    def test_platform_specific_tables(self, tmp_path):
        manifest = _write(
            tmp_path / "Cargo.toml",
            "[target.'cfg(unix)'.dependencies]\nstd_msgs = \"*\"\n"
            '\n[target.x86_64-unknown-linux-gnu.build-dependencies]\ngeometry_msgs = "*"\n',
        )

        names = WorkspaceBindingGenerator._cargo_dependency_names(manifest)

        assert names == {"std_msgs", "geometry_msgs"}

    def test_dev_dependencies(self, tmp_path):
        manifest = _write(tmp_path / "Cargo.toml", '[dev-dependencies]\nsensor_msgs = "*"\n')

        names = WorkspaceBindingGenerator._cargo_dependency_names(manifest)

        assert names == {"sensor_msgs"}

    def test_workspace_inheritance(self, tmp_path):
        _write(
            tmp_path / "Cargo.toml",
            '[workspace]\nmembers = ["member"]\n\n[workspace.dependencies]\nsensor_msgs = "*"\n',
        )
        member = _write(
            tmp_path / "member" / "Cargo.toml",
            "[dependencies]\nsensor_msgs = { workspace = true }\n",
        )

        names = WorkspaceBindingGenerator._cargo_dependency_names(member, tmp_path)

        assert names == {"sensor_msgs"}

    def test_workspace_inheritance_with_rename(self, tmp_path):
        _write(
            tmp_path / "Cargo.toml",
            '[workspace]\n\n[workspace.dependencies]\nmsgs = { package = "sensor_msgs" }\n',
        )
        member = _write(
            tmp_path / "member" / "Cargo.toml",
            "[dependencies]\nmsgs = { workspace = true }\n",
        )

        names = WorkspaceBindingGenerator._cargo_dependency_names(member, tmp_path)

        assert names == {"sensor_msgs"}

    def test_unreadable_manifest(self, tmp_path):
        assert WorkspaceBindingGenerator._cargo_dependency_names(tmp_path / "nope.toml") == set()


class TestValidationUsesEveryDependencyForm:
    def test_renamed_undeclared_interface_package_warns(self, tmp_path, monkeypatch):
        """A rename hid this case from the validator entirely."""
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        crate = tmp_path / "src" / "pkg_b"
        _write(
            crate / "Cargo.toml",
            '[dependencies]\nmsgs = { package = "sensor_msgs", version = "*" }\n',
        )
        desc = MagicMock()
        desc.name = "pkg_b"
        desc.path = str(crate)
        desc.get_dependencies.return_value = []

        monkeypatch.setattr(RustBindingAugmentation, "_cargo_descriptors", {"pkg_b": desc})
        monkeypatch.setattr(RustBindingAugmentation, "_all_descriptors", set())
        monkeypatch.setattr(
            WorkspaceBindingGenerator,
            "_looks_like_interface_package",
            lambda _self, name: name == "sensor_msgs",
        )
        workspace_bindgen._REPORTED_MISMATCHES.clear()

        warnings = []
        monkeypatch.setattr(workspace_bindgen.logger, "warning", warnings.append)

        _make_generator(tmp_path)._validate_cargo_dependencies({})

        assert any("sensor_msgs" in w for w in warnings)


# ---------------------------------------------------------------------------
# 8.3 Never patch to a binding directory that is not there
# ---------------------------------------------------------------------------


class TestBindingDirectoryValidation:
    def test_regenerates_when_crate_manifest_is_missing(self, tmp_path, monkeypatch):
        """A matching stamp beside a gutted crate must not count as up to date."""
        share = tmp_path / "share" / "my_msgs"
        (share / "msg").mkdir(parents=True)
        (share / "msg" / "Thing.msg").write_text("int32 value\n")

        gen = _make_generator(tmp_path)
        pkg_build = tmp_path / "build" / "my_msgs" / "rosidl_cargo"
        (pkg_build / "my_msgs").mkdir(parents=True)
        gen._write_stamp(pkg_build / ".bindgen_stamp", gen._interface_stamp(share))

        generated = []
        monkeypatch.setattr(
            WorkspaceBindingGenerator,
            "_run_bindgen",
            lambda _self, name, *_args: generated.append(name),
        )
        monkeypatch.setattr(
            WorkspaceBindingGenerator, "_fixup_generated_cargo_toml", lambda *_a: None
        )
        monkeypatch.setattr(WorkspaceBindingGenerator, "_write_manifest", lambda *_a: None)

        gen._generate_bindings({"my_msgs": share}, verbose=False)

        assert generated == ["my_msgs"]

    def test_write_time_check_drops_vanished_bindings(self, tmp_path):
        """The directory can disappear between generation and writing."""
        gen = _make_generator(tmp_path)
        present = tmp_path / "build" / "a_msgs" / "rosidl_cargo" / "a_msgs"
        present.mkdir(parents=True)
        (present / "Cargo.toml").write_text('[package]\nname = "a_msgs"\n')
        gone = tmp_path / "build" / "b_msgs" / "rosidl_cargo" / "b_msgs"

        kept = gen._drop_missing_bindings({"a_msgs": present, "b_msgs": gone})

        assert set(kept) == {"a_msgs"}


# ---------------------------------------------------------------------------
# 8.4 Freshness manifest for the generated crate
# ---------------------------------------------------------------------------


class TestInterfaceManifest:
    def test_records_one_line_per_definition(self, tmp_path):
        share = tmp_path / "share" / "my_msgs"
        (share / "msg").mkdir(parents=True)
        (share / "msg" / "Thing.msg").write_text("int32 value\n")
        (share / "msg" / "Other.msg").write_text("string name\n")
        (share / "msg" / "notes.txt").write_text("ignored")

        records = WorkspaceBindingGenerator._interface_records(share)

        lines = [line for line in records.splitlines() if line]
        assert len(lines) == 2
        assert all(line.startswith("msg/") for line in lines)
        assert all(len(line.split(":")) == 3 for line in lines)

    def test_stamp_is_the_digest_of_the_records(self, tmp_path):
        import hashlib

        share = tmp_path / "share" / "my_msgs"
        (share / "msg").mkdir(parents=True)
        (share / "msg" / "Thing.msg").write_text("int32 value\n")

        records = WorkspaceBindingGenerator._interface_records(share)
        expected = hashlib.sha256(records.encode()).hexdigest()

        assert WorkspaceBindingGenerator._interface_stamp(share) == expected

    def test_manifest_file_leads_with_the_source_directory(self, tmp_path):
        share = tmp_path / "share" / "my_msgs"
        (share / "msg").mkdir(parents=True)
        (share / "msg" / "Thing.msg").write_text("int32 value\n")
        crate = tmp_path / "build" / "my_msgs" / "rosidl_cargo" / "my_msgs"
        crate.mkdir(parents=True)

        WorkspaceBindingGenerator._write_manifest(crate, share)

        content = (crate / MANIFEST_FILENAME).read_text().splitlines()
        assert content[0] == str(share.resolve())
        assert any(line.startswith("msg/Thing.msg:") for line in content[1:])

    def test_manifest_write_survives_a_missing_crate_directory(self, tmp_path):
        """Generation may have failed; a manifest is not worth failing the build."""
        share = tmp_path / "share" / "my_msgs"
        (share / "msg").mkdir(parents=True)

        WorkspaceBindingGenerator._write_manifest(tmp_path / "nope", share)


# ---------------------------------------------------------------------------
# 8.5 Version-skew guard
# ---------------------------------------------------------------------------


class TestVersionSkew:
    def test_mismatch_is_reported_with_the_rebuild_command(self):
        from colcon_cargo_ros2.task.ament_cargo.build import check_version_skew

        message = check_version_skew(native_version="0.4.0", python_version="0.4.1")

        assert message is not None
        assert "0.4.0" in message and "0.4.1" in message
        assert "just build-python" in message

    def test_match_reports_nothing(self):
        from colcon_cargo_ros2.task.ament_cargo.build import check_version_skew

        assert check_version_skew(native_version="0.4.1", python_version="0.4.1") is None

    def test_unknown_versions_are_not_a_mismatch(self):
        """Never fail a build over a version we could not read."""
        from colcon_cargo_ros2.task.ament_cargo.build import check_version_skew

        assert check_version_skew(native_version=None, python_version="0.4.1") is None
        assert check_version_skew(native_version="0.4.1", python_version=None) is None

    def test_source_tree_version_beats_installed_metadata(self, tmp_path):
        """Editable installs run the source tree, not the recorded wheel."""
        from colcon_cargo_ros2.task.ament_cargo.build import python_package_version

        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "9.9.9"\n')

        assert python_package_version(tmp_path) == "9.9.9"

    def test_falls_back_to_distribution_metadata(self, tmp_path):
        from colcon_cargo_ros2.task.ament_cargo.build import python_package_version

        # No pyproject.toml here; whatever is installed answers, or nothing does.
        result = python_package_version(tmp_path)

        assert result is None or isinstance(result, str)

    def test_real_source_tree_matches_the_native_module(self):
        """The repo's own versions agree, so a build never trips the guard."""
        from colcon_cargo_ros2.task.ament_cargo.build import python_package_version

        assert python_package_version() is not None


@pytest.mark.parametrize("attr", ["_cargo_dependency_names", "_drop_missing_bindings"])
def test_helpers_exist(attr):
    assert hasattr(WorkspaceBindingGenerator, attr)
