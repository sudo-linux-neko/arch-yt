# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DependencyError, MediaError
from .utils import human_duration

FFMPEG_CODEC_NAMES = {
    "h264": "h264",
    "av1": "av1",
    "vp9": "vp9",
    "vp8": "vp8",
}
AUDIO_KEEP_CODECS = {
    "mp4": {"aac", "mp3"},
    "webm": {"opus", "vorbis"},
}


@dataclass(frozen=True)
class MediaInfo:
    video_codec: str
    width: int
    height: int
    duration: float | None
    audio_codec: str | None
    has_audio: bool


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DependencyError(f"{name} is required but was not found on PATH")
    return path


def tool_versions() -> dict[str, str]:
    ffmpeg = require_binary("ffmpeg")
    ffprobe = require_binary("ffprobe")
    ffmpeg_result = subprocess.run(
        [ffmpeg, "-version"], capture_output=True, text=True, check=False
    )
    ffprobe_result = subprocess.run(
        [ffprobe, "-version"], capture_output=True, text=True, check=False
    )
    if ffmpeg_result.returncode != 0 or ffprobe_result.returncode != 0:
        raise DependencyError("ffmpeg or ffprobe could not be executed")
    return {
        "ffmpeg": (ffmpeg_result.stdout.splitlines() or ["unknown"])[0].strip(),
        "ffprobe": (ffprobe_result.stdout.splitlines() or ["unknown"])[0].strip(),
        "ffmpeg_path": ffmpeg,
        "ffprobe_path": ffprobe,
    }


def probe_media(path: Path, *, require_video: bool = True) -> MediaInfo:
    ffprobe = require_binary("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise MediaError(f"ffprobe failed for {path.name}: {result.stderr.strip()[-1000:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}") from exc
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaError(f"ffprobe found no streams in {path.name}")
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not isinstance(video_stream, dict):
        if require_video:
            raise MediaError(f"No video stream found in {path.name}")
        video_stream = {}
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_raw = format_info.get("duration") or video_stream.get("duration")
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    if not video_stream:
        duration_raw = format_info.get("duration")
        try:
            duration = float(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration = None
    return MediaInfo(
        video_codec=str(video_stream.get("codec_name") or ""),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        duration=duration,
        audio_codec=str(audio_stream.get("codec_name") or "") if audio_stream else None,
        has_audio=audio_stream is not None,
    )


def validate_media(
    path: Path,
    *,
    expected_codec: str,
    expected_width: int,
    expected_height: int,
    expected_duration: float | None,
    require_audio: bool,
) -> MediaInfo:
    if not path.is_file() or path.stat().st_size == 0:
        raise MediaError(f"Media file is missing or empty: {path}")
    info = probe_media(path)
    expected_probe_codec = FFMPEG_CODEC_NAMES.get(expected_codec, expected_codec)
    if info.video_codec != expected_probe_codec:
        raise MediaError(
            f"{path.name} has video codec {info.video_codec!r}; expected {expected_probe_codec!r}"
        )
    if (info.width, info.height) != (expected_width, expected_height):
        raise MediaError(
            f"{path.name} is {info.width}x{info.height}; expected "
            f"{expected_width}x{expected_height}"
        )
    if require_audio and not info.has_audio:
        raise MediaError(f"{path.name} has no audio stream")
    if expected_duration and info.duration:
        tolerance = max(5.0, expected_duration * 0.02)
        if abs(info.duration - expected_duration) > tolerance:
            raise MediaError(
                f"{path.name} duration {human_duration(info.duration)} is outside "
                f"the expected {human_duration(expected_duration)} tolerance"
            )
    return info


def write_chapter_metadata(info: dict[str, Any], destination: Path) -> Path | None:
    chapters = info.get("chapters")
    if not isinstance(chapters, list):
        return None
    duration = info.get("duration")
    entries: list[tuple[int, int, str]] = []
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        start = chapter.get("start_time")
        end = chapter.get("end_time")
        title = chapter.get("title")
        if not isinstance(start, (int, float)) or not isinstance(title, str):
            continue
        if not isinstance(end, (int, float)):
            if index + 1 < len(chapters) and isinstance(chapters[index + 1], dict):
                next_start = chapters[index + 1].get("start_time")
                end = next_start if isinstance(next_start, (int, float)) else None
            elif isinstance(duration, (int, float)):
                end = duration
        if not isinstance(end, (int, float)) or end <= start:
            continue
        entries.append((round(start * 1000), round(end * 1000), title))
    if not entries:
        return None
    lines = [";FFMETADATA1"]
    for start, end, title in entries:
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start}",
                f"END={end}",
                f"title={_escape_ffmetadata(title)}",
            ]
        )
    path = destination / "chapters.ffmeta"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def mux_rendition(
    *,
    video_path: Path,
    audio_path: Path | None,
    chapters_path: Path | None,
    output_path: Path,
    container: str,
    expected_codec: str,
    expected_width: int,
    expected_height: int,
    expected_duration: float | None,
    video_has_audio: bool,
    audio_codec: str | None,
    metadata: dict[str, str],
    logger: logging.Logger,
) -> Path:
    ffmpeg = require_binary("ffmpeg")
    if container not in {"mp4", "webm"}:
        raise MediaError(f"Unsupported output container: {container}")
    if audio_path is None and not video_has_audio:
        raise MediaError("No audio stream is available for muxing")
    temp_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    temp_path.unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path)]
    audio_input_index: int | None = None
    if audio_path is not None:
        audio_input_index = 1
        args.extend(["-i", str(audio_path)])
    chapter_input_index: int | None = None
    if chapters_path is not None:
        chapter_input_index = 1 + (1 if audio_path is not None else 0)
        args.extend(["-f", "ffmetadata", "-i", str(chapters_path)])
    args.extend(["-map", "0:v:0"])
    if audio_input_index is not None:
        args.extend(["-map", f"{audio_input_index}:a:0"])
    elif video_has_audio:
        args.extend(["-map", "0:a:0"])
    if chapter_input_index is not None:
        args.extend(["-map_chapters", str(chapter_input_index)])
    args.extend(["-c:v", "copy"])
    if audio_input_index is not None and audio_codec:
        if audio_codec in AUDIO_KEEP_CODECS[container]:
            args.extend(["-c:a", "copy"])
        elif container == "mp4":
            args.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000"])
        else:
            args.extend(["-c:a", "libopus", "-b:a", "192k", "-ar", "48000"])
    elif audio_input_index is None and video_has_audio:
        args.extend(["-c:a", "copy"])
    if container == "mp4":
        args.extend(["-movflags", "+faststart", "-tag:v", _mp4_tag(expected_codec)])
    for key, value in metadata.items():
        args.extend(["-metadata", f"{key}={value}"])
    args.extend(
        [
            "-max_muxing_queue_size",
            "4096",
            "-f",
            container,
            str(temp_path),
        ]
    )
    logger.info("Muxing %s", output_path.name)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=7200,
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise MediaError(
            f"ffmpeg failed while creating {output_path.name}: {result.stderr.strip()[-3000:]}"
        )
    try:
        validate_media(
            temp_path,
            expected_codec=expected_codec,
            expected_width=expected_width,
            expected_height=expected_height,
            expected_duration=expected_duration,
            require_audio=True,
        )
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return output_path


def _mp4_tag(codec: str) -> str:
    if codec == "h264":
        return "avc1"
    if codec == "av1":
        return "av01"
    return codec


def _escape_ffmetadata(value: str) -> str:
    text = value.replace("\\", "\\\\")
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
    return text
