# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .archive_org import ArchiveOrgClient, collection_exists
from .artifacts import (
    BundleBuilder,
    build_item_metadata_for_bundle,
    remove_video_workdir,
)
from .config import AppConfig, load_credentials
from .errors import (
    ArchiverError,
    EnumerationError,
    ItemCollisionError,
    LockError,
    VideoLiveError,
    VideoUnavailableError,
    VideoUpcomingError,
)
from .formats import build_archive_plan
from .metadata import make_item_identifier
from .state import (
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_LIVE,
    STATUS_PENDING,
    STATUS_PREPARED,
    STATUS_UNAVAILABLE,
    STATUS_UPCOMING,
    STATUS_UPLOADING,
    DiscoveredVideo,
    StateStore,
)
from .utils import (
    atomic_write_text,
    disk_free_gib,
    human_bytes,
    utc_iso,
)
from .youtube import ChannelSnapshot, YtDlpClient

SUSPECT_STATUSES = {
    STATUS_PREPARED,
    STATUS_UPLOADING,
    STATUS_INTERRUPTED,
    STATUS_FAILED,
}


@dataclass(frozen=True)
class RunOptions:
    discover: bool = True
    limit: int | None = None
    only_ids: tuple[str, ...] = ()
    reconcile: bool = False
    reuse_cached_info: bool = False
    force: bool = False


@dataclass
class RunReport:
    discovered: int = 0
    processed: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    live: int = 0
    upcoming: int = 0
    unavailable: int = 0
    paused: bool = False
    session_exhausted: bool = False
    disk_low: bool = False
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    checked: int = 0
    verified: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                existing = self._read()
                if existing is not None:
                    if _pid_alive(existing):
                        raise LockError(
                            f"Another archiver process is running (PID {existing}); "
                            f"remove {self.path} only if that process is gone"
                        ) from None
                elif _lock_is_recent(self.path):
                    raise LockError(
                        f"Another archiver process is creating {self.path}; try again shortly"
                    ) from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "started_at": utc_iso(),
                    },
                    handle,
                )
                handle.write("\n")
            self.acquired = True
            return self
        raise LockError(f"Could not acquire runner lock at {self.path}")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def _read(self) -> int | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = payload.get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None


