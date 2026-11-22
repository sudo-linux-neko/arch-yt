# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import subprocess

import pytest

from plainbagel_archiver.errors import MediaError
from plainbagel_archiver.media import (
    mux_rendition,
    probe_media,
    validate_media,
    write_chapter_metadata,
)

pytestmark = pytest.mark.ffmpeg


def test_muxes_h264_and_preserves_audio(source_media, tmp_path, logger, ffmpeg_available) -> None:
    video, audio = source_media
    chapters = write_chapter_metadata(
        {
            "duration": 2,
            "chapters": [
                {"start_time": 0, "end_time": 1, "title": "One"},
                {"start_time": 1, "end_time": 2, "title": "Two"},
            ],
        },
        tmp_path,
    )
    output = tmp_path / "out.mp4"
    mux_rendition(
        video_path=video,
        audio_path=audio,
        chapters_path=chapters,
        output_path=output,
        container="mp4",
        expected_codec="h264",
        expected_width=160,
        expected_height=120,
        expected_duration=2,
        video_has_audio=False,
        audio_codec="aac",
        metadata={"title": "test"},
        logger=logger,
    )
    info = validate_media(
        output,
        expected_codec="h264",
        expected_width=160,
        expected_height=120,
        expected_duration=2,
        require_audio=True,
    )
    assert info.audio_codec == "aac"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_chapters",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"title": "One"' in probe.stdout
    assert '"title": "Two"' in probe.stdout
    assert chapters is not None
    assert "FFMETADATA1" in chapters.read_text()


def test_rejects_wrong_expected_codec(source_media, tmp_path, logger, ffmpeg_available) -> None:
    video, audio = source_media
    output = tmp_path / "out.mp4"
    mux_rendition(
        video_path=video,
        audio_path=audio,
        chapters_path=None,
        output_path=output,
        container="mp4",
        expected_codec="h264",
        expected_width=160,
        expected_height=120,
        expected_duration=2,
        video_has_audio=False,
        audio_codec="aac",
        metadata={},
        logger=logger,
    )
    with pytest.raises(MediaError):
        validate_media(
            output,
            expected_codec="av1",
            expected_width=160,
            expected_height=120,
            expected_duration=2,
            require_audio=True,
        )


def test_webm_mux_transcodes_aac_to_opus(
    vp9_source, source_media, tmp_path, logger, ffmpeg_available
) -> None:
    _, audio = source_media
    output = tmp_path / "out.webm"
    mux_rendition(
        video_path=vp9_source,
        audio_path=audio,
        chapters_path=None,
        output_path=output,
        container="webm",
        expected_codec="vp9",
        expected_width=160,
        expected_height=120,
        expected_duration=1,
        video_has_audio=False,
        audio_codec="aac",
        metadata={},
        logger=logger,
    )
    info = probe_media(output)
    assert info.video_codec == "vp9"
    assert info.audio_codec == "opus"


def test_audio_only_probe(source_media, ffmpeg_available) -> None:
    _, audio = source_media
    info = probe_media(audio, require_video=False)
    assert info.has_audio
    assert not info.video_codec
