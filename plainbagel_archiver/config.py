# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from .constants import (
    DEFAULT_ARCHIVER_NAME,
    DEFAULT_CLEANUP,
    DEFAULT_COLLECTION,
    DEFAULT_IDENTIFIER_PREFIX,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_FREE_GB,
    DEFAULT_SESSION_MINUTES,
    DEFAULT_SUB_LANGS,
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRIES_SLEEP,
    DEFAULT_UPLOAD_TIMEOUT,
    DEFAULT_VERIFY_POLL,
    DEFAULT_VERIFY_TIMEOUT,
    DEFAULT_YTDLP_EXTRACTOR_ARGS,
)
from .errors import ConfigError

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{4,79}$")
PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,40}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_TABS = frozenset({"videos", "shorts", "streams"})
KNOWN_KEYS = frozenset(
    {
        "channel_url",
        "channel_id",
        "channel_name",
        "creator_name",
        "creator_email",
        "archiver_email",
        "archiver_name",
        "ytdlp_extractor_args",
        "tabs",
        "collection",
        "identifier_prefix",
        "work_dir",
        "cookies_file",
        "permission_acknowledged",
        "cleanup_after_upload",
        "max_attempts",
        "session_minutes",
        "min_free_gb",
        "ytdlp_retries",
        "fragment_retries",
        "socket_timeout",
        "concurrent_fragments",
        "subtitle_languages",
        "upload_retries",
        "upload_retries_sleep",
        "upload_timeout_seconds",
        "verify_timeout_seconds",
        "verify_poll_seconds",
    }
)


@dataclass(frozen=True)
class ArchiveCredentials:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class AppConfig:
    channel_url: str
    channel_id: str
    channel_name: str
    creator_name: str
    creator_email: str
    archiver_email: str
    archiver_name: str
    ytdlp_extractor_args: str
    tabs: tuple[str, ...]
    collection: str
    identifier_prefix: str
    work_dir: Path
    cookies_file: Path | None
    permission_acknowledged: bool
    cleanup_after_upload: bool
    max_attempts: int
    session_minutes: int
    min_free_gb: float
    ytdlp_retries: int
    fragment_retries: int
    socket_timeout: int
    concurrent_fragments: int
    subtitle_languages: str
    upload_retries: int
    upload_retries_sleep: int
    upload_timeout_seconds: int
    verify_timeout_seconds: int
    verify_poll_seconds: int
    config_path: Path

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def downloads_dir(self) -> Path:
        return self.work_dir / "videos"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "archive.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "runner.lock"

    @property
    def pause_path(self) -> Path:
        return self.state_dir / "PAUSE"

    def with_overrides(
        self,
        *,
        work_dir: Path | None = None,
        collection: str | None = None,
        cookies_file: Path | None = None,
        permission_acknowledged: bool | None = None,
        cleanup_after_upload: bool | None = None,
        session_minutes: int | None = None,
        subtitle_languages: str | None = None,
    ) -> AppConfig:
        changes: dict[str, Any] = {}
        if work_dir is not None:
            changes["work_dir"] = work_dir.expanduser().resolve()
        if collection is not None:
            _validate_identifier(collection, "collection")
            changes["collection"] = collection
        if cookies_file is not None:
            resolved = cookies_file.expanduser().resolve()
            if not resolved.is_file():
                raise ConfigError(f"Cookies file does not exist: {resolved}")
            changes["cookies_file"] = resolved
        if permission_acknowledged is not None:
            changes["permission_acknowledged"] = permission_acknowledged
        if cleanup_after_upload is not None:
            changes["cleanup_after_upload"] = cleanup_after_upload
        if session_minutes is not None:
            if session_minutes < 1:
                raise ConfigError("session_minutes must be at least 1")
            changes["session_minutes"] = session_minutes
        if subtitle_languages is not None:
            if not subtitle_languages.strip():
                raise ConfigError("subtitle_languages cannot be empty")
            changes["subtitle_languages"] = subtitle_languages.strip()
        return replace(self, **changes)


