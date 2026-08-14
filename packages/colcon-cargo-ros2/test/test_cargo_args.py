# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for Phase 7: parsing the cargo arguments the installer depends on.

Covers:
- --features / -F / --all-features / --no-default-features
- --target and $CARGO_BUILD_TARGET
- locating the cargo executable for the preflight check
"""

import pytest

from colcon_cargo_ros2.task.ament_cargo.build import (
    detect_cargo_features,
    detect_cargo_target,
    find_cargo_executable,
)

# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


class TestDetectCargoFeatures:
    """Feature selection mirrors what cargo itself accepts."""

    def test_no_arguments_selects_defaults(self):
        assert detect_cargo_features([]) == ([], False, False)

    def test_none_arguments_selects_defaults(self):
        assert detect_cargo_features(None) == ([], False, False)

    def test_space_separated_flag(self):
        assert detect_cargo_features(["--features", "extra"]) == (["extra"], False, False)

    def test_equals_form(self):
        assert detect_cargo_features(["--features=extra"]) == (["extra"], False, False)

    def test_comma_separated_list(self):
        features, _, _ = detect_cargo_features(["--features", "a,b,c"])
        assert features == ["a", "b", "c"]

    def test_whitespace_separated_list(self):
        features, _, _ = detect_cargo_features(["--features", "a b"])
        assert features == ["a", "b"]

    def test_short_flag(self):
        assert detect_cargo_features(["-F", "extra"]) == (["extra"], False, False)

    def test_repeated_flags_accumulate(self):
        features, _, _ = detect_cargo_features(["--features", "a", "--features=b", "-F", "c"])
        assert features == ["a", "b", "c"]

    def test_duplicates_are_collapsed(self):
        features, _, _ = detect_cargo_features(["--features", "a,a,b"])
        assert features == ["a", "b"]

    def test_no_default_features(self):
        assert detect_cargo_features(["--no-default-features"]) == ([], True, False)

    def test_all_features(self):
        assert detect_cargo_features(["--all-features"]) == ([], False, True)

    def test_trailing_features_flag_without_value(self):
        # Malformed input must not raise, since it reaches us straight from
        # the user's --cargo-args.
        assert detect_cargo_features(["--features"]) == ([], False, False)

    def test_unrelated_arguments_are_ignored(self):
        assert detect_cargo_features(["--release", "--target-dir", "build"]) == (
            [],
            False,
            False,
        )


# ---------------------------------------------------------------------------
# Target triple
# ---------------------------------------------------------------------------


class TestDetectCargoTarget:
    """The target triple decides which build subdirectory holds artifacts."""

    def test_no_target_is_none(self):
        assert detect_cargo_target([], env={}) is None

    def test_space_separated_flag(self):
        assert (
            detect_cargo_target(["--target", "aarch64-unknown-linux-gnu"], env={})
            == "aarch64-unknown-linux-gnu"
        )

    def test_equals_form(self):
        assert (
            detect_cargo_target(["--target=aarch64-unknown-linux-gnu"], env={})
            == "aarch64-unknown-linux-gnu"
        )

    def test_falls_back_to_cargo_build_target(self):
        assert (
            detect_cargo_target([], env={"CARGO_BUILD_TARGET": "riscv64gc-unknown-linux-gnu"})
            == "riscv64gc-unknown-linux-gnu"
        )

    def test_explicit_flag_wins_over_environment(self):
        assert (
            detect_cargo_target(
                ["--target", "aarch64-unknown-linux-gnu"],
                env={"CARGO_BUILD_TARGET": "riscv64gc-unknown-linux-gnu"},
            )
            == "aarch64-unknown-linux-gnu"
        )

    def test_trailing_target_flag_without_value(self):
        assert detect_cargo_target(["--target"], env={}) is None

    def test_none_arguments(self):
        assert detect_cargo_target(None, env={}) is None


# ---------------------------------------------------------------------------
# Toolchain preflight
# ---------------------------------------------------------------------------


class TestFindCargoExecutable:
    """The build fails early and clearly when cargo is missing."""

    def test_returns_path_when_cargo_is_installed(self, monkeypatch):
        monkeypatch.setattr(
            "colcon_cargo_ros2.task.ament_cargo.build.shutil.which",
            lambda name: "/usr/bin/cargo" if name == "cargo" else None,
        )
        assert find_cargo_executable() == "/usr/bin/cargo"

    def test_returns_none_when_cargo_is_missing(self, monkeypatch):
        monkeypatch.setattr(
            "colcon_cargo_ros2.task.ament_cargo.build.shutil.which",
            lambda name: None,
        )
        assert find_cargo_executable() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
