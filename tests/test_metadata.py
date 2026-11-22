# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import html
import json

from test_formats import base_formats

from plainbagel_archiver.formats import build_archive_plan
from plainbagel_archiver.metadata import (
    build_item_metadata,
    build_rights_text,
    make_item_identifier,
    write_metadata_files,
)


def sample_info() -> dict:
    return {
        "id": "AbC_-123xyZ",
        "title": "A <Test> & Video",
        "description": "Line one\nLine two <tag> & symbol",
        "upload_date": "2024-02-03",
        "timestamp": 1706918400,
        "duration": 125.7,
        "categories": ["Education"],
        "tags": ["finance", "investing"],
        "chapters": [
            {"start_time": 0, "end_time": 10, "title": "Intro"},
            {"start_time": 10, "title": "Body"},
        ],
        "formats": base_formats(),
        "language": "en",
    }


def test_identifier_preserves_video_id_case(app_config) -> None:
    assert make_item_identifier(app_config, "AbC_-123xyZ") == (
        f"{app_config.identifier_prefix}-AbC_-123xyZ"
    )


def test_item_metadata_includes_full_description_and_rights(app_config) -> None:
    info = sample_info()
    plan = build_archive_plan(info)
    metadata = build_item_metadata(
        app_config,
        info,
        plan,
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
    )
    assert metadata["date"] == "2024-02-03"
    assert metadata["collection"] == "opensource_movies"
    assert metadata["possible-copyright-status"] == "In Copyright"
    assert "NOT in the public domain" in metadata["rights"]
    assert "No commercial license" in metadata["rights"]
    assert "licenseurl" not in metadata
    assert html.escape(info["description"]) in metadata["description"]
    assert "<pre" in metadata["description"]
    assert metadata["youtube_id"] == info["id"]
    assert metadata["external-identifier"] == f"urn:youtube:{info['id']}"
    assert metadata["archiver"] == app_config.archiver_name
    assert metadata["archiver_contact"] == app_config.archiver_email
    assert metadata["contributor"].startswith("Archived by ")


def test_missing_upload_date_does_not_create_invalid_date(app_config) -> None:
    info = sample_info()
    info.pop("upload_date")
    info.pop("timestamp")
    plan = build_archive_plan(info)
    metadata = build_item_metadata(
        app_config,
        info,
        plan,
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
    )
    assert "date" not in metadata


def test_metadata_files_are_verbatim_and_complete(app_config, tmp_path) -> None:
    info = sample_info()
    plan = build_archive_plan(info)
    outputs = write_metadata_files(
        app_config,
        info,
        plan,
        tmp_path,
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
    )
    assert outputs["DESCRIPTION.txt"].read_text(encoding="utf-8") == info["description"]
    assert json.loads(outputs["source.info.json"].read_text())["id"] == info["id"]
    assert json.loads(outputs["metadata.json"].read_text())["rights"]["public_domain"] is False
    assert "Intro" in outputs["chapters.txt"].read_text()
    assert outputs["RIGHTS-AND-ATTRIBUTION.txt"].read_text().startswith("THE PLAIN BAGEL")


def test_rights_text_is_non_commercial_and_not_public_domain(app_config) -> None:
    text = build_rights_text(
        app_config,
        sample_info(),
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
    )
    assert "NOT in the public domain" in text
    assert "No commercial license" in text
    assert "No Creative Commons license" in text
    assert app_config.creator_email in text
    assert app_config.archiver_email in text
    assert app_config.archiver_name in text
    assert "Archiver: " + app_config.archiver_name in text


def test_empty_description_remains_empty(app_config, tmp_path) -> None:
    info = sample_info()
    info["description"] = ""
    plan = build_archive_plan(info)
    outputs = write_metadata_files(
        app_config,
        info,
        plan,
        tmp_path,
        identifier="theplainbagel-AbC_-123xyZ",
        captured_at="2026-10-24T00:00:00+00:00",
    )
    assert outputs["DESCRIPTION.txt"].read_bytes() == b""
