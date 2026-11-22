# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from plainbagel_archiver.config import AppConfig, load_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    config = load_config(ROOT / "config.toml")
    return replace(
        config,
        work_dir=(tmp_path / "work").resolve(),
        min_free_gb=0.0,
        cleanup_after_upload=False,
    )


@pytest.fixture
def logger() -> logging.Logger:
    value = logging.getLogger("plainbagel.tests")
    value.setLevel(logging.DEBUG)
    value.addHandler(logging.NullHandler())
    value.propagate = False
    return value


@pytest.fixture
def ffmpeg_available() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for this test")


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


@pytest.fixture(scope="session")
def source_media(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for this test")
    directory = tmp_path_factory.mktemp("source-media")
    video = directory / "source.mp4"
    audio = directory / "source.m4a"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=10",
        "-t",
        "2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(video),
    )
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        str(audio),
    )
    return video, audio


@pytest.fixture(scope="session")
def vp9_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for this test")
    directory = tmp_path_factory.mktemp("vp9-source")
    path = directory / "source-vp9.webm"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=10",
        "-t",
        "1",
        "-c:v",
        "libvpx-vp9",
        "-b:v",
        "120k",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-an",
        str(path),
    )
    return path
