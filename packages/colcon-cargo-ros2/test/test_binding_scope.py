# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for per-target binding scoping.

A Cargo target should receive ``[patch.crates-io]`` entries only for the
interface packages its own ROS packages declare in package.xml, rather than for
everything anything in the colcon workspace needs. Covers:

- Transitive closure over recorded package.xml dependency edges
- Narrowing patches to the crates of a single Cargo target
- The deliberate fallbacks that keep over-inclusion, never under-inclusion
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_ide_config import _make_cargo_toml, _make_generator, _make_package_xml

from colcon_cargo_ros2.package_augmentation import RustBindingAugmentation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_bindings(tmp_path: Path, packages):
    """Create fake generated binding crates under build/."""
    for pkg in packages:
        d = tmp_path / "build" / pkg / "rosidl_cargo" / pkg
        d.mkdir(parents=True)
        (d / "Cargo.toml").write_text(f'[package]\nname = "{pkg}"\n')


def _make_crate(tmp_path: Path, name: str) -> Path:
    """Create a standalone ROS Cargo crate (its own Cargo workspace)."""
    crate = tmp_path / "src" / name
    _make_cargo_toml(crate / "Cargo.toml", workspace=True)
    _make_package_xml(crate / "package.xml", name)
    return crate


def _descriptors(**name_to_path):
    """Build the augmentation descriptor mapping colcon would have produced."""
    descriptors = {}
    for name, path in name_to_path.items():
        desc = MagicMock()
        desc.path = str(path)
        desc.name = name
        descriptors[name] = desc
    return descriptors


def _ros_packages(*names):
    return {n: Path(f"/opt/ros/jazzy/share/{n}") for n in names}


def _dep(name):
    """A colcon dependency object, which only needs a .name for our purposes."""
    d = MagicMock()
    d.name = name
    return d


def _descriptors_with_deps(**name_to_deps):
    """Descriptors whose get_dependencies() returns the given package.xml deps."""
    descriptors = {}
    for name, deps in name_to_deps.items():
        desc = MagicMock()
        desc.name = name
        desc.path = f"/src/{name}"
        desc.get_dependencies.return_value = [_dep(d) for d in deps]
        descriptors[name] = desc
    return descriptors


def _patch_lines(config: Path):
    """Return the package names patched in a generated config.toml."""
    names = set()
    for line in config.read_text().splitlines():
        if " = { path =" in line:
            names.add(line.split(" = { path =")[0].strip())
    return names


# ---------------------------------------------------------------------------
# _transitive_closure
# ---------------------------------------------------------------------------


