# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0
"""``colcon build --merge-install``: one install prefix, libraries in ``install/lib``.

colcon hands each package ``install/<pkg>`` in the isolated layout but ``install``
itself under ``--merge-install``. Deriving the install directory as the parent of
that in both cases made the workspace root look like the install directory, so no
``-L`` named ``install/lib`` and a crate using this workspace's interfaces failed
with ``unable to find library -l<pkg>__rosidl_typesupport_c``.
"""

from pathlib import Path

from colcon_cargo_ros2.workspace_bindgen import (
    WorkspaceBindingGenerator,
    install_layout,
    is_merged_install,
)


class FakeArgs:
    rosidl_runtime_rs_version = None


def _merged_workspace(root: Path) -> Path:
    """A merged install holding one interface package's C libraries."""
    install = root / "install"
    (install / "lib").mkdir(parents=True)
    (install / "share" / "colcon-core" / "packages").mkdir(parents=True)
    (install / "share" / "colcon-core" / "packages" / "my_msgs").write_text("")
    (install / "lib" / "libmy_msgs__rosidl_typesupport_c.so").write_text("elf")
    (root / "build").mkdir()
    return install


def _isolated_workspace(root: Path) -> Path:
    install = root / "install"
    lib = install / "my_msgs" / "lib"
    lib.mkdir(parents=True)
    (install / "my_msgs" / "share" / "colcon-core" / "packages").mkdir(parents=True)
    (lib / "libmy_msgs__rosidl_typesupport_c.so").write_text("elf")
    (root / "build").mkdir()
    return install


class TestInstallLayout:
    def test_isolated_takes_the_parent(self, tmp_path):
        install_dir, ws, merged = install_layout(tmp_path / "install" / "my_node", "my_node")
        assert (install_dir, ws, merged) == (tmp_path / "install", tmp_path, False)

    def test_merged_is_the_install_directory_itself(self, tmp_path):
        install_dir, ws, merged = install_layout(tmp_path / "install", "my_node")
        assert (install_dir, ws, merged) == (tmp_path / "install", tmp_path, True)

    def test_detects_the_tree(self, tmp_path):
        assert is_merged_install(_merged_workspace(tmp_path / "m"))
        assert not is_merged_install(_isolated_workspace(tmp_path / "i"))


class TestMergedPrefixes:
    def _gen(self, root: Path, merge_install=None) -> WorkspaceBindingGenerator:
        install = _merged_workspace(root)
        return WorkspaceBindingGenerator(
            root, root / "build", install, FakeArgs(), merge_install=merge_install
        )

    def test_links_install_lib(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        gen = self._gen(tmp_path, merge_install=True)
        lib = (tmp_path / "install" / "lib").absolute()
        assert gen._library_search_dirs(None) == [lib]
        # narrowing by package has nothing to narrow in one shared prefix
        assert gen._library_search_dirs({"my_msgs"}) == [lib]

    def test_tells_the_layout_from_the_tree(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        gen = self._gen(tmp_path)
        assert gen.merge_install
        assert gen._library_search_dirs(None) == [(tmp_path / "install" / "lib").absolute()]

    def test_env_names_the_install_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AMENT_PREFIX_PATH", "/opt/ros/humble")
        gen = self._gen(tmp_path, merge_install=True)
        prefixes = gen._compute_env()["AMENT_PREFIX_PATH"].split(":")
        assert prefixes == [str((tmp_path / "install").absolute()), "/opt/ros/humble"]

    def test_relative_rpath_reaches_install_lib(self, tmp_path, monkeypatch):
        """An installed binary is install/lib/<pkg>/<binary>: two levels below install."""
        monkeypatch.setattr("sys.platform", "linux")
        gen = self._gen(tmp_path, merge_install=True)
        rpaths = gen._relative_rpaths(tmp_path / "install" / "lib")
        assert rpaths[0] == "$ORIGIN/../../lib"
        installed = tmp_path / "install" / "lib" / "my_node"
        assert (installed / "../../lib").resolve() == (tmp_path / "install" / "lib").resolve()


class TestIsolatedUnchanged:
    def test_per_package_prefixes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AMENT_PREFIX_PATH", raising=False)
        install = _isolated_workspace(tmp_path)
        gen = WorkspaceBindingGenerator(tmp_path, tmp_path / "build", install, FakeArgs())
        assert not gen.merge_install
        assert gen._library_search_dirs(None) == [(install / "my_msgs" / "lib").absolute()]
        rpaths = gen._relative_rpaths(install / "my_msgs" / "lib")
        assert rpaths[0] == "$ORIGIN/../../../my_msgs/lib"
