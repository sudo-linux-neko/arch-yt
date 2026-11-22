# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plainbagel_archiver.archive_org import (
    ArchiveOrgClient,
    _clean_metadata,
    _file_size,
    _metadata_first,
)
from plainbagel_archiver.artifacts import Artifact
from plainbagel_archiver.config import ArchiveCredentials
from plainbagel_archiver.errors import ItemCollisionError, UploadVerificationError
from plainbagel_archiver.utils import sha256_file


class FakeItem:
    def __init__(self, metadata):
        self.identifier = metadata.get("identifier", "theplainbagel-AbC_-123xyZ")
        self.exists = bool(metadata)
        self.item_metadata = {"is_collection": False}
        self.metadata = metadata
        self.modified = None

    def refresh(self, **kwargs):
        return None

    def get_files(self):
        return []

    def modify_metadata(self, metadata, **kwargs):
        self.modified = metadata
        return None


def test_metadata_helpers() -> None:
    assert _metadata_first(["a", "b"]) == "a"
    assert _metadata_first(None) is None
    assert _file_size(SimpleNamespace(size="12")) == 12
    assert _file_size(SimpleNamespace(size=None, metadata={"size": "9"})) == 9
    cleaned = _clean_metadata({"a": "", "b": None, "c": ["x"], "d": 2})
    assert cleaned == {"c": ["x"], "d": 2}


def test_remote_state_detects_collision(app_config, logger) -> None:
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    fake = FakeItem(
        {
            "identifier": "theplainbagel-AbC_-123xyZ",
            "youtube_id": "OTHERVIDEO1",
            "archive_status": "complete",
        }
    )
    client._get_item = lambda *args, **kwargs: fake
    state = client.remote_state("theplainbagel-AbC_-123xyZ", "AbC_-123xyZ")
    assert state.exists
    assert not state.owned
    assert state.complete


def test_archive_bundle_refuses_collision(app_config, logger, monkeypatch) -> None:
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    fake = FakeItem(
        {
            "identifier": "theplainbagel-AbC_-123xyZ",
            "youtube_id": "OTHERVIDEO1",
        }
    )
    client._get_item = lambda *args, **kwargs: fake
    bundle = SimpleNamespace(
        identifier="theplainbagel-AbC_-123xyZ",
        video_id="AbC_-123xyZ",
        total_size=10,
        artifacts=(),
        checksum_artifact=None,
        manifest_artifact=None,
    )
    with pytest.raises(ItemCollisionError):
        client._assert_owned_item(fake, bundle)


def test_verify_remote_item_checks_manifest_and_checksums(app_config, logger) -> None:
    checksums = b"abc  file.txt\n"
    checksum_entry = {
        "name": "SHA256SUMS",
        "sha256": hashlib.sha256(checksums).hexdigest(),
    }
    manifest = json.dumps(
        {
            "files": [{"name": "file.txt", "size": 3}],
            "checksum_file": checksum_entry,
        }
    ).encode()
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    fake = FakeItem(
        {
            "identifier": "theplainbagel-AbC_-123xyZ",
            "youtube_id": "AbC_-123xyZ",
            "archive_status": "complete",
            "archive_manifest_sha256": manifest_hash,
        }
    )
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    client._get_item = lambda *args, **kwargs: fake
    client._wait_for_expected_files = lambda *args, **kwargs: 2
    downloads = {
        "archive-manifest.json": manifest,
        "SHA256SUMS": checksums,
    }
    client._download_remote_file = lambda identifier, name, max_bytes: downloads[name]
    result = client.verify_remote_item("theplainbagel-AbC_-123xyZ", "AbC_-123xyZ")
    assert result.status == "verified"
    assert result.manifest_sha256 == manifest_hash


def test_verify_remote_item_rejects_bad_manifest_hash(app_config, logger) -> None:
    fake = FakeItem(
        {
            "identifier": "theplainbagel-AbC_-123xyZ",
            "youtube_id": "AbC_-123xyZ",
            "archive_status": "complete",
            "archive_manifest_sha256": "0" * 64,
        }
    )
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    client._get_item = lambda *args, **kwargs: fake
    client._download_remote_file = lambda *args, **kwargs: b"{}"
    with pytest.raises(UploadVerificationError):
        client.verify_remote_item("theplainbagel-AbC_-123xyZ", "AbC_-123xyZ")


