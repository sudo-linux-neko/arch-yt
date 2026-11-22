# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .config import AppConfig
from .errors import (
    DownloadError,
    EnumerationError,
    VideoLiveError,
    VideoUnavailableError,
    VideoUpcomingError,
)
from .logging_utils import redact
from .utils import atomic_write_bytes, utc_iso

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
EMPTY_TAB_MARKERS = (
    "does not have a",
    "does not have any",
    "there are no",
    "no videos",
    "http error 404",
    "not found",
)


@dataclass(frozen=True)
class ChannelEntry:
    video_id: str
    title: str
    url: str
    tab: str
    duration: int | None = None
    upload_date: str | None = None


@dataclass(frozen=True)
class ChannelSnapshot:
    channel_id: str | None
    title: str
    description: str
    url: str
    entries: tuple[ChannelEntry, ...]
    captured_at: str
    follower_count: int | None = None


@dataclass(frozen=True)
class AuxiliaryFiles:
    thumbnail: Path | None
    subtitles: tuple[Path, ...]
    warnings: tuple[str, ...]


class YtDlpClient:
    def __init__(self, config: AppConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.command = self._find_command()
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            }
        )

    def _find_command(self) -> list[str]:
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        binary = shutil.which("yt-dlp")
        if binary:
            return [binary]
        raise EnumerationError(
            "yt-dlp is not installed. Install requirements.txt in the active Python environment."
        )

    def base_args(self) -> list[str]:
        args = [
            *self.command,
            "--ignore-config",
            "--no-warnings",
            "--socket-timeout",
            str(self.config.socket_timeout),
            "--retries",
            str(self.config.ytdlp_retries),
            "--extractor-retries",
            "3",
            "--no-mtime",
        ]
        if self.config.cookies_file:
            args.extend(["--cookies", str(self.config.cookies_file)])
        if self.config.ytdlp_extractor_args:
            for token in self.config.ytdlp_extractor_args.split(" "):
                args.extend(["--extractor-args", token])
        return args

    def version(self) -> str:
        result = self._run([*self.command, "--version"], timeout=60)
        if result.returncode != 0:
            raise EnumerationError(f"Could not run yt-dlp: {result.stderr.strip()}")
        return result.stdout.strip()

    def list_channel(self) -> ChannelSnapshot:
        entries: dict[str, ChannelEntry] = {}
        channel_metadata: dict[str, Any] = {}
        errors: list[str] = []
        base_url = self.config.channel_url.rstrip("/")
        for tab in self.config.tabs:
            tab_url = f"{base_url}/{tab}"
            self.logger.info("Listing %s", tab_url)
            try:
                payload = self._extract_json(["--flat-playlist", "--dump-single-json", tab_url])
            except EnumerationError as exc:
                message = str(exc)
                if any(marker in message.lower() for marker in EMPTY_TAB_MARKERS):
                    self.logger.warning("Skipping empty or unavailable tab %s: %s", tab, message)
                    continue
                errors.append(f"{tab}: {message}")
                continue
            if not channel_metadata:
                channel_metadata = {
                    key: payload.get(key)
                    for key in (
                        "id",
                        "channel",
                        "channel_id",
                        "title",
                        "description",
                        "uploader",
                        "uploader_id",
                        "channel_follower_count",
                    )
                    if payload.get(key) is not None
                }
            raw_entries = payload.get("entries") or []
            for item in raw_entries:
                if not isinstance(item, dict):
                    continue
                video_id = item.get("id")
                if not isinstance(video_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{11}", video_id
                ):
                    continue
                title = str(item.get("title") or "").strip()
                duration = item.get("duration")
                if isinstance(duration, float):
                    duration = int(duration)
                if not isinstance(duration, int):
                    duration = None
                upload_date = item.get("upload_date")
                if not isinstance(upload_date, str) or not re.fullmatch(r"\d{8}", upload_date):
                    upload_date = None
                entries.setdefault(
                    video_id,
                    ChannelEntry(
                        video_id=video_id,
                        title=title,
                        url=WATCH_URL.format(video_id=video_id),
                        tab=tab,
                        duration=duration,
                        upload_date=upload_date,
                    ),
                )
        if errors and not entries:
            raise EnumerationError("; ".join(errors))
        for error in errors:
            self.logger.warning("One channel tab failed: %s", error)
        channel_id = str(channel_metadata.get("channel_id") or channel_metadata.get("id") or "")
        if channel_id and channel_id != self.config.channel_id:
            raise EnumerationError(
                f"Channel ID mismatch: expected {self.config.channel_id}, got {channel_id}"
            )
        if not entries:
            raise EnumerationError("No videos were found in the configured channel tabs")
        follower_count = channel_metadata.get("channel_follower_count")
        if isinstance(follower_count, bool) or not isinstance(follower_count, int):
            follower_count = None
        snapshot = ChannelSnapshot(
            channel_id=channel_id or None,
            title=str(
                channel_metadata.get("channel")
                or channel_metadata.get("uploader")
                or channel_metadata.get("title")
                or ""
            ),
            description=str(channel_metadata.get("description") or ""),
            url=self.config.channel_url,
            entries=tuple(entries.values()),
            captured_at=utc_iso(),
            follower_count=follower_count,
        )
        self.logger.info("Discovered %d unique public videos", len(snapshot.entries))
        return snapshot

    def fetch_video_info(self, video_id: str) -> dict[str, Any]:
        url = WATCH_URL.format(video_id=video_id)
        info = self._extract_json(["--no-playlist", "-J", url])
        actual_id = info.get("id")
        if actual_id != video_id:
            raise VideoUnavailableError(
                f"yt-dlp returned video {actual_id!r} while requesting {video_id!r}"
            )
        channel_id = info.get("channel_id")
        if channel_id and channel_id != self.config.channel_id:
            raise VideoUnavailableError(
                f"Video {video_id} belongs to channel {channel_id}, not the configured channel"
            )
        live_status = str(info.get("live_status") or "")
        if info.get("is_live") or live_status == "is_live":
            raise VideoLiveError(f"Video {video_id} is currently live; deferring it")
        if live_status == "is_upcoming":
            raise VideoUpcomingError(f"Video {video_id} is an upcoming premiere or stream")
        availability = str(info.get("availability") or "public")
        if availability not in {"public", "unlisted"}:
            raise VideoUnavailableError(
                f"Video {video_id} is not publicly available (availability={availability})"
            )
        if info.get("duration") is None:
            self.logger.warning("Video %s has no duration in yt-dlp metadata", video_id)
        if not isinstance(info.get("description"), str):
            info["description"] = ""
        return info

    def download_format(
        self,
        video_id: str,
        format_id: str,
        output_path: Path,
        *,
        expected_ext: str | None = None,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            output_path.exists()
            and output_path.stat().st_size > 0
            and (expected_ext is None or output_path.suffix.lstrip(".") == expected_ext)
        ):
            return output_path
        url = WATCH_URL.format(video_id=video_id)
        args = [
            *self.base_args(),
            "--no-playlist",
            "--continue",
            "--fragment-retries",
            str(self.config.fragment_retries),
            "--concurrent-fragments",
            str(self.config.concurrent_fragments),
            "--no-write-info-json",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--no-write-thumbnail",
            "--no-progress",
            "-f",
            format_id,
            "-o",
            str(output_path),
            url,
        ]
        self.logger.info("Downloading %s format %s to %s", video_id, format_id, output_path.name)
        result = self._run(args)
        if result.returncode != 0:
            raise DownloadError(
                f"yt-dlp failed for {video_id} format {format_id}: "
                f"{redact(_tail(result.stderr), self._secrets())}"
            )
        candidates = [output_path]
        if expected_ext:
            candidates.append(output_path.with_suffix(f".{expected_ext}"))
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        raise DownloadError(
            f"yt-dlp reported success but no output exists for {video_id} format {format_id}"
        )

    def download_auxiliary(self, info: dict[str, Any], destination: Path) -> AuxiliaryFiles:
        destination.mkdir(parents=True, exist_ok=True)
        video_id = str(info.get("id") or "")
        warnings: list[str] = []
        thumbnail = self._download_thumbnail(info, destination, warnings)
        subtitles = self._download_subtitles(info, destination, warnings)
        if not video_id:
            warnings.append("Missing video ID while downloading auxiliary files")
        return AuxiliaryFiles(
            thumbnail=thumbnail,
            subtitles=tuple(subtitles),
            warnings=tuple(warnings),
        )

    def _download_thumbnail(
        self,
        info: dict[str, Any],
        destination: Path,
        warnings: list[str],
    ) -> Path | None:
        thumbnails = [item for item in info.get("thumbnails") or [] if isinstance(item, dict)]
        if not thumbnails:
            warnings.append("No thumbnail metadata was provided by YouTube")
            return None
        thumbnails.sort(
            key=lambda item: (
                int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("preference") or 0),
            ),
            reverse=True,
        )
        selected = thumbnails[0]
        url = selected.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            warnings.append("Thumbnail URL was invalid")
            return None
        raw_path = destination / "thumbnail.jpg"
        try:
            content = self._http_get(url, max_bytes=30 * 1024 * 1024)
            if content[:3] == b"\xff\xd8\xff":
                atomic_write_bytes(raw_path, content)
                return raw_path
            temp_path = destination / "thumbnail.original"
            atomic_write_bytes(temp_path, content)
            converted = self._convert_thumbnail(temp_path, raw_path)
            temp_path.unlink(missing_ok=True)
            if converted is not None:
                return converted
            warnings.append("Thumbnail could not be converted to JPEG")
            return None
        except Exception as exc:
            warnings.append(f"Thumbnail download failed: {exc}")
            return None

    def _convert_thumbnail(self, source: Path, target: Path) -> Path | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            return None
        return target

    def _download_subtitles(
        self,
        info: dict[str, Any],
        destination: Path,
        warnings: list[str],
    ) -> list[Path]:
        patterns = [
            pattern.strip()
            for pattern in self.config.subtitle_languages.split(",")
            if pattern.strip()
        ]
        outputs: list[Path] = []
        for kind, collection in (
            ("manual", info.get("subtitles") or {}),
            ("auto", info.get("automatic_captions") or {}),
        ):
            if not isinstance(collection, dict):
                continue
            for language, tracks in sorted(collection.items()):
                if not isinstance(language, str) or not _language_matches(language, patterns):
                    continue
                if not isinstance(tracks, list):
                    continue
                track = _select_subtitle_track(tracks)
                if track is None:
                    continue
                url = track.get("url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    continue
                extension = str(track.get("ext") or "vtt")
                safe_language = re.sub(r"[^A-Za-z0-9._-]+", "_", language)
                output = destination / f"captions-{kind}.{safe_language}.{extension}"
                try:
                    content = self._http_get(url, max_bytes=50 * 1024 * 1024)
                    atomic_write_bytes(output, content)
                    outputs.append(output)
                except Exception as exc:
                    warnings.append(f"{kind} captions for {language} failed: {exc}")
        if not outputs:
            warnings.append("No matching English captions were available")
        return outputs

    def _http_get(self, url: str, *, max_bytes: int) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with self.http.get(url, stream=True, timeout=(30, 120)) as response:
                    response.raise_for_status()
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > max_bytes:
                        raise DownloadError(
                            f"Remote file is larger than the {max_bytes} byte limit"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise DownloadError(f"Remote file exceeded the {max_bytes} byte limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    self.logger.debug("HTTP retry %d for %s: %s", attempt, url, exc)
        raise DownloadError(f"HTTP download failed: {last_error}")

    def _extract_json(self, extra_args: list[str]) -> dict[str, Any]:
        result = self._run([*self.base_args(), *extra_args])
        if result.returncode != 0:
            message = _tail(result.stderr) or result.stdout[-1000:]
            raise EnumerationError(redact(message.strip(), self._secrets()))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EnumerationError(
                f"yt-dlp returned invalid JSON: {exc}; output starts with {result.stdout[:300]!r}"
            ) from exc

    def _run(
        self, args: list[str], timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.logger.debug("Running yt-dlp: %s", " ".join(args))
        try:
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise EnumerationError(f"Could not start yt-dlp: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise EnumerationError(f"yt-dlp timed out after {timeout} seconds") from None
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        if stderr:
            for line in stderr.splitlines()[-8:]:
                self.logger.debug("yt-dlp: %s", redact(line, self._secrets()))
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def _secrets(self) -> list[str]:
        return [os.environ.get(name, "") for name in ("IA_ACCESS_KEY", "IA_SECRET_KEY")]


def _select_subtitle_track(tracks: list[Any]) -> dict[str, Any] | None:
    valid = [track for track in tracks if isinstance(track, dict) and track.get("url")]
    if not valid:
        return None
    preference = {"vtt": 0, "srt": 1, "json3": 2}
    valid.sort(key=lambda track: preference.get(str(track.get("ext") or ""), 9))
    return valid[0]


def _language_matches(language: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        try:
            if re.fullmatch(pattern, language):
                return True
        except re.error:
            if language == pattern:
                return True
    return False


def _tail(value: str, limit: int = 2000) -> str:
    return value.strip()[-limit:]


def validate_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").endswith("youtube.com")
