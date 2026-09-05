from __future__ import annotations

import pytest

from traceart.config import ConfigError, find_config, load_config, resolve


def test_project_config_wins_over_user(tmp_path):
    local = tmp_path / "traceart.toml"
    local.write_text('[defaults]\ntheme = "mono"\n', encoding="utf-8")
    assert find_config(cwd=tmp_path) == local


def test_no_config_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("traceart.config.USER_CONFIG", tmp_path / "absent.toml")
    assert find_config(cwd=tmp_path) is None
    assert load_config(None) == {}


def test_explicit_missing_config_rejected(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        find_config(tmp_path / "nope.toml")


def test_unknown_section_rejected(tmp_path):
    path = tmp_path / "traceart.toml"
    path.write_text('[couleurs]\nrouge = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="section"):
        load_config(path)


def test_invalid_toml_rejected(tmp_path):
    path = tmp_path / "traceart.toml"
    path.write_text("[defaults\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML invalide"):
        load_config(path)


def test_precedence_cli_over_config_over_default():
    config = {"defaults": {"theme": "mono"}}
    assert resolve(config, "defaults", "theme", "dark", "light") == "dark"
    assert resolve(config, "defaults", "theme", None, "light") == "mono"
    assert resolve({}, "defaults", "theme", None, "light") == "light"


def test_falsy_config_value_is_honoured():
    # 0 et False sont des valeurs, pas des absences.
    config = {"defaults": {"tolerance": 0.0}}
    assert resolve(config, "defaults", "tolerance", None, 0.25) == 0.0