def test_add_to_collection(app_config, logger) -> None:
    fake = FakeItem(
        {
            "identifier": "theplainbagel-AbC_-123xyZ",
            "youtube_id": "AbC_-123xyZ",
            "collection": ["opensource_movies"],
        }
    )
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    client._get_item = lambda *args, **kwargs: fake
    assert client.add_to_collection(
        "theplainbagel-AbC_-123xyZ", "AbC_-123xyZ", "theplainbagel-archive"
    )
    assert fake.modified == {"collection": ["opensource_movies", "theplainbagel-archive"]}
    fake.metadata["collection"] = fake.modified["collection"]
    assert not client.add_to_collection(
        "theplainbagel-AbC_-123xyZ", "AbC_-123xyZ", "theplainbagel-archive"
    )


class FakeUploadItem:
    def __init__(self, identifier: str):
        self.identifier = identifier
        self.exists = False
        self.item_metadata = {"is_collection": False}
        self.metadata = {}
        self.files = {}
        self.derived = False

    def refresh(self, **kwargs):
        return None

    def get_files(self):
        return list(self.files.values())

    def upload(self, files, metadata, **kwargs):
        self.exists = True
        self.metadata.update(metadata)
        for name in files:
            path = Path(name)
            self.files[path.name] = SimpleNamespace(name=path.name, size=path.stat().st_size)
        return [SimpleNamespace(status_code=200) for _ in files]

    def modify_metadata(self, metadata, **kwargs):
        self.metadata.update(metadata)

    def derive(self, **kwargs):
        self.derived = True


def test_archive_bundle_uploads_and_marks_complete(app_config, logger, tmp_path) -> None:
    payload = tmp_path / "video.mp4"
    payload.write_bytes(b"payload")
    checksum_path = tmp_path / "SHA256SUMS"
    checksum_path.write_text(f"{sha256_file(payload)}  video.mp4\n", encoding="utf-8")
    manifest_path = tmp_path / "archive-manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    payload_artifact = Artifact(
        payload, "video.mp4", "video", sha256_file(payload), payload.stat().st_size, "video"
    )
    checksum_artifact = Artifact(
        checksum_path,
        "SHA256SUMS",
        "checksums",
        sha256_file(checksum_path),
        checksum_path.stat().st_size,
        "checksums",
    )
    manifest_artifact = Artifact(
        manifest_path,
        "archive-manifest.json",
        "manifest",
        sha256_file(manifest_path),
        manifest_path.stat().st_size,
        "manifest",
    )
    bundle = SimpleNamespace(
        video_id="AbC_-123xyZ",
        identifier="theplainbagel-AbC_-123xyZ",
        total_size=sum(
            item.size for item in (payload_artifact, checksum_artifact, manifest_artifact)
        ),
        artifacts=(payload_artifact, checksum_artifact, manifest_artifact),
        checksum_artifact=checksum_artifact,
        manifest_artifact=manifest_artifact,
    )
    fake = FakeUploadItem(bundle.identifier)
    client = ArchiveOrgClient(
        app_config,
        ArchiveCredentials("access123", "secret123"),
        logger,
    )
    client._get_item = lambda *args, **kwargs: fake
    client._download_remote_file = lambda identifier, name, max_bytes: (
        tmp_path / name
    ).read_bytes()
    result = client.archive_bundle(
        bundle,
        {"youtube_id": "AbC_-123xyZ", "title": "A title"},
    )
    assert result.status == "complete"
    assert result.uploaded_files == 3
    assert result.remote_files == 3
    assert fake.derived
    assert fake.metadata["archive_status"] == "complete"
    assert fake.metadata["archive_manifest_sha256"] == manifest_artifact.sha256
    assert fake.metadata["archive_checksums_sha256"] == checksum_artifact.sha256
    assert fake.metadata["archive_file_count"] == "3"
