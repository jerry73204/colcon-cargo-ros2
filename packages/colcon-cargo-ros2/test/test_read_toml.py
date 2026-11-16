"""Cargo.toml reading fails loudly (no silent empty manifest)."""

import builtins
from pathlib import Path

import pytest

from colcon_cargo_ros2 import workspace_bindgen as wb


def test_a_readable_manifest_parses(tmp_path):
    p = tmp_path / "Cargo.toml"
    p.write_text('[workspace]\nmembers = ["a"]\n')
    assert wb._read_toml(p)["workspace"]["members"] == ["a"]
    assert wb._cargo_toml_has_workspace(p)


def test_a_broken_manifest_names_the_file(tmp_path):
    p = tmp_path / "Cargo.toml"
    p.write_text("[workspace\nmembers = [\n")
    with pytest.raises(wb.CargoTomlError, match=str(p)):
        wb._read_toml(p)


def test_no_toml_parser_is_an_error_naming_tomli(tmp_path, monkeypatch):
    p = tmp_path / "Cargo.toml"
    p.write_text("[workspace]\n")
    real = builtins.__import__

    def no_toml(name, *a, **kw):
        if name in ("tomllib", "tomli"):
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_toml)
    with pytest.raises(wb.CargoTomlError, match="pip install tomli"):
        wb._read_toml(p)


def test_tomli_is_a_dependency_before_python_3_11():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "tomli>=1.1; python_version < '3.11'" in text
