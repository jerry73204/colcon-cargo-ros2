# Copyright 2026 colcon-cargo-ros2 contributors
# Licensed under the Apache License, Version 2.0

"""Tests for the console script that stands in for the cargo-ros2 binary.

The wheel ships the PyO3 extension module and no binaries, so `cargo ros2
bindgen|install|clean|doctor` exists only for people who build this repository.
Everyone else gets the same operations through this CLI, which is a thin shell
over the same functions the colcon task calls.
"""

import pytest

from colcon_cargo_ros2 import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingModule:
    """Stands in for cargo_ros2_py, recording what the CLI asked it to do."""

    def __init__(self, doctor_result=True):
        self.calls = []
        self._doctor_result = doctor_result

    def BindgenConfig(self, **kwargs):  # noqa: N802 - mirrors the PyO3 name
        self.calls.append(("BindgenConfig", kwargs))
        return kwargs

    def InstallConfig(self, **kwargs):  # noqa: N802 - mirrors the PyO3 name
        self.calls.append(("InstallConfig", kwargs))
        return kwargs

    def generate_bindings(self, config):
        self.calls.append(("generate_bindings", config))

    def install_to_ament(self, config):
        self.calls.append(("install_to_ament", config))

    def clean_bindings(self, project_root, verbose):
        self.calls.append(("clean_bindings", project_root, verbose))

    def doctor(self, path):
        self.calls.append(("doctor", path))
        return self._doctor_result


@pytest.fixture
def module(monkeypatch):
    recorder = _RecordingModule()
    monkeypatch.setattr(cli, "cargo_ros2_py", recorder)
    return recorder


def _kwargs(recorder, name):
    return next(call[1] for call in recorder.calls if call[0] == name)


# ---------------------------------------------------------------------------
# bindgen
# ---------------------------------------------------------------------------


class TestBindgen:
    def test_generates_for_a_package(self, module, tmp_path):
        rc = cli.main(["bindgen", "--package", "std_msgs", "--output", str(tmp_path)])

        assert rc == 0
        config = _kwargs(module, "BindgenConfig")
        assert config["package_name"] == "std_msgs"
        assert config["output_dir"] == str(tmp_path)
        assert any(call[0] == "generate_bindings" for call in module.calls)

    def test_optional_arguments(self, module, tmp_path):
        cli.main(
            [
                "bindgen",
                "--package",
                "std_msgs",
                "--output",
                str(tmp_path),
                "--package-path",
                "/opt/ros/humble/share/std_msgs",
                "--rosidl-runtime-rs-version",
                "0.5",
                "--verbose",
            ]
        )

        config = _kwargs(module, "BindgenConfig")
        assert config["package_path"] == "/opt/ros/humble/share/std_msgs"
        assert config["rosidl_runtime_rs_version"] == "0.5"
        assert config["verbose"] is True

    def test_failure_is_reported_not_raised(self, module, tmp_path, capsys):
        def explode(_config):
            raise RuntimeError("no such package")

        module.generate_bindings = explode

        rc = cli.main(["bindgen", "--package", "nope", "--output", str(tmp_path)])

        assert rc == 1
        assert "no such package" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class TestInstall:
    def test_defaults_mirror_the_colcon_task(self, module, tmp_path):
        rc = cli.main(["install", "--install-base", str(tmp_path / "install" / "pkg")])

        assert rc == 0
        config = _kwargs(module, "InstallConfig")
        assert config["profile"] == "debug"
        assert config["project_root"] == str(tmp_path.cwd())
        assert config["features"] == []

    def test_profile_and_features(self, module, tmp_path):
        cli.main(
            [
                "install",
                "--install-base",
                str(tmp_path),
                "--profile",
                "release",
                "--features",
                "extra,other",
                "--target",
                "aarch64-unknown-linux-gnu",
            ]
        )

        config = _kwargs(module, "InstallConfig")
        assert config["profile"] == "release"
        assert config["features"] == ["extra", "other"]
        assert config["arch"] == "aarch64-unknown-linux-gnu"


# ---------------------------------------------------------------------------
# clean and doctor
# ---------------------------------------------------------------------------


class TestCleanAndDoctor:
    def test_clean(self, module, tmp_path):
        rc = cli.main(["clean", "--path", str(tmp_path)])

        assert rc == 0
        assert ("clean_bindings", str(tmp_path), False) in module.calls

    def test_doctor_healthy(self, module, tmp_path):
        rc = cli.main(["doctor", str(tmp_path)])

        assert rc == 0
        assert ("doctor", str(tmp_path)) in module.calls

    def test_doctor_unhealthy_exits_nonzero(self, monkeypatch, tmp_path):
        recorder = _RecordingModule(doctor_result=False)
        monkeypatch.setattr(cli, "cargo_ros2_py", recorder)

        assert cli.main(["doctor", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# Shape of the command itself
# ---------------------------------------------------------------------------


class TestCommand:
    def test_no_subcommand_prints_usage(self, module, capsys):
        rc = cli.main([])

        assert rc == 2
        assert "usage" in capsys.readouterr().err.lower()

    def test_every_pyo3_entry_point_is_reachable(self, module, tmp_path):
        """The CLI exists so that nothing is source-checkout-only."""
        cli.main(["bindgen", "--package", "p", "--output", str(tmp_path)])
        cli.main(["install", "--install-base", str(tmp_path)])
        cli.main(["clean", "--path", str(tmp_path)])
        cli.main(["doctor", str(tmp_path)])

        called = {call[0] for call in module.calls}
        assert {"generate_bindings", "install_to_ament", "clean_bindings", "doctor"} <= called
