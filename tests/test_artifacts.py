# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import json
import shutil

import pytest

from plainbagel_archiver.artifacts import BundleBuilder, remove_video_workdir
from plainbagel_archiver.errors import MediaError
from plainbagel_archiver.formats import ArchivePlan, AudioFormat, RenditionPlan, VideoFormat
from plainbagel_archiver.youtube import AuxiliaryFiles

pytestmark = pytest.mark.ffmpeg


class FakeYoutube:
    def __init__(self, video: str, audio: str):
        self.video = video
        self.audio = audio
        self.calls: list[str] = []

    def download_format(self, video_id, format_id, output_path, *, expected_ext=None):
        self.calls.append(format_id)
        source = self.video if format_id == "v" else self.audio
        shutil.copy2(source, output_path)
        return output_path


def make_plan(info: dict) -> ArchivePlan:
    video = VideoFormat(
        format_id="v",
        codec="h264",
        ext="mp4",
        quality_height=120,
        width=160,
        height=120,
        fps=10,
        tbr=100,
        filesize=1000,
        has_audio=False,
        format_note="120p",
        language=None,
    )
    audio = AudioFormat(
        format_id="a",
        codec="aac",
        ext="m4a",
        abr=96,
        language="en",
        is_original=True,
        language_preference=10,
    )
    rendition = RenditionPlan(
        slot="120p-h264",
        requested_height=120,
        requested_codec="h264",
        video=video,
        audio=audio,
        container="mp4",
        filename="video-120p-h264.mp4",
        fallback_reason=None,
    )
    return ArchivePlan(renditions=(rendition,), master=None, warnings=())


def test_builds_resumable_bundle_and_manifest(
    app_config, source_media, tmp_path, logger, ffmpeg_available
) -> None:
    video, audio = source_media
    youtube = FakeYoutube(str(video), str(audio))
    info = {
        "id": "AbC_-123xyZ",
        "title": "Test",
        "description": "Description",
        "upload_date": "2024-02-03",
        "duration": 2,
    }
    thumbnail = tmp_path / "thumb.jpg"
    subtitle = tmp_path / "captions-auto.en.vtt"
    thumbnail.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 20)
    subtitle.write_text("WEBVTT\n", encoding="utf-8")
    builder = BundleBuilder(app_config, youtube, logger)
    bundle = builder.build(
        info,
        make_plan(info),
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
        auxiliary=AuxiliaryFiles(thumbnail=thumbnail, subtitles=(subtitle,), warnings=()),
    )
    names = {artifact.remote_name for artifact in bundle.artifacts}
    assert {
        "video-120p-h264.mp4",
        "DESCRIPTION.txt",
        "RIGHTS-AND-ATTRIBUTION.txt",
        "metadata.json",
        "source.info.json",
        "thumbnail.jpg",
        "captions-auto.en.vtt",
        "archive-manifest.json",
        "SHA256SUMS",
    } <= names
    assert bundle.total_size > 0
    manifest = json.loads((bundle.directory / "archive-manifest.json").read_text())
    assert manifest["video_id"] == info["id"]
    assert manifest["files"]
    assert manifest["checksum_file"]["name"] == "SHA256SUMS"
    checksum_lines = (bundle.directory / "SHA256SUMS").read_text().splitlines()
    assert len(checksum_lines) == len(bundle.artifacts) - 2
    assert all(len(line.split("  ")[0]) == 64 for line in checksum_lines)
    first_names = {artifact.remote_name for artifact in bundle.artifacts}
    assert "master" not in " ".join(first_names)

    second = builder.build(
        info,
        make_plan(info),
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
        auxiliary=AuxiliaryFiles(thumbnail=thumbnail, subtitles=(subtitle,), warnings=()),
    )
    assert youtube.calls.count("v") == 1
    assert youtube.calls.count("a") == 1
    assert second.total_size == bundle.total_size


def test_remove_video_workdir_rejects_escape(app_config) -> None:
    with pytest.raises(MediaError):
        remove_video_workdir(app_config, "../escape")
