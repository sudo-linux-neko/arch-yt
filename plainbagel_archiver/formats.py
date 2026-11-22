# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .constants import (
    CODEC_FALLBACKS,
    MASTER_CODEC_PREFERENCE,
    RENDITION_TARGETS,
)
from .errors import FormatSelectionError

QUALITY_RE = re.compile(r"(?<!\d)(\d{3,4})p(?:\d{2,3})?(?!\d)", re.IGNORECASE)
SAFE_LANGUAGE_RE = re.compile(r"[^A-Za-z0-9._-]+")

CODEC_NAMES = {
    "avc1": "h264",
    "avc3": "h264",
    "h264": "h264",
    "av01": "av1",
    "av1": "av1",
    "vp09": "vp9",
    "vp9": "vp9",
    "vp08": "vp8",
    "vp8": "vp8",
}

CONTAINER_FOR_CODEC = {
    "h264": "mp4",
    "av1": "mp4",
    "vp9": "webm",
    "vp8": "webm",
}


@dataclass(frozen=True)
class VideoFormat:
    format_id: str
    codec: str
    ext: str
    quality_height: int
    width: int
    height: int
    fps: float | None
    tbr: float | None
    filesize: int | None
    has_audio: bool
    format_note: str
    language: str | None

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def size_key(self) -> float:
        return self.tbr or 0.0


@dataclass(frozen=True)
class AudioFormat:
    format_id: str
    codec: str
    ext: str
    abr: float | None
    language: str | None
    is_original: bool
    language_preference: int
    is_drc: bool = False

    @property
    def quality_key(self) -> tuple[int, int, int, float]:
        return (
            1 if self.is_original else 0,
            0 if self.is_drc else 1,
            self.language_preference,
            self.abr or 0.0,
        )


@dataclass(frozen=True)
class RenditionPlan:
    slot: str
    requested_height: int
    requested_codec: str
    video: VideoFormat
    audio: AudioFormat | None
    container: str
    filename: str
    fallback_reason: str | None

    @property
    def output_extension(self) -> str:
        return self.container

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "requested_height": self.requested_height,
            "requested_codec": self.requested_codec,
            "actual_codec": self.video.codec,
            "actual_quality_height": self.video.quality_height,
            "actual_width": self.video.width,
            "actual_height": self.video.height,
            "fps": self.video.fps,
            "video_format_id": self.video.format_id,
            "video_ext": self.video.ext,
            "video_tbr": self.video.tbr,
            "audio_format_id": self.audio.format_id if self.audio else None,
            "audio_codec": self.audio.codec if self.audio else None,
            "audio_language": self.audio.language if self.audio else None,
            "container": self.container,
            "filename": self.filename,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class ArchivePlan:
    renditions: tuple[RenditionPlan, ...]
    master: RenditionPlan | None
    warnings: tuple[str, ...]

    def expected_filenames(self) -> tuple[str, ...]:
        names = [rendition.filename for rendition in self.renditions]
        if self.master:
            names.append(self.master.filename)
        return tuple(names)

    def as_dict(self) -> dict[str, Any]:
        return {
            "renditions": [rendition.as_dict() for rendition in self.renditions],
            "master": self.master.as_dict() if self.master else None,
            "warnings": list(self.warnings),
        }