class TestTransitiveClosure:
    def test_follows_chain(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._dep_graph = {
            "geometry_msgs": {"std_msgs"},
            "std_msgs": {"builtin_interfaces"},
            "builtin_interfaces": set(),
        }
        assert gen._transitive_closure({"geometry_msgs"}) == {
            "geometry_msgs",
            "std_msgs",
            "builtin_interfaces",
        }

    def test_unknown_package_contributes_itself(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._dep_graph = {}
        assert gen._transitive_closure({"mystery_msgs"}) == {"mystery_msgs"}

    def test_terminates_on_cycle(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._dep_graph = {"a": {"b"}, "b": {"a"}}
        assert gen._transitive_closure({"a"}) == {"a", "b"}

    def test_empty_seeds(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._dep_graph = {"a": {"b"}}
        assert gen._transitive_closure(set()) == set()


# ---------------------------------------------------------------------------
# _select_bindings_for_target
# ---------------------------------------------------------------------------


class TestSelectBindingsForTarget:
    def test_selects_only_declared(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {"a": {"std_msgs"}}
        binding_dirs = {"std_msgs": Path("/b/std"), "nav_msgs": Path("/b/nav")}

        selected = gen._select_bindings_for_target([("a", Path("/src/a"))], binding_dirs)

        assert set(selected) == {"std_msgs"}

    def test_unions_crates_sharing_a_target(self, tmp_path):
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {"a": {"std_msgs"}, "b": {"nav_msgs"}}
        binding_dirs = {
            "std_msgs": Path("/b/std"),
            "nav_msgs": Path("/b/nav"),
            "moveit_msgs": Path("/b/moveit"),
        }

        selected = gen._select_bindings_for_target(
            [("a", Path("/src/a")), ("b", Path("/src/b"))], binding_dirs
        )

        assert set(selected) == {"std_msgs", "nav_msgs"}

    def test_unknown_attribution_falls_back_to_all(self, tmp_path):
        """Absent attribution means unknown, so narrowing must be disabled."""
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {}
        binding_dirs = {"std_msgs": Path("/b/std"), "nav_msgs": Path("/b/nav")}

        selected = gen._select_bindings_for_target([("a", Path("/src/a"))], binding_dirs)

        assert set(selected) == {"std_msgs", "nav_msgs"}

    def test_one_unknown_crate_disables_narrowing_for_the_target(self, tmp_path):
        """A single crate with no attribution must not be narrowed by its neighbour."""
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {"a": {"std_msgs"}}  # 'b' absent
        binding_dirs = {"std_msgs": Path("/b/std"), "nav_msgs": Path("/b/nav")}

        selected = gen._select_bindings_for_target(
            [("a", Path("/src/a")), ("b", Path("/src/b"))], binding_dirs
        )

        assert set(selected) == {"std_msgs", "nav_msgs"}

    def test_empty_attribution_selects_nothing(self, tmp_path):
        """Known-and-empty differs from unknown: a crate needing no messages gets none."""
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {"a": set()}
        binding_dirs = {"std_msgs": Path("/b/std")}

        assert gen._select_bindings_for_target([("a", Path("/src/a"))], binding_dirs) == {}

    def test_missing_binding_raises_naming_package_and_crate(self, tmp_path):
        """A needed package with no generated bindings must fail loudly, not be dropped."""
        gen = _make_generator(tmp_path)
        gen._package_interface_deps = {"a": {"lifecycle_msgs"}}

        with pytest.raises(RuntimeError) as excinfo:
            gen._select_bindings_for_target([("a", Path("/src/a"))], {"std_msgs": Path("/b/std")})

        message = str(excinfo.value)
        assert "lifecycle_msgs" in message
        assert "a" in message
        assert "crates.io" in message


# ---------------------------------------------------------------------------
# Dependency edges recorded against a real ROS installation
# ---------------------------------------------------------------------------


def _ros_available():
    try:
        from ament_index_python.packages import get_package_share_directory

        get_package_share_directory("geometry_msgs")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ros_available(), reason="requires a ROS installation")
class TestDependencyGraphRecording:
    """_resolve_transitive_dependencies must record the edges it walks.

    The per-package attribution is only correct if replaying the recorded graph
    reproduces the walk, so assert exactly that against real package.xml data.
    """

    def test_edges_recorded_while_walking(self, tmp_path):
        gen = _make_generator(tmp_path)

        gen._resolve_transitive_dependencies({"geometry_msgs"})

        assert "std_msgs" in gen._dep_graph["geometry_msgs"]
        assert "builtin_interfaces" in gen._dep_graph["std_msgs"]

    def test_closure_reproduces_the_walk(self, tmp_path):
        gen = _make_generator(tmp_path)

        resolved = gen._resolve_transitive_dependencies({"geometry_msgs"})
        replayed = gen._transitive_closure({"geometry_msgs"})

        assert replayed == resolved
        assert {"geometry_msgs", "std_msgs", "builtin_interfaces"} <= replayed

    def test_closure_is_narrower_than_the_workspace_union(self, tmp_path):
        """The point of the whole change: one package's closure excludes another's deps."""
        gen = _make_generator(tmp_path)

        union = gen._resolve_transitive_dependencies({"geometry_msgs", "sensor_msgs"})
        geometry_only = gen._transitive_closure({"geometry_msgs"})

        assert geometry_only < union
        assert "sensor_msgs" not in geometry_only


# ---------------------------------------------------------------------------
# _discover_ros_packages populates the attribution
# ---------------------------------------------------------------------------


class TestDiscoverBuildsAttribution:
    """The scoping is only as good as the attribution _discover_ros_packages records."""

    def _run_discovery(
        self, gen, descriptors, interface_names, dep_graph=None, workspace_deps=None
    ):
        """Drive _discover_ros_packages with the ROS-dependent steps stubbed out."""

        def fake_resolve(initial):
            # The real implementation records edges while walking; mimic that so
            # the closure step has something to follow.
            gen._dep_graph.update(dep_graph or {})
            return gen._transitive_closure(set(initial))

        gen._resolve_transitive_dependencies = fake_resolve
        gen._find_workspace_interface_packages = lambda required: ({}, set(workspace_deps or ()))
        gen._filter_interface_packages = lambda pkgs: {
            n: Path(f"/opt/ros/jazzy/share/{n}") for n in pkgs if n in interface_names
        }

        RustBindingAugmentation._cargo_descriptors = descriptors
        try:
            return gen._discover_ros_packages()
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_attribution_is_per_package(self, tmp_path):
        gen = _make_generator(tmp_path)
        descriptors = _descriptors_with_deps(
            light_pkg=["std_msgs", "rclrs"],
            heavy_pkg=["nav_msgs"],
        )

        self._run_discovery(gen, descriptors, {"std_msgs", "nav_msgs"})

        assert gen._package_interface_deps == {
            "light_pkg": {"std_msgs"},
            "heavy_pkg": {"nav_msgs"},
        }

    def test_attribution_includes_transitive_interfaces(self, tmp_path):
        gen = _make_generator(tmp_path)
        descriptors = _descriptors_with_deps(my_robot=["geometry_msgs"])

        self._run_discovery(
            gen,
            descriptors,
            {"geometry_msgs", "std_msgs", "builtin_interfaces"},
            dep_graph={
                "geometry_msgs": {"std_msgs"},
                "std_msgs": {"builtin_interfaces"},
            },
        )

        assert gen._package_interface_deps["my_robot"] == {
            "geometry_msgs",
            "std_msgs",
            "builtin_interfaces",
        }

    def test_non_interface_dependencies_are_excluded(self, tmp_path):
        gen = _make_generator(tmp_path)
        descriptors = _descriptors_with_deps(my_robot=["std_msgs", "rclcpp", "ament_cmake"])

        self._run_discovery(gen, descriptors, {"std_msgs"})

        assert gen._package_interface_deps["my_robot"] == {"std_msgs"}

    def test_unclaimed_binding_disables_narrowing(self, tmp_path):
        """A generated binding nobody claims means an edge went unrecorded."""
        gen = _make_generator(tmp_path)
        descriptors = _descriptors_with_deps(my_robot=["std_msgs"])

        # nav_msgs is generated but reachable from no Cargo package, which is only
        # possible if some package.xml dependency edge was missed.
        self._run_discovery(gen, descriptors, {"std_msgs", "nav_msgs"}, workspace_deps={"nav_msgs"})

        assert gen._package_interface_deps == {}

    def test_unreadable_dependencies_leave_package_unattributed(self, tmp_path):
        """A package colcon cannot describe must be absent, so it falls back to all."""
        gen = _make_generator(tmp_path)
        broken = MagicMock()
        broken.name = "broken_pkg"
        broken.path = "/src/broken_pkg"
        broken.get_dependencies.side_effect = RuntimeError("no package.xml")

        descriptors = _descriptors_with_deps(good_pkg=["std_msgs"])
        descriptors["broken_pkg"] = broken

        self._run_discovery(gen, descriptors, {"std_msgs"})

        assert "broken_pkg" not in gen._package_interface_deps
        assert gen._package_interface_deps["good_pkg"] == {"std_msgs"}

        # And that absence must widen, not narrow, the patches written.
        selected = gen._select_bindings_for_target(
            [("broken_pkg", Path("/src/broken_pkg"))],
            {"std_msgs": Path("/b/std"), "nav_msgs": Path("/b/nav")},
        )
        assert set(selected) == {"std_msgs", "nav_msgs"}


# ---------------------------------------------------------------------------
# End-to-end through _write_cargo_configs
# ---------------------------------------------------------------------------


class TestWriteCargoConfigsScoping:
    def test_each_target_gets_only_its_own_patches(self, tmp_path):
        """The regression: one package's dependencies must not leak into another's config."""
        gen = _make_generator(tmp_path)

        light = _make_crate(tmp_path, "light_pkg")
        heavy = _make_crate(tmp_path, "heavy_pkg")
        _setup_bindings(tmp_path, ["std_msgs", "nav_msgs", "moveit_msgs"])

        gen._package_interface_deps = {
            "light_pkg": {"std_msgs"},
            "heavy_pkg": {"nav_msgs", "moveit_msgs"},
        }
        RustBindingAugmentation._cargo_descriptors = _descriptors(light_pkg=light, heavy_pkg=heavy)

        try:
            gen._write_cargo_configs(_ros_packages("std_msgs", "nav_msgs", "moveit_msgs"))

            assert _patch_lines(light / ".cargo" / "config.toml") == {"std_msgs"}
            assert _patch_lines(heavy / ".cargo" / "config.toml") == {
                "nav_msgs",
                "moveit_msgs",
            }
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_transitive_dependencies_are_patched(self, tmp_path):
        """geometry_msgs -> std_msgs -> builtin_interfaces must all be patched."""
        gen = _make_generator(tmp_path)

        crate = _make_crate(tmp_path, "my_robot")
        _setup_bindings(tmp_path, ["geometry_msgs", "std_msgs", "builtin_interfaces", "nav_msgs"])

        gen._dep_graph = {
            "geometry_msgs": {"std_msgs"},
            "std_msgs": {"builtin_interfaces"},
        }
        gen._package_interface_deps = {"my_robot": gen._transitive_closure({"geometry_msgs"})}
        RustBindingAugmentation._cargo_descriptors = _descriptors(my_robot=crate)

        try:
            gen._write_cargo_configs(
                _ros_packages("geometry_msgs", "std_msgs", "builtin_interfaces", "nav_msgs")
            )

            assert _patch_lines(crate / ".cargo" / "config.toml") == {
                "geometry_msgs",
                "std_msgs",
                "builtin_interfaces",
            }
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_crate_needing_no_messages_still_gets_rustflags(self, tmp_path):
        """An empty patch set must not skip the file: rustflags drive linking."""
        gen = _make_generator(tmp_path)

        crate = _make_crate(tmp_path, "pure_rust")
        _setup_bindings(tmp_path, ["std_msgs"])

        gen._package_interface_deps = {"pure_rust": set()}
        RustBindingAugmentation._cargo_descriptors = _descriptors(pure_rust=crate)

        try:
            gen._write_cargo_configs(_ros_packages("std_msgs"))

            config = crate / ".cargo" / "config.toml"
            assert config.exists()
            content = config.read_text()
            assert "std_msgs" not in content
            assert "rustflags" in content
            assert "# BEGIN colcon-cargo-ros2 generated patches" in content
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_dropped_dependency_clears_stale_patch(self, tmp_path):
        """Narrowing between builds must remove the patch that is no longer needed."""
        gen = _make_generator(tmp_path)

        crate = _make_crate(tmp_path, "my_robot")
        _setup_bindings(tmp_path, ["std_msgs", "nav_msgs"])
        RustBindingAugmentation._cargo_descriptors = _descriptors(my_robot=crate)

        try:
            gen._package_interface_deps = {"my_robot": {"std_msgs", "nav_msgs"}}
            gen._write_cargo_configs(_ros_packages("std_msgs", "nav_msgs"))
            assert _patch_lines(crate / ".cargo" / "config.toml") == {"std_msgs", "nav_msgs"}

            # nav_msgs dropped from package.xml on the next build
            gen._package_interface_deps = {"my_robot": {"std_msgs"}}
            gen._write_cargo_configs(_ros_packages("std_msgs", "nav_msgs"))
            assert _patch_lines(crate / ".cargo" / "config.toml") == {"std_msgs"}
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_shared_cargo_workspace_gets_the_union(self, tmp_path):
        """Two ROS packages under one Cargo workspace share one config, so union applies."""
        gen = _make_generator(tmp_path)

        ws = tmp_path / "src" / "robot_ws"
        _make_cargo_toml(ws / "Cargo.toml", workspace=True, extra='members = ["a", "b"]')
        crate_a = ws / "a"
        crate_b = ws / "b"
        for crate, name in ((crate_a, "pkg_a"), (crate_b, "pkg_b")):
            _make_cargo_toml(crate / "Cargo.toml")
            _make_package_xml(crate / "package.xml", name)

        _setup_bindings(tmp_path, ["std_msgs", "nav_msgs", "moveit_msgs"])

        gen._package_interface_deps = {"pkg_a": {"std_msgs"}, "pkg_b": {"nav_msgs"}}
        RustBindingAugmentation._cargo_descriptors = _descriptors(pkg_a=crate_a, pkg_b=crate_b)

        try:
            gen._write_cargo_configs(_ros_packages("std_msgs", "nav_msgs", "moveit_msgs"))

            assert not (crate_a / ".cargo").exists()
            assert _patch_lines(ws / ".cargo" / "config.toml") == {"std_msgs", "nav_msgs"}
        finally:
            RustBindingAugmentation._cargo_descriptors = {}

    def test_no_attribution_patches_everything(self, tmp_path):
        """Without _discover_ros_packages having run, behaviour is the old superset."""
        gen = _make_generator(tmp_path)

        crate = _make_crate(tmp_path, "my_robot")
        _setup_bindings(tmp_path, ["std_msgs", "nav_msgs"])
        RustBindingAugmentation._cargo_descriptors = _descriptors(my_robot=crate)

        try:
            gen._write_cargo_configs(_ros_packages("std_msgs", "nav_msgs"))

            assert _patch_lines(crate / ".cargo" / "config.toml") == {"std_msgs", "nav_msgs"}
        finally:
            RustBindingAugmentation._cargo_descriptors = {}
