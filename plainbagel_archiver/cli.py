# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from dataclasses import asdict
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import quote

from .archive_org import collection_exists
from .artifacts import build_item_metadata_for_bundle
from .config import AppConfig, load_config, load_credentials
from .errors import ArchiverError, ConfigError
from .formats import build_archive_plan
from .logging_utils import configure_logging, redact
from .media import tool_versions
from .metadata import make_item_identifier
from .runner import ArchiverRunner, RunOptions, build_collection_request
from .state import StateStore
from .utils import atomic_write_text, disk_free_gib, human_bytes, utc_iso
from .youtube import YtDlpClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plainbagel-archive",
        description="Resumable Internet Archive preservation pipeline for The Plain Bagel.",
    )
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    parser.add_argument("--work-dir", type=Path, help="Override work directory")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check dependencies, config, and network")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    discover = subparsers.add_parser("discover", help="Discover public channel videos")
    discover.add_argument("--json", action="store_true", dest="as_json")

    build = subparsers.add_parser(
        "build",
        help="Download and build local preview bundles without uploading",
    )
    build.add_argument(
        "--reuse-info",
        action="store_true",
        help="Reuse cached source.info.json instead of contacting YouTube",
    )
    build.add_argument(
        "--only-id",
        action="append",
        required=True,
        metavar="VIDEO_ID",
        help="YouTube video ID to build; repeatable",
    )

    run = subparsers.add_parser("run", help="Archive pending public videos")
    run.add_argument("--limit", type=int, help="Process at most this many candidates")
    run.add_argument(
        "--only-id",
        action="append",
        default=[],
        metavar="VIDEO_ID",
        help="Process only this YouTube video ID; repeatable",
    )
    run.add_argument(
        "--skip-discover",
        action="store_false",
        dest="discover",
        help="Use existing state without refreshing the channel listing",
    )
    run.add_argument(
        "--reconcile",
        action="store_true",
        help="Check archive.org for every candidate before downloading",
    )
    run.add_argument("--collection", help="Override the archive.org collection")
    run.add_argument("--session-minutes", type=int, help="Override session time budget")
    run.add_argument("--cookies", type=Path, help="Override yt-dlp cookies file")
    run.add_argument(
        "--no-cleanup",
        action="store_false",
        dest="cleanup",
        help="Keep local media after a successful upload",
    )
    run.add_argument(
        "--reuse-info",
        action="store_true",
        help=(
            "Reuse the cached source.info.json bundle metadata instead of "
            "contacting YouTube (useful when YouTube blocks unauthenticated requests)"
        ),
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="Re-archive an item even when archive.org already shows it complete",
    )
    run.add_argument("--json", action="store_true", dest="as_json")

    verify = subparsers.add_parser("verify", help="Verify complete items on archive.org")
    verify.add_argument(
        "--only-id",
        action="append",
        default=[],
        metavar="VIDEO_ID",
        help="Verify only this YouTube video ID; repeatable",
    )
    verify.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="Show local archive state")
    status.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("pause", help="Stop after the current video")
    subparsers.add_parser("resume", help="Clear the pause marker")
    subparsers.add_parser("reset-failed", help="Retry failed and interrupted videos")

    request = subparsers.add_parser(
        "collection-request",
        help="Generate a dedicated archive.org collection request",
    )
    request.add_argument("--output", type=Path, help="Output path for the request text")

    migrate = subparsers.add_parser(
        "migrate-collection",
        help="Add complete items to a newly created archive.org collection",
    )
    migrate.add_argument("--collection", required=True, help="Target collection identifier")
    migrate.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing collection membership instead of adding to it",
    )
    migrate.add_argument(
        "--only-id",
        action="append",
        default=[],
        metavar="VIDEO_ID",
        help="Migrate only this YouTube video ID; repeatable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _load_config(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logger = configure_logging(
        config.logs_dir,
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    try:
        if args.command == "doctor":
            return _doctor(config, logger, args.as_json)
        if args.command == "discover":
            snapshot = ArchiverRunner(config, logger).discover_only()
            payload = {
                "channel_id": snapshot.channel_id,
                "title": snapshot.title,
                "captured_at": snapshot.captured_at,
                "entries": len(snapshot.entries),
                "follower_count": snapshot.follower_count,
                "tabs": _tab_counts(snapshot),
            }
            _print_json(payload) if args.as_json else print(
                f"{snapshot.title}: {len(snapshot.entries)} public videos ({_tab_counts(snapshot)})"
            )
            return 0
        if args.command == "build":
            return _build_local_previews(config, logger, args)
        if args.command == "run":
            options = RunOptions(
                discover=args.discover,
                limit=args.limit,
                only_ids=tuple(args.only_id),
                reconcile=args.reconcile,
                reuse_cached_info=args.reuse_info,
                force=args.force,
            )
            report = ArchiverRunner(config, logger).run(options)
            if args.as_json:
                _print_json(asdict(report))
            else:
                print(
                    f"processed={report.processed} complete={report.completed} "
                    f"skipped={report.skipped} failed={report.failed} "
                    f"live={report.live} upcoming={report.upcoming} "
                    f"unavailable={report.unavailable} paused={report.paused} "
                    f"session_exhausted={report.session_exhausted}"
                )
            return 0
        if args.command == "verify":
            report = ArchiverRunner(config, logger).verify_all(tuple(args.only_id))
            if args.as_json:
                _print_json(asdict(report))
            else:
                print(f"checked={report.checked} verified={report.verified} failed={report.failed}")
            return 0 if report.failed == 0 else 1
        if args.command == "status":
            return _status(config, args.as_json)
        if args.command == "pause":
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.pause_path.write_text(utc_iso() + "\n", encoding="utf-8")
            print(f"Pause requested: {config.pause_path}")
            return 0
        if args.command == "resume":
            config.pause_path.unlink(missing_ok=True)
            print("Pause marker cleared")
            return 0
        if args.command == "reset-failed":
            with StateStore(config.database_path) as store:
                count = store.reset_failed()
            print(f"Reset {count} failed or interrupted record(s)")
            return 0
        if args.command == "collection-request":
            output = args.output or (config.state_dir / "collection-request.txt")
            with StateStore(config.database_path) as store:
                content = build_collection_request(config, store)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
            print(content)
            print(f"\nSaved to {output}", file=sys.stderr)
            return 0
        if args.command == "migrate-collection":
            return _migrate_collection(config, logger, args)
    except ArchiverError as exc:
        message = _redacted(str(exc))
        logger.error("%s", message)
        print(f"error: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception as exc:
        message = _redacted(str(exc))
        logger.exception("Unexpected failure")
        print(f"unexpected error: {message}", file=sys.stderr)
        return 1
    parser.error("unknown command")
    return 2


def _build_local_previews(config: AppConfig, logger: logging.Logger, args) -> int:
    if not config.permission_acknowledged:
        raise ArchiverError(
            "The creator's written permission has not been acknowledged in the config"
        )
    runner = ArchiverRunner(config, logger)
    for video_id in dict.fromkeys(args.only_id):
        info = runner._fetch_video_info(video_id, bool(args.reuse_info))
        plan = build_archive_plan(info)
        identifier = make_item_identifier(config, video_id)
        captured_at = utc_iso()
        auxiliary = runner.youtube.download_auxiliary(
            info,
            config.downloads_dir / video_id / "aux",
        )
        bundle = runner.builder.build(
            info,
            plan,
            identifier=identifier,
            captured_at=captured_at,
            auxiliary=auxiliary,
        )
        metadata = build_item_metadata_for_bundle(config, info, bundle)
        if bundle.manifest_artifact is not None:
            metadata["archive_manifest_sha256"] = bundle.manifest_artifact.sha256
        if bundle.checksum_artifact is not None:
            metadata["archive_checksums_sha256"] = bundle.checksum_artifact.sha256
        metadata["archive_file_count"] = str(len(bundle.artifacts))
        metadata["archive_total_bytes"] = str(bundle.total_size)
        preview = _write_bundle_preview(config, bundle, metadata)
        print(f"Built {video_id}: {info.get('title', '')}")
        print(f"  bundle:  {bundle.directory}")
        print(f"  preview: {preview}")
        print(f"  identifier: {identifier}")
        print(f"  files:   {len(bundle.artifacts)} ({human_bytes(bundle.total_size)})")
        for artifact in bundle.artifacts:
            print(
                f"    {artifact.kind:10s} {human_bytes(artifact.size):>10s}  {artifact.remote_name}"
            )
        print("Local preview only; nothing was uploaded.")
    return 0


def _write_bundle_preview(config: AppConfig, bundle, metadata: dict[str, object]) -> Path:
    preview_dir = config.work_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    path = preview_dir / f"{bundle.video_id}.html"
    bundle_link = os.path.relpath(bundle.directory, preview_dir).replace(os.sep, "/")
    description_html = str(metadata.get("description") or "")
    description_text = (bundle.directory / "DESCRIPTION.txt").read_text(encoding="utf-8")
    rights_text = (bundle.directory / "RIGHTS-AND-ATTRIBUTION.txt").read_text(encoding="utf-8")
    file_rows = "".join(
        f'<tr><td><a href="{quote(f"{bundle_link}/{artifact.remote_name}")}">'
        f"{html.escape(artifact.remote_name)}</a></td>"
        f"<td>{html.escape(artifact.kind)}</td><td>{human_bytes(artifact.size)}</td>"
        f"<td>{html.escape(artifact.description)}</td></tr>"
        for artifact in bundle.artifacts
    )
    meta_rows = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(_preview_value(value))}</td></tr>"
        for key, value in sorted(metadata.items())
        if key != "description"
    )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Preview: {html.escape(str(metadata.get("title") or bundle.video_id))}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