def load_config(path: Path) -> AppConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ConfigError(f"Config file not found: {resolved}")
    try:
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {resolved}: {exc}") from exc

    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ConfigError(f"Unknown config keys: {', '.join(unknown)}")

    base = resolved.parent

    def text(key: str, default: str | None = None) -> str:
        value = raw.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty string")
        return value.strip()

    def integer(key: str, default: int, minimum: int = 0) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ConfigError(f"{key} must be an integer >= {minimum}")
        return value

    def number(key: str, default: float, minimum: float = 0.0) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
            raise ConfigError(f"{key} must be a number >= {minimum}")
        return float(value)

    def boolean(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false")
        return value

    channel_url = text("channel_url")
    parsed = urlparse(channel_url)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").endswith(
        "youtube.com"
    ):
        raise ConfigError("channel_url must be a YouTube URL")
    channel_id = text("channel_id")
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ConfigError("channel_id does not look like a YouTube channel ID")

    tabs_value = raw.get("tabs", ["videos", "shorts", "streams"])
    if not isinstance(tabs_value, list) or not tabs_value:
        raise ConfigError("tabs must be a non-empty list")
    tabs = tuple(str(item).strip() for item in tabs_value)
    invalid_tabs = sorted(set(tabs) - ALLOWED_TABS)
    if invalid_tabs:
        raise ConfigError(f"Unsupported tabs: {', '.join(invalid_tabs)}")
    if len(set(tabs)) != len(tabs):
        raise ConfigError("tabs contains duplicates")

    collection = text("collection", DEFAULT_COLLECTION)
    _validate_identifier(collection, "collection")
    prefix = text("identifier_prefix", DEFAULT_IDENTIFIER_PREFIX)
    if not PREFIX_RE.fullmatch(prefix):
        raise ConfigError(
            "identifier_prefix must be 2-41 lowercase letters, digits, dots, underscores, or dashes"
        )

    creator_email = text("creator_email")
    archiver_email = text("archiver_email")
    archiver_name = text("archiver_name", DEFAULT_ARCHIVER_NAME)
    raw_extractor_args = raw.get("ytdlp_extractor_args", DEFAULT_YTDLP_EXTRACTOR_ARGS)
    if not isinstance(raw_extractor_args, str):
        raise ConfigError("ytdlp_extractor_args must be a string")
    ytdlp_extractor_args = " ".join(raw_extractor_args.split())
    for token in ytdlp_extractor_args.split(" "):
        if token and ":" not in token:
            raise ConfigError(
                f"ytdlp_extractor_args tokens look like 'youtube:key=value1,value2': got {token!r}"
            )
    for key, value in (("creator_email", creator_email), ("archiver_email", archiver_email)):
        if not EMAIL_RE.fullmatch(value):
            raise ConfigError(f"{key} is not a valid email address")

    work_dir = Path(text("work_dir")).expanduser()
    work_dir = (base / work_dir).resolve() if not work_dir.is_absolute() else work_dir.resolve()

    cookies_file_value = raw.get("cookies_file", "")
    if not isinstance(cookies_file_value, str):
        raise ConfigError("cookies_file must be a string")
    env_cookies = os.environ.get("YT_COOKIES_FILE", "").strip()
    cookies_file: Path | None = None
    selected_cookies = env_cookies or cookies_file_value.strip()
    if selected_cookies:
        cookies_path = Path(selected_cookies).expanduser()
        if not cookies_path.is_absolute():
            cookies_path = (base / cookies_path).resolve()
        else:
            cookies_path = cookies_path.resolve()
        if not cookies_path.is_file():
            raise ConfigError(f"Cookies file does not exist: {cookies_path}")
        cookies_file = cookies_path

    subtitle_languages = text("subtitle_languages", DEFAULT_SUB_LANGS)
    config = AppConfig(
        channel_url=channel_url,
        channel_id=channel_id,
        channel_name=text("channel_name"),
        creator_name=text("creator_name"),
        creator_email=creator_email,
        archiver_email=archiver_email,
        archiver_name=archiver_name,
        ytdlp_extractor_args=ytdlp_extractor_args,
        tabs=tabs,
        collection=collection,
        identifier_prefix=prefix,
        work_dir=work_dir,
        cookies_file=cookies_file,
        permission_acknowledged=boolean("permission_acknowledged", False),
        cleanup_after_upload=boolean("cleanup_after_upload", DEFAULT_CLEANUP),
        max_attempts=integer("max_attempts", DEFAULT_MAX_ATTEMPTS, minimum=1),
        session_minutes=integer("session_minutes", DEFAULT_SESSION_MINUTES, minimum=1),
        min_free_gb=number("min_free_gb", DEFAULT_MIN_FREE_GB),
        ytdlp_retries=integer("ytdlp_retries", 10, minimum=0),
        fragment_retries=integer("fragment_retries", 10, minimum=0),
        socket_timeout=integer("socket_timeout", 30, minimum=5),
        concurrent_fragments=integer("concurrent_fragments", 4, minimum=1),
        subtitle_languages=subtitle_languages,
        upload_retries=integer("upload_retries", DEFAULT_UPLOAD_RETRIES, minimum=0),
        upload_retries_sleep=integer(
            "upload_retries_sleep", DEFAULT_UPLOAD_RETRIES_SLEEP, minimum=1
        ),
        upload_timeout_seconds=integer(
            "upload_timeout_seconds", DEFAULT_UPLOAD_TIMEOUT, minimum=30
        ),
        verify_timeout_seconds=integer(
            "verify_timeout_seconds", DEFAULT_VERIFY_TIMEOUT, minimum=30
        ),
        verify_poll_seconds=integer("verify_poll_seconds", DEFAULT_VERIFY_POLL, minimum=5),
        config_path=resolved,
    )
    return config


def _validate_identifier(value: str, label: str) -> None:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ConfigError(
            f"{label} must be 5-80 lowercase letters, digits, dots, underscores, or dashes "
            "and start with a letter or digit"
        )


def load_credentials(required: bool = True) -> ArchiveCredentials | None:
    access = os.environ.get("IA_ACCESS_KEY", "").strip()
    secret = os.environ.get("IA_SECRET_KEY", "").strip()
    if not access and not secret:
        if required:
            raise ConfigError(
                "Internet Archive credentials are missing. Set IA_ACCESS_KEY and "
                "IA_SECRET_KEY from https://archive.org/account/s3.php"
            )
        return None
    if not access or not secret:
        raise ConfigError("Both IA_ACCESS_KEY and IA_SECRET_KEY must be set")
    if len(access) < 8 or len(secret) < 8:
        raise ConfigError("Internet Archive credentials appear to be malformed")
    return ArchiveCredentials(access_key=access, secret_key=secret)
