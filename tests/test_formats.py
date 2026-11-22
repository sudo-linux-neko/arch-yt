# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import pytest

from plainbagel_archiver.errors import FormatSelectionError
from plainbagel_archiver.formats import build_archive_plan


def video(
    format_id: str,
    codec: str,
    width: int,
    height: int,
    note: str,
    tbr: float,
    *,
    ext: str | None = None,
) -> dict:
    vcodec = {
        "h264": "avc1.4d401f",
        "av1": "av01.0.05M.08",
        "vp9": "vp09.00.10.08",
        "vp8": "vp08.00.10.08",
    }[codec]
    return {
        "format_id": format_id,
        "vcodec": vcodec,
        "acodec": "none",
        "ext": ext or ("webm" if codec in {"vp9", "vp8"} else "mp4"),
        "width": width,
        "height": height,
        "format_note": note,
        "tbr": tbr,
        "fps": 30,
    }


def audio(format_id: str, codec: str, ext: str, abr: float, note: str = "English") -> dict:
    return {
        "format_id": format_id,
        "vcodec": "none",
        "acodec": {
            "aac": "mp4a.40.2",
            "opus": "opus",
            "vorbis": "vorbis",
        }[codec],
        "ext": ext,
        "abr": abr,
        "format_note": note,
        "language": "en",
    }


def base_formats() -> list[dict]:
    return [
        video("137", "h264", 1920, 1080, "1080p", 4000),
        video("399", "av1", 1920, 1080, "1080p", 2500),
        video("136", "h264", 1280, 720, "720p", 2000),
        video("398", "av1", 1280, 720, "720p", 1400),
        video("135", "h264", 854, 480, "480p", 1000),
        video("397", "av1", 854, 480, "480p", 700),
        audio("140", "aac", "m4a", 128),
        audio("251", "opus", "webm", 160),
    ]


def test_selects_four_renditions_and_master() -> None:
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": base_formats()})
    assert [item.slot for item in plan.renditions] == [
        "720p-h264",
        "720p-av1",
        "480p-h264",
        "480p-av1",
    ]
    assert [item.filename for item in plan.renditions] == [
        "video-720p-h264.mp4",
        "video-720p-av1.mp4",
        "video-480p-h264.mp4",
        "video-480p-av1.mp4",
    ]
    assert plan.master is not None
    assert plan.master.filename == "master-1080p-av1.mp4"
    assert [item.audio.codec for item in plan.renditions] == ["aac", "aac", "aac", "aac"]


def test_falls_back_to_vp9_and_webm() -> None:
    formats = [
        video("136", "h264", 1280, 720, "720p", 2000),
        video("135", "h264", 854, 480, "480p", 1000),
        video("247", "vp9", 1280, 720, "720p", 1500),
        video("244", "vp9", 854, 480, "480p", 700),
        audio("140", "aac", "m4a", 128),
        audio("251", "opus", "webm", 160),
    ]
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": formats})
    av1_720 = next(item for item in plan.renditions if item.slot == "720p-av1")
    assert av1_720.video.codec == "vp9"
    assert av1_720.container == "webm"
    assert av1_720.filename == "video-720p-vp9.webm"
    assert av1_720.audio is not None and av1_720.audio.codec == "opus"
    assert any("VP9" in warning for warning in plan.warnings)


def test_vertical_short_uses_standard_480p_stream() -> None:
    formats = [
        video("399", "av1", 1080, 1920, "1080p", 1800),
        video("232", "h264", 720, 1280, "720p", 1500),
        video("231", "h264", 480, 854, "480p", 1200),
        video("398", "av1", 720, 1280, "720p", 1000),
        video("397", "av1", 480, 854, "480p", 500),
        video("788", "av1", 608, 1080, "480p", 600),
        audio("140", "aac", "m4a", 128),
    ]
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": formats})
    selected = next(item for item in plan.renditions if item.slot == "480p-av1")
    assert selected.video.format_id == "397"
    assert (selected.video.width, selected.video.height) == (480, 854)


def test_skips_unavailable_requested_slot_without_transcoding() -> None:
    formats = [
        video("399", "av1", 1920, 1080, "1080p", 2500),
        video("398", "av1", 1280, 720, "720p", 1400),
        video("135", "h264", 854, 480, "480p", 1000),
        video("397", "av1", 854, 480, "480p", 700),
        audio("140", "aac", "m4a", 128),
    ]
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": formats})
    assert "720p-h264" not in {item.slot for item in plan.renditions}
    assert any("720p-h264" in warning for warning in plan.warnings)


def test_uses_lower_source_when_slot_is_unused() -> None:
    formats = [
        video("399", "av1", 1920, 1080, "1080p", 2500),
        video("398", "av1", 1280, 720, "720p", 1400),
        video("397", "av1", 854, 480, "480p", 700),
        video("134", "h264", 640, 360, "360p", 400),
        audio("140", "aac", "m4a", 128),
    ]
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": formats})
    h264 = next(item for item in plan.renditions if item.slot == "720p-h264")
    assert h264.video.quality_height == 360
    assert h264.filename == "video-720p-h264-from-360p.mp4"
    assert h264.fallback_reason is not None


def test_no_audio_is_an_error() -> None:
    formats = [
        video("399", "av1", 1920, 1080, "1080p", 2500),
        video("398", "av1", 1280, 720, "720p", 1400),
        video("397", "av1", 854, 480, "480p", 700),
    ]
    with pytest.raises(FormatSelectionError):
        build_archive_plan({"id": "abc", "duration": 60, "formats": formats})


def test_no_video_is_an_error() -> None:
    with pytest.raises(FormatSelectionError):
        build_archive_plan({"id": "abc", "formats": [audio("140", "aac", "m4a", 128)]})


def test_prefers_non_drc_audio() -> None:
    formats = [
        video("136", "h264", 1280, 720, "720p", 2000),
        audio("140-drc", "aac", "m4a", 129.53, "medium, DRC"),
        audio("140", "aac", "m4a", 129.529, "medium"),
    ]
    plan = build_archive_plan({"id": "abc", "duration": 60, "formats": formats})
    assert plan.renditions[0].audio.format_id == "140"
