# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for ``[workspace.metadata.ros2] patches-in-manifest``.

Cargo merges every ``.cargo/config.toml`` from the working directory up to /,
so a Cargo workspace nested below another inherits the outer config's
``[patch.crates-io]`` and records the entries it does not use as
``[[patch.unused]]`` in its Cargo.lock. With the option set, a workspace's
patches go into its own Cargo.toml, through links under ``.cargo/``.
"""

import json
import logging
import os
import shutil
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2 import workspace_bindgen
from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation
from colcon_cargo_ros2.workspace_bindgen import CargoTomlError, WorkspaceBindingGenerator

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib

GEN = WorkspaceBindingGenerator
OUTER_MANIFEST = textwrap.dedent(
    """\
    # The outer workspace.
    [workspace]
    resolver = "2"
    members = ["bridge"]
    exclude = ["nested"]
    """
)
OPT_IN = "\n[workspace.metadata.ros2]\npatches-in-manifest = true\n"


@pytest.fixture(autouse=True)
def _fresh_state():
    """No warning already reported, no descriptors left over from another test."""
    workspace_bindgen._REPORTED_MISMATCHES.clear()
    yield
    workspace_bindgen._REPORTED_MISMATCHES.clear()
    RustBindingAugmentation._cargo_descriptors = {}


@pytest.fixture
def log():
    """Collect the module's info and warning messages.

    Not caplog: colcon's logger does not propagate to the root handler.
    """
    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))

    handler = _Collector(level=logging.INFO)
    workspace_bindgen.logger.addHandler(handler)
    previous = workspace_bindgen.logger.level
    workspace_bindgen.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        workspace_bindgen.logger.setLevel(previous)
        workspace_bindgen.logger.removeHandler(handler)


def _warnings(log):
    return [message for level, message in log if level >= logging.WARNING]


def _setup_bindings(tmp_path: Path, packages):
    for pkg in packages:
        d = tmp_path / "build" / pkg / "rosidl_cargo" / pkg
        d.mkdir(parents=True, exist_ok=True)
        (d / "Cargo.toml").write_text(
            f'[package]\nname = "{pkg}"\nversion = "0.0.0"\nedition = "2021"\n'
        )
        (d / "src").mkdir(exist_ok=True)
        (d / "src" / "lib.rs").write_text("")


def _crate(path: Path, name: str, deps=()):
    path.mkdir(parents=True, exist_ok=True)
    lines = [
        "[package]",
        f'name = "{name}"',
        'version = "0.1.0"',
        'edition = "2021"',
        "",
        "[dependencies]",
    ]
    lines += [f'{dep} = "*"' for dep in deps]
    (path / "Cargo.toml").write_text("\n".join(lines) + "\n")
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "lib.rs").write_text("")


def _layout(tmp_path: Path, outer_manifest: str = OUTER_MANIFEST + OPT_IN, msgs=("std_msgs",)):
    """An outer workspace with member ``bridge`` and a nested workspace ``nested``.

    Returns (outer dir, nested dir, generator, ros_packages).
    """
    outer = tmp_path / "src" / "ws"
    outer.mkdir(parents=True)
    (outer / "Cargo.toml").write_text(outer_manifest)
    _crate(outer / "bridge", "bridge", msgs)

    nested = outer / "nested"
    nested.mkdir()
    (nested / "Cargo.toml").write_text('[workspace]\nmembers = ["core"]\n')
    _crate(nested / "core", "core_proto")

    _setup_bindings(tmp_path, msgs)

    bridge_desc = MagicMock()
    bridge_desc.path = str(outer / "bridge")
    core_desc = MagicMock()
    core_desc.path = str(nested / "core")
    RustBindingAugmentation._cargo_descriptors = {"bridge": bridge_desc, "core": core_desc}

    gen = _make_generator(tmp_path)
    gen._package_interface_deps = {"bridge": set(msgs), "core": set()}
    ros_packages = {name: Path(f"/opt/ros/jazzy/share/{name}") for name in msgs}
    return outer, nested, gen, ros_packages


def _patch_table(manifest: Path):
    return tomllib.loads(manifest.read_text()).get("patch", {}).get("crates-io", {})


# ---------------------------------------------------------------------------
# Option detection
# ---------------------------------------------------------------------------


class TestOptionDetection:
    def _target(self, tmp_path, content):
        (tmp_path / "Cargo.toml").write_text(content)
        return tmp_path

    def test_enabled(self, tmp_path):
        assert GEN._patches_in_manifest(self._target(tmp_path, OUTER_MANIFEST + OPT_IN))

    def test_absent(self, tmp_path):
        assert not GEN._patches_in_manifest(self._target(tmp_path, OUTER_MANIFEST))

    def test_false(self, tmp_path):
        content = OUTER_MANIFEST + "\n[workspace.metadata.ros2]\npatches-in-manifest = false\n"
        assert not GEN._patches_in_manifest(self._target(tmp_path, content))

    def test_package_metadata_is_ignored_with_a_warning(self, tmp_path, log):
        content = (
            '[package]\nname = "solo"\nversion = "0.1.0"\n\n'
            "[package.metadata.ros2]\npatches-in-manifest = true\n"
        )
        assert not GEN._patches_in_manifest(self._target(tmp_path, content))
        (warning,) = _warnings(log)
        assert "[package.metadata.ros2]" in warning
        assert "[workspace.metadata.ros2]" in warning

    def test_non_boolean_is_false_with_a_warning(self, tmp_path, log):
        content = OUTER_MANIFEST + '\n[workspace.metadata.ros2]\npatches-in-manifest = "yes"\n'
        assert not GEN._patches_in_manifest(self._target(tmp_path, content))
        assert "must be true or false" in _warnings(log)[0]


# ---------------------------------------------------------------------------
# Text-level merging into Cargo.toml
# ---------------------------------------------------------------------------

PATCHES = [
    'nav_msgs = { path = ".cargo/ros2-bindings/nav_msgs" }',
    'std_msgs = { path = ".cargo/ros2-bindings/std_msgs" }',
]


class TestManifestMerge:
    def test_appends_a_table_when_there_is_none(self):
        merged = GEN._merge_patches_into_manifest(OUTER_MANIFEST, PATCHES)

        assert merged.startswith(OUTER_MANIFEST)  # user text untouched
        assert merged.count("[patch.crates-io]") == 1
        table = tomllib.loads(merged)["patch"]["crates-io"]
        assert table["std_msgs"]["path"] == ".cargo/ros2-bindings/std_msgs"
        assert table["nav_msgs"]["path"] == ".cargo/ros2-bindings/nav_msgs"

    def test_goes_inside_the_users_table(self):
        content = textwrap.dedent(
            """\
            [workspace]
            members = ["a"]

            [patch.crates-io]
            # my fork
            serde = { git = "https://example.com/serde" }

            # The profile below is mine.
            [profile.release]
            lto = true
            """
        )
        merged = GEN._merge_patches_into_manifest(content, PATCHES)

        assert merged.count("[patch.crates-io]") == 1
        assert '# my fork\nserde = { git = "https://example.com/serde" }\n' in merged
        # The comment above [profile.release] stays with it.
        assert "\n\n# The profile below is mine.\n[profile.release]\nlto = true\n" in merged
        parsed = tomllib.loads(merged)
        assert set(parsed["patch"]["crates-io"]) == {"serde", "nav_msgs", "std_msgs"}
        assert parsed["profile"]["release"]["lto"] is True

    def test_users_table_at_end_of_file(self):
        content = '[workspace]\n\n[patch."crates-io"]  # mine\nfoo = { path = "x" }\n'
        merged = GEN._merge_patches_into_manifest(content, PATCHES)
        parsed = tomllib.loads(merged)
        assert set(parsed["patch"]["crates-io"]) == {"foo", "nav_msgs", "std_msgs"}

    def test_replacing_is_idempotent(self):
        first = GEN._merge_patches_into_manifest(OUTER_MANIFEST, PATCHES)
        assert GEN._merge_patches_into_manifest(first, PATCHES) == first

        user = '[workspace]\n\n[patch.crates-io]\nfoo = { path = "x" }\n\n[profile.dev]\n'
        first = GEN._merge_patches_into_manifest(user, PATCHES)
        assert GEN._merge_patches_into_manifest(first, PATCHES) == first

    def test_removal_restores_a_manifest_without_a_table(self):
        merged = GEN._merge_patches_into_manifest(OUTER_MANIFEST, PATCHES)
        removed, note = GEN._remove_patches_from_manifest(merged)
        assert removed == OUTER_MANIFEST
        assert note is None

    def test_removal_restores_the_users_table(self):
        user = '[workspace]\n\n[patch.crates-io]\nfoo = { path = "x" }\n\n[profile.dev]\n'
        merged = GEN._merge_patches_into_manifest(user, PATCHES)
        removed, note = GEN._remove_patches_from_manifest(merged)
        assert removed == user
        assert note is None

    def test_removal_keeps_our_header_for_entries_added_below_the_block(self):
        merged = GEN._merge_patches_into_manifest(OUTER_MANIFEST, PATCHES)
        merged += 'foo = { path = "x" }\n'
        removed, note = GEN._remove_patches_from_manifest(merged)
        assert tomllib.loads(removed)["patch"]["crates-io"] == {"foo": {"path": "x"}}
        assert "header stays" in note


# ---------------------------------------------------------------------------
# _write_cargo_configs with the option
# ---------------------------------------------------------------------------


class TestWriteWithOption:
    def test_manifest_gets_link_patches(self, tmp_path, log):
        outer, _nested, gen, ros = _layout(tmp_path, msgs=("std_msgs", "nav_msgs"))
        gen._write_cargo_configs(ros)

        table = _patch_table(outer / "Cargo.toml")
        assert table == {
            "nav_msgs": {"path": ".cargo/ros2-bindings/nav_msgs"},
            "std_msgs": {"path": ".cargo/ros2-bindings/std_msgs"},
        }
        for name in ("std_msgs", "nav_msgs"):
            link = outer / ".cargo" / "ros2-bindings" / name
            assert link.is_symlink()
            assert os.path.isabs(os.readlink(link))
            assert link.resolve() == (tmp_path / "build" / name / "rosidl_cargo" / name).resolve()
            assert (link / "Cargo.toml").is_file()

        (warning,) = [w for w in _warnings(log) if "Cargo.toml" in w]
        assert "+nav_msgs, +std_msgs" in warning
        assert "commit it" in warning

    def test_config_has_no_patches_but_keeps_build_and_env(self, tmp_path):
        outer, _nested, gen, ros = _layout(tmp_path)
        gen._write_cargo_configs(ros)

        config = tomllib.loads((outer / ".cargo" / "config.toml").read_text())
        assert not config.get("patch", {}).get("crates-io")
        assert "rustflags" in config["build"]
        assert "target-dir" in config["build"]

    def test_second_build_leaves_manifest_byte_identical(self, tmp_path, log):
        outer, _nested, gen, ros = _layout(tmp_path, msgs=("std_msgs", "nav_msgs"))
        gen._write_cargo_configs(ros)
        first = (outer / "Cargo.toml").read_bytes()
        mtime = (outer / "Cargo.toml").stat().st_mtime_ns
        log.clear()

        gen._write_cargo_configs(ros)

        assert (outer / "Cargo.toml").read_bytes() == first
        assert (outer / "Cargo.toml").stat().st_mtime_ns == mtime
        assert not [w for w in _warnings(log) if "Cargo.toml" in w]

    def test_stale_links_and_entries_go(self, tmp_path, log):
        outer, _nested, gen, ros = _layout(tmp_path, msgs=("std_msgs", "nav_msgs"))
        gen._write_cargo_configs(ros)
        log.clear()

        gen._package_interface_deps["bridge"] = {"std_msgs"}
        gen._write_cargo_configs(ros)

        links = outer / ".cargo" / "ros2-bindings"
        assert sorted(p.name for p in links.iterdir()) == ["std_msgs"]
        assert set(_patch_table(outer / "Cargo.toml")) == {"std_msgs"}
        assert any("(-nav_msgs)" in w for w in _warnings(log))

    def test_link_pointing_elsewhere_is_repointed(self, tmp_path):
        outer, _nested, gen, ros = _layout(tmp_path)
        gen._write_cargo_configs(ros)
        link = outer / ".cargo" / "ros2-bindings" / "std_msgs"
        link.unlink()
        link.symlink_to(tmp_path)

        gen._write_cargo_configs(ros)

        assert link.resolve() == (tmp_path / "build/std_msgs/rosidl_cargo/std_msgs").resolve()

    def test_users_patch_table_kept(self, tmp_path):
        manifest = OUTER_MANIFEST + OPT_IN + '\n[patch.crates-io]\nfoo = { path = "x" }\n'
        outer, _nested, gen, ros = _layout(tmp_path, outer_manifest=manifest)
        gen._write_cargo_configs(ros)

        text = (outer / "Cargo.toml").read_text()
        assert text.count("[patch.crates-io]") == 1
        assert set(_patch_table(outer / "Cargo.toml")) == {"foo", "std_msgs"}

    def test_unparseable_result_is_refused(self, tmp_path):
        # Dotted keys define the table, so a [patch.crates-io] header would
        # redefine it: the manifest is left alone and the error says why.
        manifest = 'patch.crates-io.foo = { path = "x" }\n' + OUTER_MANIFEST + OPT_IN
        outer, _nested, gen, ros = _layout(tmp_path, outer_manifest=manifest)

        with pytest.raises(CargoTomlError, match="table header"):
            gen._write_cargo_configs(ros)
        assert (outer / "Cargo.toml").read_text() == manifest

    def test_nested_workspace_keeps_its_own_config_patches(self, tmp_path):
        outer, nested, gen, ros = _layout(tmp_path, msgs=("std_msgs", "nav_msgs"))
        gen._package_interface_deps["core"] = {"nav_msgs"}
        gen._write_cargo_configs(ros)

        nested_config = tomllib.loads((nested / ".cargo" / "config.toml").read_text())
        assert set(nested_config["patch"]["crates-io"]) == {"nav_msgs"}
        assert "patch" not in tomllib.loads((nested / "Cargo.toml").read_text())

    def test_links_are_git_ignored(self, tmp_path):
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        outer, _nested, gen, ros = _layout(tmp_path)
        gen._write_cargo_configs(ros)

        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "."],
            cwd=outer,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert ".cargo/" not in status
        gitignore = (outer / ".gitignore").read_text()
        assert ".cargo/config.toml" in gitignore
        assert ".cargo/ros2-bindings" in gitignore


# ---------------------------------------------------------------------------
# Without the option
# ---------------------------------------------------------------------------


class TestWithoutOption:
    def test_nested_target_warning(self, tmp_path, log):
        outer, nested, gen, ros = _layout(tmp_path, outer_manifest=OUTER_MANIFEST)
        gen._write_cargo_configs(ros)

        (warning,) = [w for w in _warnings(log) if "[[patch.unused]]" in w]
        assert str(nested) in warning
        assert str(outer) in warning
        assert "the 1 [patch.crates-io] entries" in warning
        assert "patches-in-manifest = true" in warning
        assert "[workspace.metadata.ros2]" in warning
        assert str(outer / "Cargo.toml") in warning
        # Behaviour otherwise unchanged: the patches are in the config.
        assert set(_patch_table(outer / ".cargo" / "config.toml")) == {"std_msgs"}
        assert (outer / "Cargo.toml").read_text() == OUTER_MANIFEST

    def test_no_warning_with_the_option(self, tmp_path, log):
        _outer, _nested, gen, ros = _layout(tmp_path)
        gen._write_cargo_configs(ros)
        assert not [w for w in _warnings(log) if "[[patch.unused]]" in w]

    def test_turning_it_off_removes_block_and_links(self, tmp_path, log):
        outer, _nested, gen, ros = _layout(tmp_path)
        gen._write_cargo_configs(ros)
        enabled = (outer / "Cargo.toml").read_text()
        assert "[patch.crates-io]" in enabled

        (outer / "Cargo.toml").write_text(enabled.replace(OPT_IN, ""))
        log.clear()
        gen._write_cargo_configs(ros)

        assert (outer / "Cargo.toml").read_text() == OUTER_MANIFEST
        assert not (outer / ".cargo" / "ros2-bindings").exists()
        assert set(_patch_table(outer / ".cargo" / "config.toml")) == {"std_msgs"}
        assert any("removed the generated [patch.crates-io] block" in w for w in _warnings(log))


# ---------------------------------------------------------------------------
# End to end, with cargo itself
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
class TestWithCargo:
    """``cargo metadata --offline`` in both workspaces, as an IDE would run it."""

    def _metadata(self, cwd: Path):
        result = subprocess.run(
            ["cargo", "metadata", "--offline", "--format-version", "1"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_nested_lock_stays_unchanged(self, tmp_path):
        outer, nested, gen, ros = _layout(tmp_path)
        # The nested workspace's Cargo.lock as committed, before any colcon build.
        self._metadata(nested)
        committed = (nested / "Cargo.lock").read_bytes()

        gen._write_cargo_configs(ros)

        metadata = self._metadata(outer)
        std_msgs = next(p for p in metadata["packages"] if p["name"] == "std_msgs")
        assert ".cargo/ros2-bindings/std_msgs" in std_msgs["manifest_path"]
        # The generated crate is a patched dependency, not a workspace member.
        assert std_msgs["id"] not in metadata["workspace_members"]
        assert "patch.unused" not in (outer / "Cargo.lock").read_text()

        self._metadata(nested)
        assert (nested / "Cargo.lock").read_bytes() == committed

    def test_without_the_option_the_nested_lock_gains_unused_patches(self, tmp_path):
        """The failure the option exists for, so the test above means something."""
        _outer, nested, gen, ros = _layout(tmp_path, outer_manifest=OUTER_MANIFEST)
        self._metadata(nested)
        assert "patch.unused" not in (nested / "Cargo.lock").read_text()

        gen._write_cargo_configs(ros)

        self._metadata(nested)
        assert "[[patch.unused]]" in (nested / "Cargo.lock").read_text()
