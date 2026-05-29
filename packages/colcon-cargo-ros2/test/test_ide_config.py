# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for Phase 6: IDE support via .cargo/config.toml generation.

Covers:
- Cargo workspace root detection
- Comment-based marker merge logic
- Relative path computation
- Deduplication of config targets
- _cargo_toml_has_workspace helper
"""

import os
import textwrap
from pathlib import Path

from colcon_cargo_ros2.workspace_bindgen import (
    WorkspaceBindingGenerator,
    _cargo_toml_has_workspace,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cargo_toml(path: Path, workspace: bool = False, extra: str = ""):
    """Create a minimal Cargo.toml at *path*.

    Args:
        path: File path for the Cargo.toml.
        workspace: If True, include a [workspace] section.
        extra: Additional TOML content to append.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = '[package]\nname = "test"\nversion = "0.1.0"\n'
    if workspace:
        content += "\n[workspace]\n"
    if extra:
        content += "\n" + extra + "\n"
    path.write_text(content)


def _make_package_xml(path: Path, name: str = "test"):
    """Create a minimal package.xml next to a Cargo.toml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0"?>\n'
        f'<package format="3">\n'
        f"  <name>{name}</name>\n"
        f"  <version>0.1.0</version>\n"
        f"</package>\n"
    )


def _make_generator(tmp_path: Path) -> WorkspaceBindingGenerator:
    """Create a WorkspaceBindingGenerator rooted at *tmp_path*."""
    build = tmp_path / "build"
    install = tmp_path / "install"
    build.mkdir(exist_ok=True)
    install.mkdir(exist_ok=True)

    class FakeArgs:
        rosidl_runtime_rs_version = None

    return WorkspaceBindingGenerator(tmp_path, build, install, FakeArgs())


# ---------------------------------------------------------------------------
# _cargo_toml_has_workspace
# ---------------------------------------------------------------------------


class TestCargoTomlHasWorkspace:
    def test_with_workspace(self, tmp_path):
        toml = tmp_path / "Cargo.toml"
        _make_cargo_toml(toml, workspace=True)
        assert _cargo_toml_has_workspace(toml) is True

    def test_without_workspace(self, tmp_path):
        toml = tmp_path / "Cargo.toml"
        _make_cargo_toml(toml, workspace=False)
        assert _cargo_toml_has_workspace(toml) is False

    def test_missing_file(self, tmp_path):
        assert _cargo_toml_has_workspace(tmp_path / "nope.toml") is False

    def test_empty_workspace(self, tmp_path):
        """An empty [workspace] section still counts as having a workspace."""
        toml = tmp_path / "Cargo.toml"
        toml.write_text('[package]\nname = "x"\nversion = "0.1.0"\n\n[workspace]\n')
        assert _cargo_toml_has_workspace(toml) is True

    def test_workspace_with_members(self, tmp_path):
        toml = tmp_path / "Cargo.toml"
        toml.write_text(
            '[package]\nname = "x"\nversion = "0.1.0"\n\n[workspace]\nmembers = ["a", "b"]\n'
        )
        assert _cargo_toml_has_workspace(toml) is True


# ---------------------------------------------------------------------------
# _detect_cargo_workspace_root
# ---------------------------------------------------------------------------


class TestDetectCargoWorkspaceRoot:
    def test_standalone_with_workspace(self, tmp_path):
        """Crate with [workspace] → returns itself."""
        gen = _make_generator(tmp_path)
        crate = tmp_path / "src" / "my_crate"
        _make_cargo_toml(crate / "Cargo.toml", workspace=True)
        assert gen._detect_cargo_workspace_root(crate, tmp_path) == crate

    def test_standalone_without_workspace(self, tmp_path):
        """Crate without [workspace] and no parent workspace → returns itself."""
        gen = _make_generator(tmp_path)
        crate = tmp_path / "src" / "my_crate"
        _make_cargo_toml(crate / "Cargo.toml", workspace=False)
        assert gen._detect_cargo_workspace_root(crate, tmp_path) == crate

    def test_workspace_member(self, tmp_path):
        """Crate whose parent has [workspace] → returns parent."""
        gen = _make_generator(tmp_path)
        ws = tmp_path / "src" / "my_robot"
        _make_cargo_toml(ws / "Cargo.toml", workspace=True)

        crate = ws / "core"
        _make_cargo_toml(crate / "Cargo.toml", workspace=False)

        assert gen._detect_cargo_workspace_root(crate, tmp_path) == ws

    def test_deeply_nested_member(self, tmp_path):
        """Crate deeply nested under a workspace root."""
        gen = _make_generator(tmp_path)
        ws = tmp_path / "src" / "my_robot"
        _make_cargo_toml(ws / "Cargo.toml", workspace=True)

        crate = ws / "packages" / "nav"
        _make_cargo_toml(crate / "Cargo.toml", workspace=False)

        assert gen._detect_cargo_workspace_root(crate, tmp_path) == ws

    def test_colcon_root_has_workspace(self, tmp_path):
        """Cargo workspace at colcon root level."""
        gen = _make_generator(tmp_path)
        _make_cargo_toml(tmp_path / "Cargo.toml", workspace=True)
        assert gen._detect_cargo_workspace_root(tmp_path, tmp_path) == tmp_path

    def test_stops_at_colcon_root(self, tmp_path):
        """Does not walk above the colcon workspace root."""
        gen = _make_generator(tmp_path)
        # Put a workspace Cargo.toml ABOVE the colcon root
        colcon_root = tmp_path / "ws"
        colcon_root.mkdir()
        _make_cargo_toml(tmp_path / "Cargo.toml", workspace=True)

        crate = colcon_root / "src" / "pkg"
        _make_cargo_toml(crate / "Cargo.toml", workspace=False)

        # Should not find the workspace above colcon root
        assert gen._detect_cargo_workspace_root(crate, colcon_root) == crate


# ---------------------------------------------------------------------------
# _generate_marker_block / _merge_into_config
# ---------------------------------------------------------------------------


class TestMarkerBlock:
    def test_generate_marker_block(self):
        patches = [
            'geometry_msgs = { path = "../../build/geometry_msgs" }',
            'std_msgs = { path = "../../build/std_msgs" }',
        ]
        block = WorkspaceBindingGenerator._generate_marker_block(patches)
        assert "# BEGIN colcon-cargo-ros2" in block
        assert "# END colcon-cargo-ros2" in block
        assert 'std_msgs = { path = "../../build/std_msgs" }' in block
        assert "Do not edit between markers" in block


class TestMergeIntoConfig:
    _cls = WorkspaceBindingGenerator

    def _marker_block(self, patches=None):
        if patches is None:
            patches = ['std_msgs = { path = "build/std_msgs" }']
        return self._cls._generate_marker_block(patches)

    def test_new_file(self):
        """No existing config → creates [patch.crates-io] with markers."""
        result = self._cls._merge_into_config(None, self._marker_block())
        assert result.startswith("[patch.crates-io]\n")
        assert "# BEGIN colcon-cargo-ros2" in result
        assert "# END colcon-cargo-ros2" in result
        assert "std_msgs" in result

    def test_empty_string(self):
        result = self._cls._merge_into_config("", self._marker_block())
        assert "[patch.crates-io]" in result
        assert "std_msgs" in result

    def test_existing_with_markers_replaces(self):
        """Existing markers → content between them is replaced."""
        existing = textwrap.dedent("""\
            [patch.crates-io]
            my_crate = { path = "../my" }
            # BEGIN colcon-cargo-ros2 generated patches
            old_msgs = { path = "old" }
            # END colcon-cargo-ros2

            [build]
            rustflags = []
        """)
        result = self._cls._merge_into_config(existing, self._marker_block())
        assert "old_msgs" not in result
        assert "std_msgs" in result
        assert 'my_crate = { path = "../my" }' in result
        assert "[build]" in result

    def test_existing_patch_section_no_markers(self):
        """Existing [patch.crates-io] without markers → appends before next section."""
        existing = textwrap.dedent("""\
            [patch.crates-io]
            my_crate = { path = "../my" }

            [build]
            rustflags = []
        """)
        result = self._cls._merge_into_config(existing, self._marker_block())
        assert 'my_crate = { path = "../my" }' in result
        assert "std_msgs" in result
        assert "[build]" in result
        # Markers should appear between [patch] entries and [build]
        patch_pos = result.index("[patch.crates-io]")
        build_pos = result.index("[build]")
        marker_pos = result.index("# BEGIN colcon-cargo-ros2")
        assert patch_pos < marker_pos < build_pos

    def test_no_patch_section(self):
        """No [patch.crates-io] section → appends new section at end."""
        existing = textwrap.dedent("""\
            [build]
            rustflags = []
        """)
        result = self._cls._merge_into_config(existing, self._marker_block())
        assert "[build]" in result
        assert "[patch.crates-io]" in result
        assert "std_msgs" in result

    def test_preserves_other_sections(self):
        """Other TOML sections are fully preserved."""
        existing = textwrap.dedent("""\
            [env]
            MY_VAR = "hello"

            [build]
            rustflags = ["-C", "link-arg=-fuse-ld=lld"]
        """)
        result = self._cls._merge_into_config(existing, self._marker_block())
        assert '[env]\nMY_VAR = "hello"' in result
        assert "link-arg=-fuse-ld=lld" in result
        assert "[patch.crates-io]" in result

    def test_idempotent(self):
        """Running merge twice produces the same output."""
        block = self._marker_block()
        first = self._cls._merge_into_config(None, block)
        second = self._cls._merge_into_config(first, block)
        assert first == second

    def test_update_patches(self):
        """Updating with different patches replaces old ones."""
        block_v1 = self._marker_block(['std_msgs = { path = "v1" }'])
        block_v2 = self._marker_block(['geometry_msgs = { path = "v2" }'])

        first = self._cls._merge_into_config(None, block_v1)
        assert "std_msgs" in first

        second = self._cls._merge_into_config(first, block_v2)
        assert "std_msgs" not in second
        assert "geometry_msgs" in second

    def test_deleted_markers_treated_as_new(self):
        """If user deletes markers, next build re-appends."""
        existing = textwrap.dedent("""\
            [patch.crates-io]
            my_crate = { path = "../my" }
        """)
        result = self._cls._merge_into_config(existing, self._marker_block())
        assert "# BEGIN colcon-cargo-ros2" in result
        assert "my_crate" in result


# ---------------------------------------------------------------------------
# _compute_relative_patches
# ---------------------------------------------------------------------------


class TestComputeRelativePatches:
    def test_basic_relative_paths(self, tmp_path):
        config_target = tmp_path / "src" / "my_crate"
        config_target.mkdir(parents=True)

        binding_a = tmp_path / "build" / "std_msgs" / "rosidl_cargo" / "std_msgs"
        binding_b = tmp_path / "build" / "geometry_msgs" / "rosidl_cargo" / "geometry_msgs"
        binding_a.mkdir(parents=True)
        binding_b.mkdir(parents=True)

        binding_dirs = {"std_msgs": binding_a, "geometry_msgs": binding_b}
        patches = WorkspaceBindingGenerator._compute_relative_patches(config_target, binding_dirs)

        assert len(patches) == 2
        # Patches are sorted alphabetically
        assert patches[0].startswith("geometry_msgs")
        assert patches[1].startswith("std_msgs")
        # All paths should be relative (no leading /)
        for p in patches:
            path_val = p.split('"')[1]
            assert not os.path.isabs(path_val), f"Path should be relative: {path_val}"

    def test_crate_at_workspace_root(self, tmp_path):
        """Config target is at workspace root → paths are like build/..."""
        binding = tmp_path / "build" / "std_msgs" / "rosidl_cargo" / "std_msgs"
        binding.mkdir(parents=True)

        patches = WorkspaceBindingGenerator._compute_relative_patches(
            tmp_path, {"std_msgs": binding}
        )
        assert len(patches) == 1
        path_val = patches[0].split('"')[1]
        assert path_val.startswith("build/")

    def test_deeply_nested_crate(self, tmp_path):
        """Deeply nested crate → paths go up multiple levels."""
        config_target = tmp_path / "src" / "a" / "b" / "c"
        config_target.mkdir(parents=True)

        binding = tmp_path / "build" / "std_msgs" / "rosidl_cargo" / "std_msgs"
        binding.mkdir(parents=True)

        patches = WorkspaceBindingGenerator._compute_relative_patches(
            config_target, {"std_msgs": binding}
        )
        path_val = patches[0].split('"')[1]
        assert path_val.startswith("../../../../")

    def test_forward_slashes(self, tmp_path):
        """All paths use forward slashes regardless of platform."""
        binding = tmp_path / "build" / "std_msgs"
        binding.mkdir(parents=True)

        patches = WorkspaceBindingGenerator._compute_relative_patches(
            tmp_path, {"std_msgs": binding}
        )
        path_val = patches[0].split('"')[1]
        assert "\\" not in path_val


# ---------------------------------------------------------------------------
# _collect_binding_dirs
# ---------------------------------------------------------------------------


class TestCollectBindingDirs:
    def test_finds_nested_bindings(self, tmp_path):
        gen = _make_generator(tmp_path)
        # Create nested structure: build/std_msgs/rosidl_cargo/std_msgs/Cargo.toml
        nested = tmp_path / "build" / "std_msgs" / "rosidl_cargo" / "std_msgs"
        nested.mkdir(parents=True)
        (nested / "Cargo.toml").write_text('[package]\nname = "std_msgs"\n')

        dirs = gen._collect_binding_dirs({"std_msgs": Path("/some/share/std_msgs")})
        assert "std_msgs" in dirs
        assert dirs["std_msgs"] == nested

    def test_finds_flat_bindings(self, tmp_path):
        gen = _make_generator(tmp_path)
        # Create flat structure: build/my_pkg/rosidl_cargo/Cargo.toml
        flat = tmp_path / "build" / "my_pkg" / "rosidl_cargo"
        flat.mkdir(parents=True)
        (flat / "Cargo.toml").write_text('[package]\nname = "my_pkg"\n')

        dirs = gen._collect_binding_dirs({"my_pkg": Path("/some/share/my_pkg")})
        assert "my_pkg" in dirs
        assert dirs["my_pkg"] == flat

    def test_skips_missing(self, tmp_path):
        gen = _make_generator(tmp_path)
        dirs = gen._collect_binding_dirs({"nope": Path("/some/share/nope")})
        assert len(dirs) == 0


# ---------------------------------------------------------------------------
# End-to-end: _write_cargo_configs (filesystem-based)
# ---------------------------------------------------------------------------


class TestWriteCargoConfigs:
    """Integration-style tests using real filesystem layout."""

    def _setup_bindings(self, tmp_path, packages, dependency_map=None):
        """Create fake binding directories under build/."""
        dependency_map = dependency_map or {}
        for pkg in packages:
            d = tmp_path / "build" / pkg / "rosidl_cargo" / pkg
            d.mkdir(parents=True)
            content = f'[package]\nname = "{pkg}"\n'
            dependencies = dependency_map.get(pkg, [])
            if dependencies:
                content += "\n[dependencies]\n"
                for dep in dependencies:
                    content += f'{dep} = "*"\n'
            (d / "Cargo.toml").write_text(content)

    def test_standalone_crate(self, tmp_path):
        """A standalone crate gets .cargo/config.toml next to its Cargo.toml."""
        gen = _make_generator(tmp_path)

        # Create crate
        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            crate / "Cargo.toml",
            workspace=True,
            extra='[dependencies]\nstd_msgs = "*"',
        )
        _make_package_xml(crate / "package.xml", "my_robot")

        # Create bindings
        self._setup_bindings(tmp_path, ["std_msgs", "geometry_msgs"])

        # Simulate the augmentation data
        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        desc.name = "my_robot"
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            gen._write_cargo_configs(
                {
                    "std_msgs": Path("/opt/ros/jazzy/share/std_msgs"),
                    "geometry_msgs": Path("/opt/ros/jazzy/share/geometry_msgs"),
                }
            )

            config = crate / ".cargo" / "config.toml"
            assert config.exists()

            content = config.read_text()
            assert "[patch.crates-io]" in content
            assert "# BEGIN colcon-cargo-ros2 generated patches" in content
            assert "# END colcon-cargo-ros2" in content
            assert "std_msgs" in content
            assert "geometry_msgs" not in content

            # Verify [build] section with rustflags is present
            assert "[build]" in content
            assert "# BEGIN colcon-cargo-ros2 generated build flags" in content
            assert "# END colcon-cargo-ros2 build flags" in content
            assert "rustflags" in content

            # Verify paths are relative
            for line in content.splitlines():
                if "path =" in line:
                    path_val = line.split('"')[1]
                    assert not os.path.isabs(path_val)
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_preserves_existing_user_config(self, tmp_path):
        """User content in .cargo/config.toml is preserved across builds."""
        gen = _make_generator(tmp_path)

        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            crate / "Cargo.toml",
            workspace=True,
            extra='[dependencies]\nstd_msgs = "*"',
        )
        _make_package_xml(crate / "package.xml", "my_robot")

        # Pre-existing user config
        cargo_dir = crate / ".cargo"
        cargo_dir.mkdir(parents=True)
        (cargo_dir / "config.toml").write_text(
            '[build]\nrustflags = ["-C", "link-arg=-fuse-ld=lld"]\n'
        )

        self._setup_bindings(tmp_path, ["std_msgs"])

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            gen._write_cargo_configs({"std_msgs": Path("/opt/ros/jazzy/share/std_msgs")})

            content = (cargo_dir / "config.toml").read_text()
            assert "link-arg=-fuse-ld=lld" in content
            assert "[patch.crates-io]" in content
            assert "std_msgs" in content
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_idempotent_writes(self, tmp_path):
        """Running _write_cargo_configs twice produces the same file."""
        gen = _make_generator(tmp_path)

        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            crate / "Cargo.toml",
            workspace=True,
            extra='[dependencies]\nstd_msgs = "*"',
        )
        _make_package_xml(crate / "package.xml", "my_robot")

        self._setup_bindings(tmp_path, ["std_msgs"])

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            ros_pkgs = {"std_msgs": Path("/opt/ros/jazzy/share/std_msgs")}
            gen._write_cargo_configs(ros_pkgs)
            first = (crate / ".cargo" / "config.toml").read_text()

            gen._write_cargo_configs(ros_pkgs)
            second = (crate / ".cargo" / "config.toml").read_text()

            assert first == second
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_workspace_member_dedup(self, tmp_path):
        """Two crates sharing a Cargo workspace → one .cargo/config.toml."""
        gen = _make_generator(tmp_path)

        # Cargo workspace root
        ws = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            ws / "Cargo.toml",
            workspace=True,
            extra='members = ["core", "nav"]\n\n[workspace.dependencies]\nstd_msgs = "*"',
        )

        # Two member crates
        core = ws / "core"
        _make_cargo_toml(
            core / "Cargo.toml",
            workspace=False,
            extra='[dependencies]\nstd_msgs = { workspace = true }',
        )
        _make_package_xml(core / "package.xml", "core")

        nav = ws / "nav"
        _make_cargo_toml(nav / "Cargo.toml", workspace=False)
        _make_package_xml(nav / "package.xml", "nav")

        self._setup_bindings(tmp_path, ["std_msgs"])

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc_core = MagicMock()
        desc_core.path = str(core)
        desc_nav = MagicMock()
        desc_nav.path = str(nav)
        RustBindingAugmentation._cargo_descriptors = {"core": desc_core, "nav": desc_nav}

        try:
            gen._write_cargo_configs({"std_msgs": Path("/opt/ros/jazzy/share/std_msgs")})

            # Only one config at workspace root, not per-crate
            assert (ws / ".cargo" / "config.toml").exists()
            assert not (core / ".cargo" / "config.toml").exists()
            assert not (nav / ".cargo" / "config.toml").exists()
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_filters_unused_generated_patches(self, tmp_path):
        """Generated bindings absent from Cargo manifests are not patched."""
        gen = _make_generator(tmp_path)

        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            crate / "Cargo.toml",
            workspace=True,
            extra='[dependencies]\nstd_msgs = "*"',
        )
        _make_package_xml(crate / "package.xml", "my_robot")

        self._setup_bindings(tmp_path, ["std_msgs", "geometry_msgs", "action_msgs"])

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            gen._write_cargo_configs(
                {
                    "std_msgs": Path("/opt/ros/jazzy/share/std_msgs"),
                    "geometry_msgs": Path("/opt/ros/jazzy/share/geometry_msgs"),
                    "action_msgs": Path("/opt/ros/jazzy/share/action_msgs"),
                }
            )

            content = (crate / ".cargo" / "config.toml").read_text()
            assert "std_msgs" in content
            assert "geometry_msgs" not in content
            assert "action_msgs" not in content
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_includes_transitive_generated_binding_dependencies(self, tmp_path):
        """A selected generated binding pulls in generated deps it references."""
        gen = _make_generator(tmp_path)

        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(
            crate / "Cargo.toml",
            workspace=True,
            extra='[dependencies]\nstd_msgs = "*"',
        )
        _make_package_xml(crate / "package.xml", "my_robot")

        self._setup_bindings(
            tmp_path,
            ["std_msgs", "builtin_interfaces", "action_msgs"],
            dependency_map={"std_msgs": ["builtin_interfaces"]},
        )

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            gen._write_cargo_configs(
                {
                    "std_msgs": Path("/opt/ros/jazzy/share/std_msgs"),
                    "builtin_interfaces": Path("/opt/ros/jazzy/share/builtin_interfaces"),
                    "action_msgs": Path("/opt/ros/jazzy/share/action_msgs"),
                }
            )

            content = (crate / ".cargo" / "config.toml").read_text()
            assert "std_msgs" in content
            assert "builtin_interfaces" in content
            assert "action_msgs" not in content
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_empty_filter_rewrites_stale_generated_patches(self, tmp_path):
        """No matching bindings still rewrites markers and removes stale entries."""
        gen = _make_generator(tmp_path)

        crate = tmp_path / "src" / "my_robot"
        _make_cargo_toml(crate / "Cargo.toml", workspace=True)
        _make_package_xml(crate / "package.xml", "my_robot")

        cargo_dir = crate / ".cargo"
        cargo_dir.mkdir(parents=True)
        stale_block = WorkspaceBindingGenerator._generate_marker_block(
            ['std_msgs = { path = "old" }']
        )
        (cargo_dir / "config.toml").write_text(
            f"[patch.crates-io]\n{stale_block}\n"
        )

        self._setup_bindings(tmp_path, ["std_msgs"])

        from unittest.mock import MagicMock

        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        desc = MagicMock()
        desc.path = str(crate)
        RustBindingAugmentation._cargo_descriptors = {"my_robot": desc}

        try:
            gen._write_cargo_configs({"std_msgs": Path("/opt/ros/jazzy/share/std_msgs")})

            content = (cargo_dir / "config.toml").read_text()
            assert "old" not in content
            assert 'std_msgs = { path =' not in content
            assert "# BEGIN colcon-cargo-ros2 generated patches" in content
            assert "# END colcon-cargo-ros2" in content
        finally:
            RustBindingAugmentation._cargo_descriptors = {}


# ---------------------------------------------------------------------------
# _compute_rustflags
# ---------------------------------------------------------------------------


class TestComputeRustflags:
    def test_from_install_base(self, tmp_path):
        """Collects -L flags from install base lib dirs."""
        gen = _make_generator(tmp_path)
        # Create install/<pkg>/lib/ directories
        (tmp_path / "install" / "pkg_a" / "lib").mkdir(parents=True)
        (tmp_path / "install" / "pkg_b" / "lib").mkdir(parents=True)

        flags = gen._compute_rustflags()
        assert len(flags) >= 2
        assert any("pkg_a" in f for f in flags)
        assert any("pkg_b" in f for f in flags)
        # All flags should use absolute paths
        for f in flags:
            assert "native=/" in f or "native=\\" in f

    def test_from_ament_prefix_path(self, tmp_path, monkeypatch):
        """Collects -L flags from AMENT_PREFIX_PATH."""
        gen = _make_generator(tmp_path)
        ros_prefix = tmp_path / "opt" / "ros" / "jazzy"
        (ros_prefix / "lib").mkdir(parents=True)
        monkeypatch.setenv("AMENT_PREFIX_PATH", str(ros_prefix))

        flags = gen._compute_rustflags()
        assert any("jazzy" in f for f in flags)

    def test_empty_when_nothing_exists(self, tmp_path, monkeypatch):
        """Returns empty list when no lib directories exist."""
        gen = _make_generator(tmp_path)
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)

        flags = gen._compute_rustflags()
        assert flags == []

    def test_skips_non_directories(self, tmp_path, monkeypatch):
        """Skips files in install base (only directories)."""
        gen = _make_generator(tmp_path)
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "some_file.txt").write_text("not a dir")
        (install / "real_pkg" / "lib").mkdir(parents=True)

        flags = gen._compute_rustflags()
        assert len(flags) == 1
        assert "real_pkg" in flags[0]


# ---------------------------------------------------------------------------
# _generate_build_marker_block
# ---------------------------------------------------------------------------


class TestGenerateBuildMarkerBlock:
    _cls = WorkspaceBindingGenerator

    def test_with_rustflags(self):
        """Block contains rustflags array."""
        flags = ['"-L", "native=/opt/ros/jazzy/lib"']
        block = self._cls._generate_build_marker_block(flags)
        assert "# BEGIN colcon-cargo-ros2 generated build flags" in block
        assert "# END colcon-cargo-ros2 build flags" in block
        assert "rustflags = [" in block
        assert "native=/opt/ros/jazzy/lib" in block

    def test_empty_rustflags(self):
        """Empty flags produce rustflags = []."""
        block = self._cls._generate_build_marker_block([])
        assert "rustflags = []" in block
        assert "# BEGIN colcon-cargo-ros2 generated build flags" in block

    def test_multiple_flags(self):
        """Multiple flags are separated by commas."""
        flags = [
            '"-L", "native=/opt/ros/jazzy/lib"',
            '"-L", "native=/ws/install/pkg/lib"',
        ]
        block = self._cls._generate_build_marker_block(flags)
        lines = block.splitlines()
        # First flag line should end with comma, last should not
        flag_lines = [line for line in lines if "native=" in line]
        assert len(flag_lines) == 2
        assert flag_lines[0].rstrip().endswith(",")
        assert not flag_lines[1].rstrip().endswith(",")


# ---------------------------------------------------------------------------
# _merge_build_into_config
# ---------------------------------------------------------------------------


class TestMergeBuildIntoConfig:
    _cls = WorkspaceBindingGenerator

    def _build_block(self, flags=None):
        if flags is None:
            flags = ['"-L", "native=/opt/ros/jazzy/lib"']
        return self._cls._generate_build_marker_block(flags)

    def test_no_build_section(self):
        """No [build] section → appends new [build] section at end."""
        existing = '[patch.crates-io]\nstd_msgs = { path = "x" }\n'
        result = self._cls._merge_build_into_config(existing, self._build_block())
        assert "[build]" in result
        assert "# BEGIN colcon-cargo-ros2 generated build flags" in result
        assert "native=/opt/ros/jazzy/lib" in result
        # [patch.crates-io] should still be first
        assert result.index("[patch.crates-io]") < result.index("[build]")

    def test_existing_build_section_no_markers(self):
        """Existing [build] section without markers → appends block within section."""
        existing = textwrap.dedent("""\
            [build]
            jobs = 4

            [env]
            MY_VAR = "hello"
        """)
        result = self._cls._merge_build_into_config(existing, self._build_block())
        assert "jobs = 4" in result
        assert "# BEGIN colcon-cargo-ros2 generated build flags" in result
        assert '[env]\nMY_VAR = "hello"' in result

    def test_existing_build_markers_replaced(self):
        """Existing build markers → content between them is replaced."""
        existing = textwrap.dedent("""\
            [build]
            # BEGIN colcon-cargo-ros2 generated build flags
            rustflags = ["-L", "native=/old/path"]
            # END colcon-cargo-ros2 build flags

            [env]
            MY_VAR = "hello"
        """)
        new_block = self._build_block(['"-L", "native=/new/path"'])
        result = self._cls._merge_build_into_config(existing, new_block)
        assert "/old/path" not in result
        assert "/new/path" in result
        assert '[env]\nMY_VAR = "hello"' in result

    def test_idempotent(self):
        """Running merge twice produces the same output."""
        existing = '[patch.crates-io]\nstd_msgs = { path = "x" }\n'
        block = self._build_block()
        first = self._cls._merge_build_into_config(existing, block)
        second = self._cls._merge_build_into_config(first, block)
        assert first == second

    def test_preserves_other_sections(self):
        """Other TOML sections are fully preserved."""
        existing = textwrap.dedent("""\
            [env]
            MY_VAR = "hello"

            [target.x86_64-unknown-linux-gnu]
            linker = "clang"
        """)
        result = self._cls._merge_build_into_config(existing, self._build_block())
        assert '[env]\nMY_VAR = "hello"' in result
        assert 'linker = "clang"' in result
        assert "[build]" in result
