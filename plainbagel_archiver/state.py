# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import StateError
from .utils import utc_iso

SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PREPARED = "prepared"
STATUS_UPLOADING = "uploading"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_LIVE = "live"
STATUS_UPCOMING = "upcoming"
STATUS_UNAVAILABLE = "unavailable"

ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_PREPARED, STATUS_UPLOADING)
RETRYABLE_STATUSES = (
    STATUS_PENDING,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_LIVE,
    STATUS_UPCOMING,
)
TERMINAL_STATUSES = (STATUS_COMPLETE, STATUS_UNAVAILABLE)


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    url: str
    title: str
    upload_date: str | None
    duration: int | None
    tab: str | None
    status: str
    item_id: str | None
    attempts: int
    last_error: str | None
    first_seen_at: str
    updated_at: str
    last_checked_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class DiscoveredVideo:
    video_id: str
    url: str
    title: str
    upload_date: str | None = None
    duration: int | None = None
    tab: str | None = None


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> StateStore:
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            self._connection = connection
            self._migrate()
        except sqlite3.Error as exc:
            raise StateError(f"Could not open state database {self.path}: {exc}") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.open()
        assert self._connection is not None
        return self._connection

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StateError(
                f"State database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            with self.connection:
                self.connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS videos (
                        video_id TEXT PRIMARY KEY,
                        url TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        upload_date TEXT,
                        duration INTEGER,
                        tab TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        item_id TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        first_seen_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_checked_at TEXT,
                        completed_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_videos_status
                        ON videos(status, attempts, first_seen_at);
                    CREATE INDEX IF NOT EXISTS idx_videos_item_id ON videos(item_id);
                    """
                )
                self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def reset_stale(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connection:
            cursor = self.connection.execute(
                f"""
                UPDATE videos
                SET status = ?, updated_at = ?, last_error = ?
                WHERE status IN ({placeholders})
                """,
                (
                    STATUS_INTERRUPTED,
                    utc_iso(),
                    "Previous process stopped before the video finished",
                    *ACTIVE_STATUSES,
                ),
            )
        return cursor.rowcount

    def upsert_many(self, videos: Iterable[DiscoveredVideo]) -> int:
        now = utc_iso()
        rows = [
            (
                video.video_id,
                video.url,
                video.title,
                video.upload_date,
                video.duration,
                video.tab,
                now,
                now,
            )
            for video in videos
        ]
        if not rows:
            return 0
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO videos (
                    video_id, url, title, upload_date, duration, tab,
                    status, attempts, first_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    url = excluded.url,
                    title = CASE
                        WHEN excluded.title <> '' THEN excluded.title
                        ELSE videos.title
                    END,
                    upload_date = COALESCE(excluded.upload_date, videos.upload_date),
                    duration = COALESCE(excluded.duration, videos.duration),
                    tab = COALESCE(excluded.tab, videos.tab),
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get(self, video_id: str) -> VideoRecord | None:
        row = self.connection.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
        return _record(row) if row else None

    def list_all(self) -> list[VideoRecord]:
        rows = self.connection.execute(
            "SELECT * FROM videos ORDER BY first_seen_at, video_id"
        ).fetchall()
        return [_record(row) for row in rows]

    def candidates(
        self,
        *,
        max_attempts: int,
        limit: int | None = None,
        only_ids: Iterable[str] | None = None,
    ) -> list[VideoRecord]:
        statuses = ",".join("?" for _ in RETRYABLE_STATUSES)
        parameters: list[object] = [*RETRYABLE_STATUSES, max_attempts]
        sql = f"""
            SELECT * FROM videos
            WHERE status IN ({statuses})
              AND (attempts < ? OR status IN ('live', 'upcoming'))
        """
        ids = list(only_ids or [])
        if ids:
            placeholders = ",".join("?" for _ in ids)
            sql += f" AND video_id IN ({placeholders})"
            parameters.extend(ids)
        sql += """
            ORDER BY
                CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'interrupted' THEN 1
                    WHEN 'live' THEN 2
                    WHEN 'upcoming' THEN 3
                    ELSE 4
                END,
                first_seen_at,
                video_id
        """
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = self.connection.execute(sql, parameters).fetchall()
        return [_record(row) for row in rows]

    def start_attempt(self, video_id: str) -> None:
        now = utc_iso()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE videos
                SET status = ?, attempts = attempts + 1, updated_at = ?,
                    last_checked_at = ?, last_error = NULL
                WHERE video_id = ?
                """,
                (STATUS_RUNNING, now, now, video_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"Unknown video in state database: {video_id}")

    def set_status(
        self,
        video_id: str,
        status: str,
        *,
        error: str | None = None,
        item_id: str | None = None,
    ) -> None:
        now = utc_iso()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE videos
                SET status = ?, last_error = ?, updated_at = ?,
                    last_checked_at = ?,
                    item_id = COALESCE(?, item_id)
                WHERE video_id = ?
                """,
                (status, _short_error(error), now, now, item_id, video_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"Unknown video in state database: {video_id}")

    def mark_complete(self, video_id: str, item_id: str) -> None:
        now = utc_iso()
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE videos
                SET status = ?, item_id = ?, last_error = NULL,
                    updated_at = ?, last_checked_at = ?, completed_at = ?
                WHERE video_id = ?
                """,
                (STATUS_COMPLETE, item_id, now, now, now, video_id),
            )
        if cursor.rowcount != 1:
            raise StateError(f"Unknown video in state database: {video_id}")

    def update_metadata(
        self,
        video_id: str,
        *,
        title: str | None = None,
        upload_date: str | None = None,
        duration: int | None = None,
        item_id: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE videos
                SET title = COALESCE(?, title),
                    upload_date = COALESCE(?, upload_date),
                    duration = COALESCE(?, duration),
                    item_id = COALESCE(?, item_id),
                    updated_at = ?
                WHERE video_id = ?
                """,
                (title, upload_date, duration, item_id, utc_iso(), video_id),
            )

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS total FROM videos GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def complete_records(self) -> list[VideoRecord]:
        rows = self.connection.execute(
            "SELECT * FROM videos WHERE status = ? ORDER BY completed_at, video_id",
            (STATUS_COMPLETE,),
        ).fetchall()
        return [_record(row) for row in rows]

    def reset_failed(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE videos
                SET status = ?, attempts = 0, last_error = NULL, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (STATUS_PENDING, utc_iso(), STATUS_FAILED, STATUS_INTERRUPTED),
            )
        return cursor.rowcount

    def checkpoint(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _record(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(
        video_id=row["video_id"],
        url=row["url"],
        title=row["title"],
        upload_date=row["upload_date"],
        duration=row["duration"],
        tab=row["tab"],
        status=row["status"],
        item_id=row["item_id"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        first_seen_at=row["first_seen_at"],
        updated_at=row["updated_at"],
        last_checked_at=row["last_checked_at"],
        completed_at=row["completed_at"],
    )


def _short_error(error: str | None, limit: int = 4000) -> str | None:
    if error is None:
        return None
    value = error.strip()
    return value[:limit] if value else None
