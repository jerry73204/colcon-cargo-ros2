# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for binding cache invalidation and missing-binding detection.

Both behaviours exist because of one real failure: a workspace whose
``ComponentEvent.msg`` gained a ``pid`` field kept reusing bindings generated
sixteen weeks earlier, and a package whose bindings were never generated fell
back to crates.io and failed with ``version 1.2.1 is yanked``. Neither symptom
named the actual problem.

Covers:
- interface digest changes with content, size and set of definition files
- digest is stable across unrelated files and directory ordering
- stamp comparison treats missing/unreadable stamps as stale
- missing bindings raise, naming every affected package
"""

import os
import pathlib
import sys
import types
from pathlib import Path

import pytest

# The module under test imports the compiled PyO3 extension at import time.
# Nothing exercised here touches it, so stub it when it is absent (running
# against the source tree rather than an installed wheel) instead of requiring
# a full maturin build to run pure-Python tests.
try:  # pragma: no cover - depends on how the suite is invoked
    from colcon_cargo_ros2 import cargo_ros2_py  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules.setdefault("colcon_cargo_ros2.cargo_ros2_py", types.ModuleType("cargo_ros2_py"))

from colcon_cargo_ros2.workspace_bindgen import (  # noqa: E402
    STAMP_FILENAME,
    WorkspaceBindingGenerator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pkg(share: Path, msgs: dict = None, srvs: dict = None) -> Path:
    """Create a package share tree with the given interface definitions."""
    for subdir, contents in (("msg", msgs), ("srv", srvs)):
        if not contents:
            continue
        d = share / subdir
        d.mkdir(parents=True, exist_ok=True)
        for name, body in contents.items():
            (d / name).write_text(body)
    return share


def _stamp(share: Path) -> str:
    return WorkspaceBindingGenerator._interface_stamp(share)


def _touch_distinct(path: Path, body: str):
    """Rewrite *path* so its mtime differs from the previous write.

    Filesystem mtime granularity is coarse enough that a same-size rewrite in
    the same nanosecond window would not register; bump it explicitly so the
    test asserts the intended behaviour rather than timing luck.
    """
    path.write_text(body)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


# ---------------------------------------------------------------------------
# Interface digest
# ---------------------------------------------------------------------------


def test_stamp_is_stable_for_unchanged_definitions(tmp_path):
    share = _make_pkg(tmp_path / "pkg", msgs={"A.msg": "int32 x\n"})
    assert _stamp(share) == _stamp(share)


def test_stamp_changes_when_a_field_is_added(tmp_path):
    """The regression that motivated this: ComponentEvent.msg gained a field."""
    share = _make_pkg(tmp_path / "pkg", msgs={"ComponentEvent.msg": "uint64 unique_id\n"})
    before = _stamp(share)
    _touch_distinct(share / "msg" / "ComponentEvent.msg", "uint64 unique_id\nint32 pid\n")
    assert _stamp(share) != before


def test_stamp_changes_when_a_definition_is_added(tmp_path):
    share = _make_pkg(tmp_path / "pkg", msgs={"A.msg": "int32 x\n"})
    before = _stamp(share)
    (share / "msg" / "B.msg").write_text("int32 y\n")
    assert _stamp(share) != before


def test_stamp_changes_when_a_definition_is_removed(tmp_path):
    share = _make_pkg(tmp_path / "pkg", msgs={"A.msg": "int32 x\n", "B.msg": "int32 y\n"})
    before = _stamp(share)
    (share / "msg" / "B.msg").unlink()
    assert _stamp(share) != before


def test_stamp_covers_services_as_well_as_messages(tmp_path):
    share = _make_pkg(tmp_path / "pkg", msgs={"A.msg": "int32 x\n"}, srvs={"S.srv": "---\n"})
    before = _stamp(share)
    _touch_distinct(share / "srv" / "S.srv", "int32 req\n---\nint32 resp\n")
    assert _stamp(share) != before


def test_stamp_ignores_non_interface_files(tmp_path):
    """Package metadata churn must not force pointless regeneration."""
    share = _make_pkg(tmp_path / "pkg", msgs={"A.msg": "int32 x\n"})
    before = _stamp(share)
    (share / "msg" / "README.txt").write_text("not an interface\n")
    (share / "package.xml").write_text("<package/>\n")
    assert _stamp(share) == before


def test_stamp_of_package_without_interfaces_is_constant(tmp_path):
    a = (tmp_path / "a").resolve()
    b = (tmp_path / "b").resolve()
    a.mkdir()
    b.mkdir()
    assert _stamp(a) == _stamp(b)


def test_stamp_distinguishes_same_name_in_different_subdirs(tmp_path):
    """A path component change must register, not just the basename."""
    one = _make_pkg(tmp_path / "one", msgs={"A.msg": "int32 x\n"})
    two = _make_pkg(tmp_path / "two", srvs={"A.msg": "int32 x\n"})
    assert _stamp(one) != _stamp(two)


# ---------------------------------------------------------------------------
# Stamp comparison
# ---------------------------------------------------------------------------


def test_stamp_ignores_a_touch_that_changes_nothing(tmp_path):
    """A checkout, a copy, or a `touch` rewrites mtimes without changing content.

    Keying freshness on mtime meant every one of those made perfectly good
    bindings look stale, and the escape hatch was a blunt environment variable.
    """
    share = _make_pkg(tmp_path / "share", msgs={"A.msg": "int32 x\n"})
    before = _stamp(share)

    os.utime(share / "msg" / "A.msg", (1_000_000, 1_000_000))

    assert _stamp(share) == before


def test_stamp_notices_content_changing_under_the_same_mtime(tmp_path):
    """Same size, same mtime, different bytes -- what stat-based records missed.

    The digests are memoised on the stat signature within a process, so this is
    the across-builds contract: each colcon invocation reads the files once.
    """
    from colcon_cargo_ros2 import workspace_bindgen

    share = _make_pkg(tmp_path / "share", msgs={"A.msg": "int32 x\n"})
    path = share / "msg" / "A.msg"
    stat = path.stat()
    before = _stamp(share)

    path.write_text("int32 y\n")  # identical length
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    workspace_bindgen._FILE_DIGESTS.clear()  # a new build starts here

    assert _stamp(share) != before


def test_within_one_build_the_stat_signature_is_trusted(tmp_path):
    """A file rewritten mid-build, preserving size and mtime, is not re-read.

    Deliberate: the memo is what keeps repeated generation passes cheap on a
    large workspace. The next build reads it and notices.
    """
    share = _make_pkg(tmp_path / "share", msgs={"A.msg": "int32 x\n"})
    path = share / "msg" / "A.msg"
    stat = path.stat()
    before = _stamp(share)

    path.write_text("int32 y\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert _stamp(share) == before


def test_digest_is_reused_for_an_unchanged_file(tmp_path):
    """Hashing is memoised per process, so repeated stamps cost a stat."""
    from colcon_cargo_ros2 import workspace_bindgen

    share = _make_pkg(tmp_path / "share", msgs={"A.msg": "int32 x\n"})
    workspace_bindgen._FILE_DIGESTS.clear()

    _stamp(share)
    assert len(workspace_bindgen._FILE_DIGESTS) == 1

    reads = []
    original = pathlib.Path.read_bytes

    def counting_read(self):
        reads.append(self)
        return original(self)

    try:
        pathlib.Path.read_bytes = counting_read
        _stamp(share)
    finally:
        pathlib.Path.read_bytes = original

    assert reads == []


def test_stamp_matches_after_write(tmp_path):
    stamp_file = tmp_path / STAMP_FILENAME
    WorkspaceBindingGenerator._write_stamp(stamp_file, "abc123")
    assert WorkspaceBindingGenerator._stamp_matches(stamp_file, "abc123")


def test_missing_stamp_is_stale(tmp_path):
    """Bindings generated before stamping existed must be regenerated once."""
    assert not WorkspaceBindingGenerator._stamp_matches(tmp_path / STAMP_FILENAME, "abc123")


def test_different_stamp_is_stale(tmp_path):
    stamp_file = tmp_path / STAMP_FILENAME
    WorkspaceBindingGenerator._write_stamp(stamp_file, "old")
    assert not WorkspaceBindingGenerator._stamp_matches(stamp_file, "new")


def test_unreadable_stamp_is_stale(tmp_path):
    """A directory where the stamp should be must not raise."""
    stamp_file = tmp_path / STAMP_FILENAME
    stamp_file.mkdir()
    assert not WorkspaceBindingGenerator._stamp_matches(stamp_file, "abc123")


def test_write_stamp_creates_parent_directory(tmp_path):
    stamp_file = tmp_path / "nested" / "deeper" / STAMP_FILENAME
    WorkspaceBindingGenerator._write_stamp(stamp_file, "abc123")
    assert WorkspaceBindingGenerator._stamp_matches(stamp_file, "abc123")


# ---------------------------------------------------------------------------
# Missing-binding detection
# ---------------------------------------------------------------------------


def test_no_missing_bindings_is_silent():
    required = {"std_msgs": Path("/share/std_msgs")}
    generated = {"std_msgs": Path("/build/std_msgs")}
    WorkspaceBindingGenerator._assert_no_missing_bindings(required, generated)


def test_missing_binding_raises_naming_the_package():
    """The lifecycle_msgs case: declared, never generated, silently omitted."""
    required = {
        "std_msgs": Path("/share/std_msgs"),
        "lifecycle_msgs": Path("/share/lifecycle_msgs"),
    }
    generated = {"std_msgs": Path("/build/std_msgs")}
    with pytest.raises(RuntimeError, match="lifecycle_msgs"):
        WorkspaceBindingGenerator._assert_no_missing_bindings(required, generated)


def test_missing_binding_error_names_every_package():
    required = {n: Path(f"/share/{n}") for n in ("a_msgs", "b_msgs", "c_msgs")}
    with pytest.raises(RuntimeError) as excinfo:
        WorkspaceBindingGenerator._assert_no_missing_bindings(required, {})
    message = str(excinfo.value)
    assert "a_msgs" in message and "b_msgs" in message and "c_msgs" in message


def test_missing_binding_error_explains_the_crates_io_fallback():
    """The message must connect the cause to the confusing symptom."""
    with pytest.raises(RuntimeError) as excinfo:
        WorkspaceBindingGenerator._assert_no_missing_bindings(
            {"lifecycle_msgs": Path("/share/lifecycle_msgs")}, {}
        )
    message = str(excinfo.value)
    assert "crates.io" in message
    assert "yanked" in message


def test_extra_generated_bindings_are_not_an_error():
    """Generating more than required (e.g. transitive deps) is fine."""
    required = {"std_msgs": Path("/share/std_msgs")}
    generated = {
        "std_msgs": Path("/build/std_msgs"),
        "builtin_interfaces": Path("/build/builtin_interfaces"),
    }
    WorkspaceBindingGenerator._assert_no_missing_bindings(required, generated)


def test_no_required_packages_is_silent():
    WorkspaceBindingGenerator._assert_no_missing_bindings({}, {})