.notice {{ background: #fff3cd; border: 1px solid #e0b400; padding: 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ccc; padding: .35rem .5rem; text-align: left; vertical-align: top; }}
th {{ background: #f3f3f3; width: 25%; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f7f7f7; padding: 1rem; }}
</style></head><body>
<h1>Local preview: {html.escape(str(metadata.get("title") or bundle.video_id))}</h1>
<p class="notice"><b>Not uploaded.</b> This page previews the bundle and the archive.org metadata before a real upload.</p>
<p><b>Identifier:</b> <code>{html.escape(bundle.identifier)}</code><br>
<b>YouTube:</b> <a href="https://www.youtube.com/watch?v={quote(bundle.video_id)}">https://www.youtube.com/watch?v={quote(bundle.video_id)}</a></p>
<h2>Archive.org item description</h2>
{description_html}
<h2>Original YouTube description (DESCRIPTION.txt)</h2>
<pre>{html.escape(description_text)}</pre>
<h2>Rights and attribution</h2>
<pre>{html.escape(rights_text)}</pre>
<h2>Files</h2>
<table><thead><tr><th>File</th><th>Kind</th><th>Size</th><th>Description</th></tr></thead>
<tbody>{file_rows}</tbody></table>
<h2>Archive.org metadata</h2>
<table><tbody>{meta_rows}</tbody></table>
</body></html>
"""
    atomic_write_text(path, document)
    return path


def _preview_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _redacted(value: str) -> str:
    return redact(
        value,
        [
            os.environ.get("IA_ACCESS_KEY", ""),
            os.environ.get("IA_SECRET_KEY", ""),
        ],
    )


def _load_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(Path(args.config))
    overrides = {}
    if args.work_dir is not None:
        overrides["work_dir"] = args.work_dir
    if getattr(args, "collection", None):
        overrides["collection"] = args.collection
    if getattr(args, "session_minutes", None):
        overrides["session_minutes"] = args.session_minutes
    if getattr(args, "cookies", None):
        overrides["cookies_file"] = args.cookies
    if getattr(args, "cleanup", None) is not None:
        overrides["cleanup_after_upload"] = args.cleanup
    return config.with_overrides(**overrides)


def _migrate_collection(config: AppConfig, logger: logging.Logger, args) -> int:
    from .archive_org import ArchiveOrgClient
    from .metadata import make_item_identifier

    credentials = load_credentials(required=True)
    assert credentials is not None
    if not collection_exists(args.collection):
        raise ArchiverError(f"archive.org collection {args.collection!r} does not exist")
    client = ArchiveOrgClient(config, credentials, logger)
    with StateStore(config.database_path) as store:
        records = store.complete_records()
    wanted = set(args.only_id)
    if wanted:
        records = [record for record in records if record.video_id in wanted]
    if not records:
        raise ArchiverError("No matching complete archive records are available")
    changed = 0
    for record in records:
        identifier = record.item_id or make_item_identifier(config, record.video_id)
        if client.add_to_collection(
            identifier,
            record.video_id,
            args.collection,
            keep_existing=not args.replace,
        ):
            changed += 1
            logger.info("Added %s to collection %s", identifier, args.collection)
    print(f"Updated collection membership for {changed} item(s)")
    print(f"Set collection = {args.collection!r} in config.toml if future uploads should go there.")
    return 0


def _status(config: AppConfig, as_json: bool) -> int:
    if not config.database_path.exists():
        print("No state database exists yet. Run `discover` or `run` first.")
        return 0
    with StateStore(config.database_path) as store:
        counts = store.counts()
        complete = store.complete_records()
        payload = {
            "counts": counts,
            "complete": [
                {
                    "video_id": record.video_id,
                    "title": record.title,
                    "item_id": record.item_id,
                    "completed_at": record.completed_at,
                    "url": (
                        f"https://archive.org/details/{record.item_id}" if record.item_id else None
                    ),
                }
                for record in complete
            ],
        }
    if as_json:
        _print_json(payload)
        return 0
    print("Status counts:")
    for status, count in sorted(counts.items()):
        print(f"  {status:12s} {count}")
    print(f"\nComplete archive items: {len(complete)}")
    for record in complete:
        identifier = record.item_id or "(missing identifier)"
        print(f"  {record.video_id}  https://archive.org/details/{identifier}")
    return 0


def _doctor(config: AppConfig, logger: logging.Logger, as_json: bool) -> int:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("config", True, str(config.config_path))
    add("permission", config.permission_acknowledged, str(config.permission_acknowledged))
    try:
        versions = tool_versions()
        add("ffmpeg", True, versions["ffmpeg"])
        add("ffprobe", True, versions["ffprobe"])
    except ArchiverError as exc:
        add("ffmpeg", False, str(exc))
    try:
        ytdlp = YtDlpClient(config, logger)
        add("yt-dlp", True, ytdlp.version())
    except Exception as exc:
        add("yt-dlp", False, str(exc))
    try:
        ia_version = importlib_metadata.version("internetarchive")
        add("internetarchive", True, ia_version)
    except importlib_metadata.PackageNotFoundError:
        add("internetarchive", False, "not installed")
    credentials = load_credentials(required=False)
    if credentials is None:
        add("IA credentials", False, "IA_ACCESS_KEY and IA_SECRET_KEY are not set")
    else:
        add("IA credentials", True, "present (not authenticated during doctor)")
    try:
        exists = collection_exists(config.collection)
        add("collection", exists, f"{config.collection} exists={exists}")
    except ArchiverError as exc:
        add("collection", False, str(exc))
    free_gib = disk_free_gib(config.work_dir)
    add(
        "disk",
        free_gib >= config.min_free_gb,
        f"{free_gib:.2f} GiB free (reserve {config.min_free_gb:.1f} GiB)",
    )
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with StateStore(config.database_path) as store:
            counts = store.counts()
        add("state database", True, json.dumps(counts, sort_keys=True))
    except Exception as exc:
        add("state database", False, str(exc))
    add("work directory", True, str(config.work_dir))

    if as_json:
        _print_json({"ok": all(bool(check["ok"]) for check in checks), "checks": checks})
    else:
        for check in checks:
            marker = "OK" if check["ok"] else "FAIL"
            print(f"[{marker:4s}] {check['check']}: {check['detail']}")
        if not all(bool(check["ok"]) for check in checks):
            print(
                "\nSet IA_ACCESS_KEY and IA_SECRET_KEY from "
                "https://archive.org/account/s3.php before running uploads."
            )
    return 0 if all(bool(check["ok"]) for check in checks) else 1


def _tab_counts(snapshot) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in snapshot.entries:
        counts[entry.tab] = counts.get(entry.tab, 0) + 1
    return counts


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
