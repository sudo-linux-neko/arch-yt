# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

from pathlib import Path

import pytest

from plainbagel_archiver.config import load_config, load_credentials
from plainbagel_archiver.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_loads_project_config() -> None:
    config = load_config(ROOT / "config.toml")
    assert config.channel_id == "UCFCEuCsyWP0YkP3CZ3Mr01Q"
    assert config.channel_name == "The Plain Bagel"
    assert config.creator_email == "richard@theplainbagel.com"
    assert config.archiver_email == "nekomeowpurr@proton.me"
    assert config.archiver_name == "vaibhav"
    assert config.collection == "opensource_movies"
    assert config.tabs == ("videos", "shorts", "streams")
    assert config.work_dir.is_absolute()
    assert config.permission_acknowledged
    assert config.upload_timeout_seconds >= 1800


def test_unknown_key_is_rejected(tmp_path) -> None:
    source = (ROOT / "config.toml").read_text(encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(source + "\nunknown_key = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Unknown config keys"):
        load_config(path)


def test_relative_work_dir_is_relative_to_config(tmp_path) -> None:
    source = (ROOT / "config.toml").read_text(encoding="utf-8")
    source = source.replace('work_dir = "./work"', 'work_dir = "./data/work"')
    path = tmp_path / "config.toml"
    path.write_text(source, encoding="utf-8")
    config = load_config(path)
    assert config.work_dir == (tmp_path / "data" / "work").resolve()


def test_invalid_override_collection() -> None:
    config = load_config(ROOT / "config.toml")
    with pytest.raises(ConfigError):
        config.with_overrides(collection="Not A Valid Collection")


def test_extractor_args_parsed_from_project_config() -> None:
    config = load_config(ROOT / "config.toml")
    assert config.ytdlp_extractor_args.startswith("youtube:player_client=")


def test_extractor_args_optional_and_normalised(tmp_path) -> None:
    source = (ROOT / "config.toml").read_text(encoding="utf-8")
    source = source.replace(
        'ytdlp_extractor_args = "youtube:player_client=tv_simply,android_vr,web_safari"',
        "",
    )
    path = tmp_path / "config.toml"
    path.write_text(source, encoding="utf-8")
    config = load_config(path)
    assert config.ytdlp_extractor_args == ""


def test_extractor_args_must_contain_colon(tmp_path) -> None:
    source = (ROOT / "config.toml").read_text(encoding="utf-8")
    source = source.replace(
        'ytdlp_extractor_args = "youtube:player_client=tv_simply,android_vr,web_safari"',
        'ytdlp_extractor_args = "not-a-valid-arg"',
    )
    path = tmp_path / "config.toml"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError, match="ytdlp_extractor_args"):
        load_config(path)


def test_extractor_args_rejects_non_string(tmp_path) -> None:
    source = (ROOT / "config.toml").read_text(encoding="utf-8")
    source = source.replace(
        'ytdlp_extractor_args = "youtube:player_client=tv_simply,android_vr,web_safari"',
        "ytdlp_extractor_args = 5",
    )
    path = tmp_path / "config.toml"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a string"):
        load_config(path)


def test_credentials_from_env(monkeypatch) -> None:
    monkeypatch.setenv("IA_ACCESS_KEY", "access123")
    monkeypatch.setenv("IA_SECRET_KEY", "secret123")
    credentials = load_credentials()
    assert credentials is not None
    assert credentials.access_key == "access123"
    assert credentials.secret_key == "secret123"


def test_missing_or_partial_credentials(monkeypatch) -> None:
    monkeypatch.delenv("IA_ACCESS_KEY", raising=False)
    monkeypatch.delenv("IA_SECRET_KEY", raising=False)
    assert load_credentials(required=False) is None
    with pytest.raises(ConfigError):
        load_credentials()
    monkeypatch.setenv("IA_ACCESS_KEY", "access123")
    with pytest.raises(ConfigError):
        load_credentials()
