# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

from dataclasses import replace

from test_config import ROOT

from plainbagel_archiver.config import load_config
from plainbagel_archiver.youtube import YtDlpClient


def test_base_args_include_extractor_args(tmp_path, logger) -> None:
    config = load_config(ROOT / "config.toml")
    client = YtDlpClient(replace(config, work_dir=(tmp_path / "work").resolve()), logger)
    assert "--extractor-args" in " ".join(client.base_args())


def test_base_args_omits_extractor_flags_when_unset(tmp_path, logger) -> None:
    config = load_config(ROOT / "config.toml")
    config = replace(config, ytdlp_extractor_args="", work_dir=(tmp_path / "work").resolve())
    client = YtDlpClient(config, logger)
    assert "--extractor-args" not in " ".join(client.base_args())


def test_base_args_passes_cookies_file(tmp_path, logger) -> None:
    config = load_config(ROOT / "config.toml")
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("", encoding="utf-8")
    config = replace(
        config,
        ytdlp_extractor_args="",
        cookies_file=cookies,
        work_dir=(tmp_path / "work").resolve(),
    )
    client = YtDlpClient(config, logger)
    args = client.base_args()
    assert args[args.index("--cookies") + 1] == str(cookies)
