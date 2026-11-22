# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .constants import (
    ARCHIVE_CONTACT_NOTE,
    CREATOR_ATTRIBUTION,
    PERMISSION_NOTE,
    PERMISSION_SUMMARY,
    RIGHTS_STATEMENT,
    SUBJECT_BASE,
)
from .formats import ArchivePlan
from .utils import human_duration, utc_iso


def make_item_identifier(config: AppConfig, video_id: str) -> str:
    identifier = f"{config.identifier_prefix}-{video_id}"
    if len(identifier) > 100:
        raise ValueError(f"Archive identifier is too long: {identifier}")
    return identifier


def item_url(identifier: str) -> str:
    return f"https://archive.org/details/{identifier}"


def upload_date_from_info(info: dict[str, Any]) -> str | None:
    raw = info.get("upload_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def year_from_info(info: dict[str, Any]) -> str | None:
    date = upload_date_from_info(info)
    return date[:4] if date else None


def duration_seconds(info: dict[str, Any]) -> int | None:
    raw = info.get("duration")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = round(raw)
    return value if value >= 0 else None


def full_description(info: dict[str, Any]) -> str:
    value = info.get("description")
    return value if isinstance(value, str) else ""


def build_description_file(info: dict[str, Any]) -> str:
    return full_description(info)


def build_rights_text(
    config: AppConfig,
    info: dict[str, Any],
    *,
    identifier: str,
    captured_at: str,
) -> str:
    video_id = str(info.get("id") or "")
    title = str(info.get("title") or "Unknown title")
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    upload_date = upload_date_from_info(info) or "unknown"
    lines = [
        "THE PLAIN BAGEL — PRESERVATION ARCHIVE",
        "=" * 47,
        "",
        f"Title: {title}",
        f"YouTube video: {source_url}",
        f"Original channel: {config.channel_url}",
        f"Original upload date: {upload_date}",
        f"Archive item: {item_url(identifier)}",
        f"Captured: {captured_at}",
        "",
        "COPYRIGHT AND RIGHTS",
        "-------------------",
        "All copyright and all other rights in the original work belong to",
        "Richard Coffin and The Plain Bagel. Nothing in this item transfers,",
        "waives, or licenses those rights.",
        "",
        "This work is NOT in the public domain.",
        "No Creative Commons license has been applied.",
        "No commercial license is granted or implied.",
        f"Rights statement: {RIGHTS_STATEMENT}",
        "",
        "PERMISSION",
        "----------",
        PERMISSION_SUMMARY,
        PERMISSION_NOTE,
        "",
        "CONTACTS",
        "--------",
        f"Creator / rights holder: {config.creator_name} <{config.creator_email}>",
        f"Archiver: {config.archiver_name} <{config.archiver_email}>",
        ARCHIVE_CONTACT_NOTE,
        "",
        "If you are a rights holder and want this item corrected, removed, or",
        "licensed under different terms, contact the creator and the archiver.",
        "The archiver cannot license or relicense the creator's work. Only the",
        "rights holder can grant a license.",
        "",
        "SOURCE AND METADATA",
        "-------------------",
        "The full, unmodified YouTube description is stored in DESCRIPTION.txt.",
        "The complete yt-dlp metadata captured at download time is stored in",
        "source.info.json. Subtitle files, a thumbnail, chapter timestamps, and",
        "checksums are also included in this item.",
        "",
        "No paywall, private member content, or DRM is bypassed by this archive.",
        "This is a non-commercial preservation copy of publicly accessible media.",
        "",
        f"Archive contact: {config.archiver_email}",
        "",
    ]
    return "\n".join(lines)


def build_chapters_text(info: dict[str, Any]) -> str | None:
    chapters = info.get("chapters")
    if not isinstance(chapters, list):
        return None
    lines: list[str] = []
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        start = chapter.get("start_time")
        title = chapter.get("title")
        if not isinstance(start, (int, float)) or not isinstance(title, str):
            continue
        lines.append(f"{human_duration(start)}  {title}")
    if not lines:
        return None
    return "\n".join(lines) + "\n"


def build_clean_metadata(
    config: AppConfig,
    info: dict[str, Any],
    plan: ArchivePlan,
    *,
    identifier: str,
    captured_at: str,
) -> dict[str, Any]:
    subtitles = info.get("subtitles")
    automatic = info.get("automatic_captions")
    chapters = info.get("chapters")
    return {
        "schema": "plainbagel-preservation/v1",
        "captured_at": captured_at,
        "archive_identifier": identifier,
        "archive_url": item_url(identifier),
        "source": {
            "platform": "YouTube",
            "video_id": info.get("id"),
            "url": f"https://www.youtube.com/watch?v={info.get('id')}",
            "title": info.get("title"),
            "description": full_description(info),
            "upload_date": upload_date_from_info(info),
            "timestamp": info.get("timestamp"),
            "duration_seconds": duration_seconds(info),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "channel_url": info.get("channel_url") or config.channel_url,
            "uploader": info.get("uploader"),
            "uploader_id": info.get("uploader_id"),
            "availability": info.get("availability"),
            "live_status": info.get("live_status"),
            "was_live": info.get("was_live"),
            "categories": info.get("categories"),
            "tags": info.get("tags"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "age_limit": info.get("age_limit"),
            "chapters": chapters if isinstance(chapters, list) else [],
            "subtitle_languages": sorted(subtitles) if isinstance(subtitles, dict) else [],
            "automatic_caption_languages": sorted(automatic) if isinstance(automatic, dict) else [],
        },
        "renditions": plan.as_dict(),
        "rights": {
            "statement": RIGHTS_STATEMENT,
            "permission_summary": PERMISSION_SUMMARY,
            "copyright_holder": CREATOR_ATTRIBUTION,
            "public_domain": False,
            "creative_commons_license": None,
            "commercial_license": False,
            "creator_contact": config.creator_email,
            "archiver": config.archiver_name,
            "archiver_contact": config.archiver_email,
        },
    }


def build_item_metadata(
    config: AppConfig,
    info: dict[str, Any],
    plan: ArchivePlan,
    *,
    identifier: str,
    captured_at: str,
    archive_status: str = "uploading",
) -> dict[str, Any]:
    video_id = str(info.get("id") or "")
    title = str(info.get("title") or f"The Plain Bagel video {video_id}")
    description = full_description(info)
    date = upload_date_from_info(info)
    runtime = duration_seconds(info)
    categories = info.get("categories")
    tags = info.get("tags")
    subject = _subjects(config, info)
    rendered_description = _render_item_description(
        config=config,
        info=info,
        plan=plan,
        identifier=identifier,
        captured_at=captured_at,
        original_description=description,
    )
    metadata: dict[str, Any] = {
        "title": title,
        "creator": f"{config.creator_name} ({config.channel_name})",
        "contributor": f"Archived by {config.archiver_name}",
        "collection": config.collection,
        "mediatype": "movies",
        "description": rendered_description,
        "language": "eng",
        "subject": subject,
        "rights": RIGHTS_STATEMENT,
        "possible-copyright-status": "In Copyright",
        "copyright": (
            f"Copyright (c) {config.creator_name} / {config.channel_name}. All rights reserved."
        ),
        "permission": PERMISSION_SUMMARY,
        "source": "YouTube",
        "originalurl": f"https://www.youtube.com/watch?v={video_id}",
        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
        "youtube_id": video_id,
        "youtube_channel_id": config.channel_id,
        "channel_url": config.channel_url,
        "channel_name": config.channel_name,
        "creator_contact": config.creator_email,
        "archiver": config.archiver_name,
        "archiver_contact": config.archiver_email,
        "external-identifier": f"urn:youtube:{video_id}",
        "archive_status": archive_status,
        "archive_version": "1",
        "archive_manifest_file": "archive-manifest.json",
        "checksum_file": "SHA256SUMS",
        "capture_date": captured_at,
        "capture_software": "yt-dlp + ffmpeg via plainbagel-archiver",
        "notes": (
            "Non-commercial preservation copy. Copyright remains with the creator. "
            "Not public domain. Full permission correspondence is held privately "
            "by the archiver."
        ),
    }
    if date:
        metadata["date"] = date
    elif year_from_info(info):
        metadata["date"] = str(year_from_info(info))
    if runtime is not None:
        metadata["runtime"] = human_duration(runtime)
    if categories:
        metadata["youtube_category"] = ", ".join(str(item) for item in categories)
    if isinstance(tags, list) and tags:
        metadata["youtube_tags"] = ", ".join(str(item) for item in tags)
    for key in ("view_count", "like_count", "comment_count"):
        value = info.get(key)
        if isinstance(value, int):
            metadata[f"youtube_{key}"] = str(value)
    if info.get("was_live"):
        metadata["source_live_status"] = "was_live"
    actual_codecs = ", ".join(
        f"{item.requested_height}p={item.video.codec}" for item in plan.renditions
    )
    if plan.master:
        actual_codecs = (
            f"{actual_codecs}; master={plan.master.video.quality_height}p/{plan.master.video.codec}"
        )
    metadata["archived_streams"] = actual_codecs
    return metadata


def _render_item_description(
    *,
    config: AppConfig,
    info: dict[str, Any],
    plan: ArchivePlan,
    identifier: str,
    captured_at: str,
    original_description: str,
) -> str:
    video_id = str(info.get("id") or "")
    source_url = f"https://www.youtube.com/watch?v={video_id}"
    upload_date = upload_date_from_info(info) or "unknown"
    duration = duration_seconds(info)
    escaped_description = html.escape(original_description)
    file_lines = [
        f"<li><code>{html.escape(item.filename)}</code> — "
        f"{item.video.quality_height}p {item.video.codec.upper()}"
        f"{' (source fallback)' if item.fallback_reason else ''}</li>"
        for item in plan.renditions
    ]
    if plan.master:
        file_lines.append(
            f"<li><code>{html.escape(plan.master.filename)}</code> — preservation master "
            f"({plan.master.video.quality_height}p {plan.master.video.codec.upper()})</li>"
        )
    file_lines.extend(
        [
            "<li><code>DESCRIPTION.txt</code> — full original YouTube description</li>",
            "<li><code>source.info.json</code> — complete yt-dlp metadata at capture time</li>",
            "<li><code>RIGHTS-AND-ATTRIBUTION.txt</code> — rights and permission statement</li>",
            "<li><code>archive-manifest.json</code> — technical manifest</li>",
            "<li><code>SHA256SUMS</code> — integrity checksums</li>",
        ]
    )
    warnings = ""
    if plan.warnings:
        warnings = "<p><b>Technical notes:</b> " + html.escape("; ".join(plan.warnings)) + "</p>"
    return f"""<p><b>This is a non-commercial preservation copy, archived with the creator's written permission.</b></p>
<p>Copyright and all other rights remain with <b>{html.escape(config.creator_name)}</b> and <b>{html.escape(config.channel_name)}</b>. This work is <b>NOT in the public domain</b>, is <b>not</b> offered under a Creative Commons license, and carries <b>no commercial license</b>. For commercial licensing, contact the creator.</p>
<p><b>Original video:</b> <a href="{html.escape(source_url)}">{html.escape(source_url)}</a><br>
<b>Original channel:</b> <a href="{html.escape(config.channel_url)}">{html.escape(config.channel_url)}</a><br>
<b>Original upload date:</b> {html.escape(upload_date)}<br>
<b>Runtime:</b> {html.escape(human_duration(duration))}<br>
<b>Archived:</b> {html.escape(captured_at)}<br>
<b>Archive identifier:</b> <code>{html.escape(identifier)}</code></p>
<p><b>Creator / rights holder:</b> <a href="mailto:{html.escape(config.creator_email)}">{html.escape(config.creator_email)}</a><br>
<b>Archiver:</b> {html.escape(config.archiver_name)} <a href="mailto:{html.escape(config.archiver_email)}">{html.escape(config.archiver_email)}</a></p>
<p>{html.escape(PERMISSION_SUMMARY)}</p>
<p>{html.escape(ARCHIVE_CONTACT_NOTE)}</p>
<h3>Original YouTube description (verbatim)</h3>
<pre style="white-space: pre-wrap;">{escaped_description}</pre>
<h3>Files and technical details</h3>
<ul>{"".join(file_lines)}</ul>
{warnings}
<p>Full source metadata is preserved in <code>source.info.json</code> and in the item metadata. Captions and the thumbnail are included where YouTube provided them.</p>"""


def _subjects(config: AppConfig, info: dict[str, Any]) -> list[str]:
    result = list(SUBJECT_BASE)
    year = year_from_info(info)
    if year:
        result.append(year)
    categories = info.get("categories")
    if isinstance(categories, list):
        for value in categories:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
    result.append("non-commercial archive")
    deduped: list[str] = []
    for value in result:
        if value not in deduped:
            deduped.append(value)
    return deduped[:10]


def write_metadata_files(
    config: AppConfig,
    info: dict[str, Any],
    plan: ArchivePlan,
    destination: Path,
    *,
    identifier: str,
    captured_at: str,
    auxiliary_files: dict[str, Path] | None = None,
) -> dict[str, Path]:
    from .utils import atomic_write_text

    destination.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    description_path = destination / "DESCRIPTION.txt"
    atomic_write_text(description_path, build_description_file(info))
    outputs["DESCRIPTION.txt"] = description_path

    rights_path = destination / "RIGHTS-AND-ATTRIBUTION.txt"
    atomic_write_text(
        rights_path,
        build_rights_text(config, info, identifier=identifier, captured_at=captured_at),
    )
    outputs["RIGHTS-AND-ATTRIBUTION.txt"] = rights_path

    chapters = build_chapters_text(info)
    if chapters:
        chapters_path = destination / "chapters.txt"
        atomic_write_text(chapters_path, chapters)
        outputs["chapters.txt"] = chapters_path

    clean = build_clean_metadata(config, info, plan, identifier=identifier, captured_at=captured_at)
    metadata_path = destination / "metadata.json"
    atomic_write_text(
        metadata_path,
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    outputs["metadata.json"] = metadata_path

    source_info_path = destination / "source.info.json"
    atomic_write_text(
        source_info_path,
        json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    outputs["source.info.json"] = source_info_path

    if auxiliary_files:
        outputs.update(auxiliary_files)
    return outputs


def item_metadata_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)


def captured_at_now() -> str:
    return utc_iso()