def build_archive_plan(info: dict[str, Any]) -> ArchivePlan:
    formats = info.get("formats")
    if not isinstance(formats, list) or not formats:
        raise FormatSelectionError("yt-dlp returned no downloadable formats")
    videos = _video_formats(formats)
    audios = _audio_formats(formats, str(info.get("language") or "en"))
    if not videos:
        raise FormatSelectionError("No non-storyboard video formats were available")
    if not audios and not any(video.has_audio for video in videos):
        raise FormatSelectionError("No compatible audio stream was available")

    warnings: list[str] = []
    used: set[str] = set()
    renditions_by_slot: dict[str, RenditionPlan] = {}
    slots: list[tuple[str, int, str, tuple[str, ...]]] = []
    for height, requested_codec in RENDITION_TARGETS:
        slots.append(
            (
                f"{height}p-{requested_codec}",
                height,
                requested_codec,
                CODEC_FALLBACKS[requested_codec],
            )
        )

    for slot, height, requested_codec, codecs in slots:
        selection = _pick_exact(videos, height, codecs, used)
        if selection is None:
            continue
        video, reason = selection
        used.add(video.format_id)
        rendition = _make_rendition(
            slot=slot,
            requested_height=height,
            requested_codec=requested_codec,
            video=video,
            audio=_pick_audio(audios, CONTAINER_FOR_CODEC[video.codec]),
            fallback_reason=reason,
        )
        renditions_by_slot[slot] = rendition

    for slot, height, requested_codec, codecs in slots:
        if slot in renditions_by_slot:
            continue
        selection = _pick_lower(videos, height, codecs, used)
        if selection is None:
            warnings.append(
                f"No source stream was available for the {slot} rendition; it was skipped"
            )
            continue
        video, reason = selection
        used.add(video.format_id)
        rendition = _make_rendition(
            slot=slot,
            requested_height=height,
            requested_codec=requested_codec,
            video=video,
            audio=_pick_audio(audios, CONTAINER_FOR_CODEC[video.codec]),
            fallback_reason=reason,
        )
        renditions_by_slot[slot] = rendition
        warnings.append(reason)

    for rendition in list(renditions_by_slot.values()):
        if rendition.video.codec != rendition.requested_codec:
            warnings.append(
                f"{rendition.slot} used {rendition.video.codec.upper()} because no "
                f"{rendition.requested_codec.upper()} stream was available"
            )
        if rendition.video.quality_height < rendition.requested_height:
            warnings.append(
                f"{rendition.slot} fell back to {rendition.video.quality_height}p source"
            )

    master = _build_master(videos, audios)
    if master and master.video.format_id in used:
        warnings.append(
            "The preservation master uses the same source stream as one requested rendition"
        )
    return ArchivePlan(
        renditions=tuple(
            renditions_by_slot[slot] for slot, _, _, _ in slots if slot in renditions_by_slot
        ),
        master=master,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _build_master(
    videos: list[VideoFormat],
    audios: list[AudioFormat],
) -> RenditionPlan | None:
    if not videos:
        return None
    codec_rank = {codec: index for index, codec in enumerate(MASTER_CODEC_PREFERENCE)}
    best = max(
        videos,
        key=lambda video: (
            video.quality_height,
            video.pixels,
            -codec_rank.get(video.codec, len(codec_rank)),
            video.size_key,
            video.fps or 0.0,
        ),
    )
    return _make_rendition(
        slot="master",
        requested_height=best.quality_height,
        requested_codec=best.codec,
        video=best,
        audio=_pick_audio(audios, CONTAINER_FOR_CODEC[best.codec]),
        fallback_reason=None,
    )


def _make_rendition(
    *,
    slot: str,
    requested_height: int,
    requested_codec: str,
    video: VideoFormat,
    audio: AudioFormat | None,
    fallback_reason: str | None,
) -> RenditionPlan:
    container = CONTAINER_FOR_CODEC[video.codec]
    if slot == "master":
        filename = f"master-{video.quality_height}p-{video.codec}.{container}"
    else:
        suffix = ""
        if video.quality_height != requested_height:
            suffix = f"-from-{video.quality_height}p"
        filename = f"video-{requested_height}p-{video.codec}{suffix}.{container}"
    return RenditionPlan(
        slot=slot,
        requested_height=requested_height,
        requested_codec=requested_codec,
        video=video,
        audio=audio,
        container=container,
        filename=filename,
        fallback_reason=fallback_reason,
    )


def _pick_exact(
    videos: list[VideoFormat],
    height: int,
    codecs: tuple[str, ...],
    used: set[str],
) -> tuple[VideoFormat, str | None] | None:
    for codec_index, codec in enumerate(codecs):
        candidates = [
            video
            for video in videos
            if video.format_id not in used
            and video.quality_height == height
            and video.codec == codec
        ]
        if candidates:
            selected = max(candidates, key=_video_preference)
            reason = None
            if codec_index > 0:
                reason = (
                    f"Requested {codecs[0].upper()} was unavailable at {height}p; "
                    f"used {codec.upper()} at the same resolution"
                )
            return selected, reason
    return None


def _pick_lower(
    videos: list[VideoFormat],
    target_height: int,
    codecs: tuple[str, ...],
    used: set[str],
) -> tuple[VideoFormat, str | None] | None:
    lower_heights = sorted(
        {video.quality_height for video in videos if video.quality_height < target_height},
        reverse=True,
    )
    for height in lower_heights:
        for codec in codecs:
            candidates = [
                video
                for video in videos
                if video.format_id not in used
                and video.quality_height == height
                and video.codec == codec
            ]
            if candidates:
                selected = max(candidates, key=_video_preference)
                reason = (
                    f"Requested {target_height}p was unavailable; used {height}p "
                    f"{codec.upper()} as the closest lower-resolution source"
                )
                return selected, reason
    return None


def _video_preference(video: VideoFormat) -> tuple[int, int, float, int, float]:
    short_side = min(video.width, video.height)
    return (
        0 if not video.has_audio else 1,
        1 if short_side == video.quality_height else 0,
        video.size_key,
        video.pixels,
        video.fps or 0.0,
    )


def _video_formats(raw_formats: list[Any]) -> list[VideoFormat]:
    result: list[VideoFormat] = []
    for raw in raw_formats:
        if not isinstance(raw, dict):
            continue
        format_id = str(raw.get("format_id") or "")
        vcodec = str(raw.get("vcodec") or "none").lower()
        acodec = str(raw.get("acodec") or "none").lower()
        ext = str(raw.get("ext") or "")
        if not format_id or vcodec == "none" or ext == "mhtml":
            continue
        if format_id.startswith("sb"):
            continue
        codec = _normalize_codec(vcodec)
        if codec is None:
            continue
        width = _positive_int(raw.get("width"))
        height = _positive_int(raw.get("height"))
        if width == 0 or height == 0:
            continue
        quality_height = _quality_height(raw, width, height)
        if quality_height <= 0:
            continue
        result.append(
            VideoFormat(
                format_id=format_id,
                codec=codec,
                ext=ext,
                quality_height=quality_height,
                width=width,
                height=height,
                fps=_float_or_none(raw.get("fps")),
                tbr=_float_or_none(raw.get("tbr")),
                filesize=_int_or_none(raw.get("filesize") or raw.get("filesize_approx")),
                has_audio=acodec != "none",
                format_note=str(raw.get("format_note") or ""),
                language=str(raw.get("language") or "") or None,
            )
        )
    return result


def _audio_formats(raw_formats: list[Any], video_language: str) -> list[AudioFormat]:
    result: list[AudioFormat] = []
    for raw in raw_formats:
        if not isinstance(raw, dict):
            continue
        format_id = str(raw.get("format_id") or "")
        vcodec = str(raw.get("vcodec") or "none").lower()
        acodec = str(raw.get("acodec") or "none").lower()
        if not format_id or vcodec != "none" or acodec == "none":
            continue
        codec = _normalize_audio_codec(acodec)
        if codec is None:
            continue
        note = str(raw.get("format_note") or "").lower()
        language = str(raw.get("language") or "") or None
        preference = _int_or_none(raw.get("language_preference")) or 0
        is_original = (
            "original" in note
            or "default" in note
            or preference > 0
            or bool(
                language
                and video_language
                and language.split("-")[0] == video_language.split("-")[0]
            )
        )
        result.append(
            AudioFormat(
                format_id=format_id,
                codec=codec,
                ext=str(raw.get("ext") or ""),
                abr=_float_or_none(raw.get("abr")),
                language=language,
                is_original=is_original,
                language_preference=preference,
                is_drc="drc" in note,
            )
        )
    return result


def _pick_audio(audios: list[AudioFormat], container: str) -> AudioFormat | None:
    preferred = ("aac", "opus", "vorbis") if container == "mp4" else ("opus", "vorbis", "aac")
    for codec in preferred:
        candidates = [audio for audio in audios if audio.codec == codec]
        if candidates:
            return max(candidates, key=lambda audio: audio.quality_key)
    return None


def _quality_height(raw: dict[str, Any], width: int, height: int) -> int:
    note = str(raw.get("format_note") or "")
    match = QUALITY_RE.search(note)
    if match:
        return int(match.group(1))
    return min(width, height)


def _normalize_codec(value: str) -> str | None:
    lowered = value.lower()
    for prefix, codec in CODEC_NAMES.items():
        if lowered.startswith(prefix):
            return codec
    return None


def _normalize_audio_codec(value: str) -> str | None:
    lowered = value.lower()
    if lowered.startswith(("mp4a", "aac")):
        return "aac"
    if lowered.startswith("opus"):
        return "opus"
    if lowered.startswith("vorbis"):
        return "vorbis"
    if lowered.startswith("mp3"):
        return "mp3"
    return None


def _positive_int(value: Any) -> int:
    result = _int_or_none(value)
    return max(0, result or 0)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
