# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for Phase 9: a bare ``cargo`` invocation works after one colcon build.

Covers:
- Library search paths derived from the dependency graph, not from whatever
  happens to sit in install/
- rpath link arguments so built binaries run without LD_LIBRARY_PATH
- The ``[env]`` block that lets build scripts run without a sourced ROS
- ``target-dir`` redirection out of the source tree
- .gitignore hygiene for the generated config
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_generator

from colcon_cargo_ros2.workspace_bindgen import WorkspaceBindingGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lib_dir(root: Path, pkg: str, with_library: bool = True) -> Path:
    """Create ``install/<pkg>/lib``, optionally holding a real library file."""
    lib = root / "install" / pkg / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    if with_library:
        (lib / f"lib{pkg}__rosidl_typesupport_c.so").write_text("elf")
    return lib


def _crates(*names):
    """The (name, path) pairs a config target covers."""
    return [(name, Path(f"/src/{name}")) for name in names]


def _git_init(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


# ---------------------------------------------------------------------------
# Library search directories
# ---------------------------------------------------------------------------


class TestLibrarySearchDirs:
    def test_skips_directories_without_libraries(self, tmp_path, monkeypatch):
        """A Rust binary package installs no libraries, so its lib/ is noise."""
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        _lib_dir(tmp_path, "my_msgs", with_library=True)
        _lib_dir(tmp_path, "rust_node", with_library=False)
        gen = _make_generator(tmp_path)

        dirs = gen._library_search_dirs(None)

        assert any("my_msgs" in str(d) for d in dirs)
        assert not any("rust_node" in str(d) for d in dirs)

    def test_narrows_to_declared_dependencies(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        _lib_dir(tmp_path, "my_msgs")
        _lib_dir(tmp_path, "other_msgs")
        gen = _make_generator(tmp_path)

        dirs = gen._library_search_dirs({"my_msgs"})

        assert [d.parent.name for d in dirs] == ["my_msgs"]

    def test_unknown_attribution_includes_everything(self, tmp_path, monkeypatch):
        """None means 'unknown', which must not be read as 'needs nothing'."""
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        _lib_dir(tmp_path, "my_msgs")
        _lib_dir(tmp_path, "other_msgs")
        gen = _make_generator(tmp_path)

        dirs = gen._library_search_dirs(None)

        assert sorted(d.parent.name for d in dirs) == ["my_msgs", "other_msgs"]

    def test_ignores_workspace_prefixes_on_ament_prefix_path(self, tmp_path, monkeypatch):
        """Sourcing install/setup.bash must not widen the next build's config.

        The workspace's own prefixes land on AMENT_PREFIX_PATH once it has been
        sourced, and taking them there would bypass the per-target narrowing --
        so a second build would produce a different config than the first.
        """
        _lib_dir(tmp_path, "my_msgs")
        _lib_dir(tmp_path, "other_msgs")
        ros = tmp_path / "opt" / "ros" / "humble"
        (ros / "lib").mkdir(parents=True)
        (ros / "lib" / "librcl.so").write_text("elf")
        monkeypatch.setenv(
            "AMENT_PREFIX_PATH",
            f"{tmp_path / 'install' / 'other_msgs'}:{ros}",
        )
        gen = _make_generator(tmp_path)

        dirs = gen._library_search_dirs({"my_msgs"})

        assert [str(d) for d in dirs] == [
            str(tmp_path / "install" / "my_msgs" / "lib"),
            str(ros / "lib"),
        ]

    def test_includes_ament_prefix_path(self, tmp_path, monkeypatch):
        ros = tmp_path / "opt" / "ros" / "humble"
        (ros / "lib").mkdir(parents=True)
        (ros / "lib" / "librcl.so").write_text("elf")
        monkeypatch.setenv("AMENT_PREFIX_PATH", str(ros))
        gen = _make_generator(tmp_path)

        dirs = gen._library_search_dirs(set())

        assert dirs == [ros / "lib"]


class TestSelectLibPackagesForTarget:
    def test_union_of_crate_dependencies(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._package_all_deps = {"pkg_b": {"std_msgs"}, "pkg_c": {"geometry_msgs"}}

        assert gen._select_lib_packages_for_target(_crates("pkg_b", "pkg_c")) == {
            "std_msgs",
            "geometry_msgs",
        }

    def test_unknown_crate_disables_narrowing(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._package_all_deps = {"pkg_b": {"std_msgs"}}

        assert gen._select_lib_packages_for_target(_crates("pkg_b", "pkg_x")) is None


# ---------------------------------------------------------------------------
# rustflags: -L plus rpath
# ---------------------------------------------------------------------------


class TestComputeRustflagsWithRpath:
    def test_linux_emits_rpath_with_disabled_new_dtags(self, tmp_path, monkeypatch):
        """RUNPATH does not cover transitive libraries; RPATH does."""
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        lib = _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)

        flags = gen._compute_rustflags(None)

        assert f'"-L", "native={lib}"' in flags
        assert f'"-C", "link-arg=-Wl,-rpath,{lib},--disable-new-dtags"' in flags

    def test_macos_emits_plain_rpath(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "darwin")
        lib = _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)

        flags = gen._compute_rustflags(None)

        assert f'"-C", "link-arg=-Wl,-rpath,{lib}"' in flags

    def test_windows_emits_no_rpath(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "win32")
        _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)

        flags = gen._compute_rustflags(None)

        # Matching on "link-arg", not on "rpath": pytest's tmp_path is named
        # after the test, so the flag *values* contain the word "rpath".
        assert not any("link-arg" in f for f in flags)

    def test_no_rpath_option(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        lib = _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)
        gen.args.no_rpath = True

        flags = gen._compute_rustflags(None)

        assert f'"-L", "native={lib}"' in flags
        assert not any("link-arg" in f for f in flags)


# ---------------------------------------------------------------------------
# [env] block
# ---------------------------------------------------------------------------


class TestRelocatableRpath:
    """Workspace-internal libraries get $ORIGIN-relative entries as well.

    An absolute rpath stops working the moment the workspace is moved, renamed
    or copied elsewhere, which is a normal thing to do with a built tree. The
    relative entries survive it, because they are resolved from wherever the
    binary itself ended up.
    """

    def test_workspace_libraries_get_origin_relative_entries(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)

        flags = gen._compute_rustflags({"my_msgs"})
        rpaths = " ".join(flags)

        # From an installed binary: install/<consumer>/lib/<consumer>/<bin>
        assert "$ORIGIN/../../../my_msgs/lib" in rpaths
        # From a built one: build/.cargo_target/<slug>/<profile>/<bin>
        assert "$ORIGIN/../../../../install/my_msgs/lib" in rpaths
        # ... and the same with a target triple in the path
        assert "$ORIGIN/../../../../../install/my_msgs/lib" in rpaths
        # The absolute entry stays, for layouts these depths do not describe.
        assert f"native={tmp_path / 'install' / 'my_msgs' / 'lib'}" in rpaths

    def test_system_libraries_stay_absolute(self, tmp_path, monkeypatch):
        """/opt/ros is not part of the workspace and does not move with it."""
        monkeypatch.setattr("sys.platform", "linux")
        ros = tmp_path / "opt" / "ros" / "humble"
        (ros / "lib").mkdir(parents=True)
        (ros / "lib" / "librcl.so").write_text("elf")
        monkeypatch.setenv("AMENT_PREFIX_PATH", str(ros))
        gen = _make_generator(tmp_path)

        flags = [f for f in gen._compute_rustflags(set()) if "link-arg" in f]

        assert flags
        assert not any("ORIGIN" in f for f in flags)

    def test_macos_uses_loader_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "darwin")
        _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)

        rpaths = " ".join(gen._compute_rustflags({"my_msgs"}))

        assert "@loader_path/../../../my_msgs/lib" in rpaths
        assert "$ORIGIN" not in rpaths

    def test_no_rpath_suppresses_relative_entries_too(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        _lib_dir(tmp_path, "my_msgs")
        gen = _make_generator(tmp_path)
        gen.args.no_rpath = True

        flags = gen._compute_rustflags({"my_msgs"})

        assert not any("link-arg" in f for f in flags)

    def test_install_base_name_is_respected(self, tmp_path, monkeypatch):
        """The --install-base option can rename the directory."""
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        monkeypatch.setattr("sys.platform", "linux")
        lib = tmp_path / "elsewhere" / "my_msgs" / "lib"
        lib.mkdir(parents=True)
        (lib / "libmy_msgs.so").write_text("elf")
        gen = _make_generator(tmp_path)
        gen.install_base = tmp_path / "elsewhere"

        rpaths = " ".join(gen._compute_rustflags({"my_msgs"}))

        assert "$ORIGIN/../../../../elsewhere/my_msgs/lib" in rpaths


class TestComputeEnv:
    def test_prepends_workspace_prefixes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AMENT_PREFIX_PATH", "/opt/ros/humble")
        (tmp_path / "install" / "my_msgs" / "share").mkdir(parents=True)
        gen = _make_generator(tmp_path)

        env = gen._compute_env()

        expected = f"{tmp_path / 'install' / 'my_msgs'}:/opt/ros/humble"
        assert env["AMENT_PREFIX_PATH"] == expected

    def test_no_duplicate_entries(self, tmp_path, monkeypatch):
        prefix = tmp_path / "install" / "my_msgs"
        (prefix / "share").mkdir(parents=True)
        monkeypatch.setenv("AMENT_PREFIX_PATH", f"{prefix}:/opt/ros/humble")
        gen = _make_generator(tmp_path)

        env = gen._compute_env()

        assert env["AMENT_PREFIX_PATH"] == f"{prefix}:/opt/ros/humble"

    def test_empty_without_any_prefix(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        gen = _make_generator(tmp_path)

        assert gen._compute_env() == {}


class TestEnvMarkerBlock:
    _cls = WorkspaceBindingGenerator

    def test_entries_do_not_force(self):
        """A sourced environment must win over the value baked at build time."""
        block = self._cls._generate_env_marker_block({"AMENT_PREFIX_PATH": "/opt/ros/humble"})

        assert 'AMENT_PREFIX_PATH = { value = "/opt/ros/humble", force = false }' in block
        assert "# BEGIN colcon-cargo-ros2 generated environment" in block
        assert "# END colcon-cargo-ros2 environment" in block

    def test_merges_into_fresh_file(self):
        block = self._cls._generate_env_marker_block({"AMENT_PREFIX_PATH": "/opt/ros"})
        merged = self._cls._merge_env_into_config("[build]\nrustflags = []\n", block)

        assert "[env]" in merged
        assert "[build]" in merged

    def test_replaces_previous_block(self):
        first = self._cls._generate_env_marker_block({"AMENT_PREFIX_PATH": "/old"})
        content = self._cls._merge_env_into_config("", first)
        second = self._cls._generate_env_marker_block({"AMENT_PREFIX_PATH": "/new"})

        merged = self._cls._merge_env_into_config(content, second)

        assert "/new" in merged
        assert "/old" not in merged

    def test_preserves_user_entries_in_env_section(self):
        existing = '[env]\nMY_VAR = "keep"\n'
        block = self._cls._generate_env_marker_block({"AMENT_PREFIX_PATH": "/opt/ros"})

        merged = self._cls._merge_env_into_config(existing, block)

        assert 'MY_VAR = "keep"' in merged
        assert merged.count("[env]") == 1


# ---------------------------------------------------------------------------
# target-dir redirection
# ---------------------------------------------------------------------------


class TestTargetDir:
    def test_path_under_build_base_and_unique_per_target(self, tmp_path):
        gen = _make_generator(tmp_path)

        a = gen._compute_target_dir(tmp_path / "src" / "pkg_a")
        b = gen._compute_target_dir(tmp_path / "src" / "cargo_ws")

        assert a != b
        for path in (a, b):
            assert str(path).startswith(str(tmp_path / "build"))
            assert "src" not in Path(path).relative_to(tmp_path / "build").parts[1:]

    def test_emitted_in_build_block(self, tmp_path):
        block = WorkspaceBindingGenerator._generate_build_marker_block(
            [], "/ws/build/.cargo_target/x"
        )

        assert 'target-dir = "/ws/build/.cargo_target/x"' in block

    def test_omitted_when_not_requested(self):
        block = WorkspaceBindingGenerator._generate_build_marker_block([], None)

        assert "target-dir" not in block

    def test_user_target_dir_outside_markers_is_respected(self, tmp_path):
        existing = '[build]\ntarget-dir = "/my/own/target"\n'
        assert WorkspaceBindingGenerator._has_user_target_dir(existing) is True

    def test_our_target_dir_is_not_mistaken_for_the_users(self):
        block = WorkspaceBindingGenerator._generate_build_marker_block(
            [], "/ws/build/.cargo_target/x"
        )
        content = WorkspaceBindingGenerator._merge_build_into_config("", block)

        assert WorkspaceBindingGenerator._has_user_target_dir(content) is False


# ---------------------------------------------------------------------------
# .gitignore hygiene
# ---------------------------------------------------------------------------


class TestGitignore:
    def test_writes_marker_block_in_git_worktree(self, tmp_path):
        _git_init(tmp_path)
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        gen = _make_generator(tmp_path)

        gen._ensure_gitignored(crate)

        text = (crate / ".gitignore").read_text()
        assert ".cargo/config.toml" in text
        assert "# BEGIN colcon-cargo-ros2" in text

    def test_idempotent(self, tmp_path):
        _git_init(tmp_path)
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        gen = _make_generator(tmp_path)

        gen._ensure_gitignored(crate)
        gen._ensure_gitignored(crate)

        assert (crate / ".gitignore").read_text().count(".cargo/config.toml") == 1

    def test_preserves_user_entries(self, tmp_path):
        _git_init(tmp_path)
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        (crate / ".gitignore").write_text("*.log\n")
        gen = _make_generator(tmp_path)

        gen._ensure_gitignored(crate)

        assert "*.log" in (crate / ".gitignore").read_text()

    def test_skips_when_already_ignored(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / ".gitignore").write_text(".cargo/\n")
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        gen = _make_generator(tmp_path)

        gen._ensure_gitignored(crate)

        assert not (crate / ".gitignore").exists()

    def test_skips_outside_git(self, tmp_path):
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        gen = _make_generator(tmp_path)

        gen._ensure_gitignored(crate)

        assert not (crate / ".gitignore").exists()

    def test_no_gitignore_option(self, tmp_path):
        _git_init(tmp_path)
        crate = tmp_path / "src" / "pkg_a"
        (crate / ".cargo").mkdir(parents=True)
        (crate / ".cargo" / "config.toml").write_text("")
        gen = _make_generator(tmp_path)
        gen.args.no_gitignore = True

        gen._ensure_gitignored(crate)

        assert not (crate / ".gitignore").exists()


# ---------------------------------------------------------------------------
# End-to-end config content
# ---------------------------------------------------------------------------


class TestWriteCargoConfigs:
    @pytest.fixture
    def workspace(self, tmp_path, monkeypatch):
        """A colcon workspace with one crate depending on one message package."""
        from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

        crate = tmp_path / "src" / "pkg_a"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "pkg_a"\n')
        (crate / "package.xml").write_text("<package><name>pkg_a</name></package>")

        binding = tmp_path / "build" / "my_msgs" / "rosidl_cargo" / "my_msgs"
        binding.mkdir(parents=True)
        (binding / "Cargo.toml").write_text('[package]\nname = "my_msgs"\n')

        _lib_dir(tmp_path, "my_msgs")

        desc = MagicMock()
        desc.name = "pkg_a"
        desc.path = str(crate)
        monkeypatch.setattr(RustBindingAugmentation, "_cargo_descriptors", {"pkg_a": desc})
        monkeypatch.setenv("AMENT_PREFIX_PATH", "")
        return tmp_path, crate

    def test_config_carries_patches_flags_env_and_target_dir(self, workspace, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        tmp_path, crate = workspace
        gen = _make_generator(tmp_path)
        gen._package_all_deps = {"pkg_a": {"my_msgs"}}
        gen._package_interface_deps = {"pkg_a": {"my_msgs"}}

        gen._write_cargo_configs({"my_msgs": tmp_path / "share" / "my_msgs"})

        content = (crate / ".cargo" / "config.toml").read_text()
        assert "[patch.crates-io]" in content
        assert "my_msgs = { path =" in content
        assert "rpath" in content
        assert "[env]" in content
        assert "AMENT_PREFIX_PATH" in content
        assert 'target-dir = "' in content