def _fetch_or_cached_info(
    config: AppConfig,
    youtube: YtDlpClient,
    logger: logging.Logger,
    video_id: str,
    reuse_cached: bool,
) -> dict[str, Any]:
    if not reuse_cached:
        return youtube.fetch_video_info(video_id)
    cached = config.downloads_dir / video_id / "bundle" / "source.info.json"
    if not cached.exists():
        raise ArchiverError(
            f"No cached source.info.json for {video_id}; rerun without --reuse-info"
        )
    try:
        info = json.loads(cached.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiverError(f"Cannot read cached source.info.json for {video_id}: {exc}") from exc
    if not isinstance(info, dict) or str(info.get("id") or "") != video_id:
        raise ArchiverError(f"Cached source.info.json for {video_id} is missing or mismatched")
    logger.info("Reusing cached yt-dlp metadata for %s (YouTube not contacted)", video_id)
    return info


class ArchiverRunner:
    def __init__(self, config: AppConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.youtube = YtDlpClient(config, logger)
        self.builder = BundleBuilder(config, self.youtube, logger)

    def run(self, options: RunOptions | None = None) -> RunReport:
        options = options or RunOptions()
        if not self.config.permission_acknowledged:
            raise ArchiverError(
                "The creator's written permission has not been acknowledged in the config"
            )
        credentials = load_credentials(required=True)
        assert credentials is not None
        client = ArchiveOrgClient(self.config, credentials, self.logger)
        report = RunReport()
        started = time.monotonic()
        deadline = started + self.config.session_minutes * 60

        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        with RunLock(self.config.lock_path), StateStore(self.config.database_path) as store:
            stale = store.reset_stale()
            if stale:
                self.logger.warning("Reset %d stale in-progress records", stale)
            if options.discover:
                try:
                    self._discover(store, report)
                except EnumerationError as exc:
                    if not store.list_all():
                        raise
                    self.logger.warning(
                        "Channel discovery failed; continuing with existing state: %s",
                        exc,
                    )
            if not collection_exists(self.config.collection):
                raise ArchiverError(
                    f"archive.org collection {self.config.collection!r} does not exist "
                    "or is not a collection"
                )
            records = store.candidates(
                max_attempts=self.config.max_attempts,
                only_ids=options.only_ids or None,
            )
            if options.limit is not None:
                records = records[: options.limit]
            self.logger.info(
                "Starting session: %d candidate video(s), %.0f minute budget, %s free",
                len(records),
                self.config.session_minutes,
                human_bytes(disk_free_gib(self.config.work_dir) * 1024**3),
            )
            for record in records:
                if self._pause_requested():
                    report.paused = True
                    self.logger.warning("Pause marker found; stopping after current item")
                    break
                if time.monotonic() >= deadline:
                    report.session_exhausted = True
                    self.logger.warning("Session time budget exhausted")
                    break
                free_gib = disk_free_gib(self.config.work_dir)
                if free_gib < self.config.min_free_gb:
                    report.disk_low = True
                    self.logger.error(
                        "Only %.1f GiB free; stopping before starting %s",
                        free_gib,
                        record.video_id,
                    )
                    break
                if self._remote_already_complete(
                    client,
                    record.video_id,
                    record.status,
                    options.reconcile,
                    store,
                    report,
                ):
                    continue
                report.processed += 1
                self._process_record(
                    store,
                    client,
                    record.video_id,
                    report,
                    options.reuse_cached_info,
                    options.force,
                )
            self._save_discovery_report(store, report)
            counts = store.counts()
            store.checkpoint()
        report.elapsed_seconds = time.monotonic() - started
        self.logger.info(
            "Session finished in %s: processed=%d complete=%d skipped=%d failed=%d live=%d "
            "upcoming=%d unavailable=%d paused=%s session_exhausted=%s",
            _format_elapsed(report.elapsed_seconds),
            report.processed,
            report.completed,
            report.skipped,
            report.failed,
            report.live,
            report.upcoming,
            report.unavailable,
            report.paused,
            report.session_exhausted,
        )
        self.logger.info("State counts: %s", counts)
        return report

    def verify_all(self, only_ids: tuple[str, ...] = ()) -> VerifyReport:
        credentials = load_credentials(required=True)
        assert credentials is not None
        client = ArchiveOrgClient(self.config, credentials, self.logger)
        report = VerifyReport()
        with StateStore(self.config.database_path) as store:
            records = store.complete_records()
            if only_ids:
                wanted = set(only_ids)
                records = [record for record in records if record.video_id in wanted]
            if not records:
                raise ArchiverError("No complete archive records are available to verify")
            for record in records:
                identifier = record.item_id or make_item_identifier(self.config, record.video_id)
                try:
                    result = client.verify_remote_item(identifier, record.video_id)
                    report.checked += 1
                    report.verified += 1
                    self.logger.info(
                        "Verified %s (%d files, %s)",
                        result.item_url,
                        result.remote_files,
                        human_bytes(result.total_size),
                    )
                except Exception as exc:
                    report.checked += 1
                    report.failed += 1
                    message = f"{record.video_id}: {exc}"
                    report.errors.append(message)
                    self.logger.error("Verification failed for %s", message)
        return report

    def discover_only(self) -> ChannelSnapshot:
        with StateStore(self.config.database_path) as store:
            report = RunReport()
            snapshot = self._discover(store, report)
        return snapshot

    def _discover(self, store: StateStore, report: RunReport) -> ChannelSnapshot:
        snapshot = self.youtube.list_channel()
        discovered = [
            DiscoveredVideo(
                video_id=entry.video_id,
                url=entry.url,
                title=entry.title,
                upload_date=entry.upload_date,
                duration=entry.duration,
                tab=entry.tab,
            )
            for entry in snapshot.entries
        ]
        store.upsert_many(discovered)
        report.discovered = len(discovered)
        self._write_discovery_snapshot(snapshot)
        self.logger.info(
            "Discovery: %d unique public videos across %s",
            len(discovered),
            ", ".join(self.config.tabs),
        )
        return snapshot

    def _write_discovery_snapshot(self, snapshot: ChannelSnapshot) -> None:
        path = self.config.state_dir / "discovery.json"
        payload = {
            "channel_id": snapshot.channel_id,
            "title": snapshot.title,
            "description": snapshot.description,
            "url": snapshot.url,
            "captured_at": snapshot.captured_at,
            "follower_count": snapshot.follower_count,
            "entries": [
                {
                    "video_id": entry.video_id,
                    "title": entry.title,
                    "url": entry.url,
                    "tab": entry.tab,
                    "duration": entry.duration,
                    "upload_date": entry.upload_date,
                }
                for entry in snapshot.entries
            ],
        }
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _save_discovery_report(self, store: StateStore, report: RunReport) -> None:
        path = self.config.state_dir / "last-run.json"
        payload = {
            "finished_at": utc_iso(),
            "report": report.__dict__,
            "counts": store.counts(),
        }
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _remote_already_complete(
        self,
        client: ArchiveOrgClient,
        video_id: str,
        status: str,
        reconcile: bool,
        store: StateStore,
        report: RunReport,
    ) -> bool:
        if status == STATUS_PENDING and not reconcile:
            return False
        identifier = make_item_identifier(self.config, video_id)
        remote = client.remote_state(identifier, video_id)
        if remote.exists and not remote.owned:
            message = (
                f"archive.org identifier collision at {identifier}: "
                f"remote youtube_id={remote.youtube_id!r}"
            )
            store.set_status(video_id, STATUS_FAILED, error=message, item_id=identifier)
            report.failed += 1
            report.errors.append(f"{video_id}: {message}")
            self.logger.error(message)
            return True
        if remote.complete:
            self.logger.info("Remote archive already complete: %s", remote.item_url)
            store.mark_complete(video_id, identifier)
            report.skipped += 1
            self._cleanup(video_id)
            return True
        return False

    def _process_record(
        self,
        store: StateStore,
        client: ArchiveOrgClient,
        video_id: str,
        report: RunReport,
        reuse_cached_info: bool = False,
        force: bool = False,
    ) -> None:
        store.start_attempt(video_id)
        identifier = make_item_identifier(self.config, video_id)
        item_id: str | None = None
        try:
            self.logger.info("Processing https://www.youtube.com/watch?v=%s", video_id)
            info = self._fetch_video_info(video_id, reuse_cached_info)
            store.update_metadata(
                video_id,
                title=str(info.get("title") or ""),
                upload_date=str(info.get("upload_date") or "") or None,
                duration=_duration_int(info),
            )
            plan = build_archive_plan(info)
            captured_at = utc_iso()
            auxiliary = self.youtube.download_auxiliary(
                info,
                self.config.downloads_dir / video_id / "aux",
            )
            bundle = self.builder.build(
                info,
                plan,
                identifier=identifier,
                captured_at=captured_at,
                auxiliary=auxiliary,
            )
            item_id = identifier
            metadata = build_item_metadata_for_bundle(self.config, info, bundle)
            manifest = bundle.manifest_artifact
            checksums = bundle.checksum_artifact
            if manifest is not None:
                metadata["archive_manifest_sha256"] = manifest.sha256
            if checksums is not None:
                metadata["archive_checksums_sha256"] = checksums.sha256
            metadata["archive_file_count"] = str(len(bundle.artifacts))
            metadata["archive_total_bytes"] = str(bundle.total_size)
            store.set_status(video_id, STATUS_PREPARED, item_id=identifier)
            store.set_status(video_id, STATUS_UPLOADING, item_id=identifier)
            result = client.archive_bundle(bundle, metadata, force=force)
            store.mark_complete(video_id, identifier)
            report.completed += 1
            self.logger.info(
                "Completed %s: %s (%d files, %s)",
                video_id,
                result.item_url,
                result.remote_files,
                human_bytes(result.total_size),
            )
            self._cleanup(video_id)
        except VideoLiveError:
            store.set_status(video_id, STATUS_LIVE, error="Video is currently live")
            report.live += 1
            self.logger.warning("Deferring live video %s", video_id)
        except VideoUpcomingError:
            store.set_status(video_id, STATUS_UPCOMING, error="Video is upcoming")
            report.upcoming += 1
            self.logger.warning("Deferring upcoming video %s", video_id)
        except VideoUnavailableError as exc:
            store.set_status(video_id, STATUS_UNAVAILABLE, error=str(exc))
            report.unavailable += 1
            self.logger.warning("Video unavailable: %s: %s", video_id, exc)
        except ItemCollisionError as exc:
            store.set_status(video_id, STATUS_FAILED, error=str(exc), item_id=item_id)
            report.failed += 1
            report.errors.append(f"{video_id}: {exc}")
            self.logger.error("Identifier collision for %s: %s", video_id, exc)
        except KeyboardInterrupt:
            store.set_status(
                video_id,
                STATUS_INTERRUPTED,
                error="Interrupted by user",
                item_id=item_id,
            )
            raise
        except Exception as exc:
            store.set_status(
                video_id,
                STATUS_FAILED,
                error=str(exc),
                item_id=item_id,
            )
            report.failed += 1
            report.errors.append(f"{video_id}: {exc}")
            if isinstance(exc, ArchiverError):
                self.logger.error("Video %s failed: %s", video_id, exc)
            else:
                self.logger.exception("Video %s failed unexpectedly", video_id)

    def _fetch_video_info(self, video_id: str, reuse_cached: bool) -> dict[str, Any]:
        return _fetch_or_cached_info(self.config, self.youtube, self.logger, video_id, reuse_cached)

    def _cleanup(self, video_id: str) -> None:
        if not self.config.cleanup_after_upload:
            return
        try:
            remove_video_workdir(self.config, video_id)
        except Exception as exc:
            self.logger.warning("Could not clean local files for %s: %s", video_id, exc)

    def _pause_requested(self) -> bool:
        return self.config.pause_path.exists()


def _duration_int(info: dict[str, Any]) -> int | None:
    value = info.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = round(value)
    return result if result >= 0 else None


def _lock_is_recent(path: Path, max_age_seconds: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime < max_age_seconds
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_collection_request(config: AppConfig, store: StateStore) -> str:
    records = store.complete_records()
    if not records:
        raise ArchiverError("No complete archive items are available")
    item_lines = [
        f"- {record.title or record.video_id}: https://archive.org/details/{record.item_id}"
        for record in records
        if record.item_id
    ]
    if not item_lines:
        raise ArchiverError("No complete archive records have item identifiers")
    count = len(item_lines)
    warning = ""
    if count < 50:
        warning = (
            "NOTE: Internet Archive's dedicated collection process normally expects a "
            f"list of at least 50 items; this list has only {count}.\n\n"
        )
    return f"""{warning}To: info@archive.org
Subject: Collection request for a The Plain Bagel preservation archive

Hello Internet Archive team,

I maintain a non-commercial preservation archive of the YouTube channel
The Plain Bagel ({config.channel_url}) at:
https://archive.org/details/{config.collection}

The creator granted written permission for non-commercial archival use. All
copyright remains with Richard Coffin / The Plain Bagel. The material is not
public domain, has no Creative Commons license, and has no commercial license.
The complete permission correspondence is held privately and can be provided
on request.

I would like to request a dedicated collection for these items.

Uploader account email: {config.archiver_email}
Current collection: {config.collection}
Requested collection name: The Plain Bagel Archive
Suggested collection identifier: theplainbagel-archive
Subject/tag: The Plain Bagel
Items currently archived: {count}

Collection description:
Non-commercial preservation copies of public YouTube videos from The Plain
Bagel, a finance education channel by Richard Coffin. All rights remain with
the creator. This archive is not public domain and grants no commercial
license. Permission was granted for non-commercial preservation only.

Item list:
{os.linesep.join(item_lines)}

Please let me know whether a dedicated collection can be created and whether
any additional information or formatting is required.

Thank you,
{config.archiver_email}
"""
