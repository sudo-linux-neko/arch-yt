# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .artifacts import ArchiveBundle
from .config import AppConfig, ArchiveCredentials
from .errors import ItemCollisionError, UploadError, UploadVerificationError
from .utils import human_bytes, utc_iso

SCRIPT_URL = "https://archive.org/download/{identifier}/{name}"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DEFAULT_REQUEST_TIMEOUT = 120


@dataclass(frozen=True)
class ArchiveResult:
    identifier: str
    item_url: str
    status: str
    uploaded_files: int
    remote_files: int
    total_size: int
    manifest_sha256: str | None


@dataclass(frozen=True)
class RemoteState:
    identifier: str
    exists: bool
    owned: bool
    complete: bool
    youtube_id: str | None
    item_url: str


class ArchiveOrgClient:
    def __init__(
        self,
        config: AppConfig,
        credentials: ArchiveCredentials,
        logger: logging.Logger,
    ):
        self.config = config
        self.credentials = credentials
        self.logger = logger
        try:
            from internetarchive import get_item
        except ImportError as exc:
            raise UploadError("internetarchive is not installed; install requirements.txt") from exc
        self._get_item = get_item
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "plainbagel-archiver/1.0"})
        self._ia_config: dict[str, Any] = {
            "s3": {
                "access": credentials.access_key,
                "secret": credentials.secret_key,
            },
            "general": {"secure": True},
        }

    def item_url(self, identifier: str) -> str:
        return f"https://archive.org/details/{identifier}"

    def check_collection(self) -> bool:
        item = self._retry(
            "read collection metadata",
            lambda: self._get_item(
                self.config.collection,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if not item.exists:
            return False
        metadata = item.metadata or {}
        return bool(
            item.item_metadata.get("is_collection") or metadata.get("mediatype") == "collection"
        )

    def archive_bundle(
        self,
        bundle: ArchiveBundle,
        metadata: dict[str, Any],
        *,
        force: bool = False,
    ) -> ArchiveResult:
        self._validate_bundle(bundle)
        item = self._retry(
            "read item metadata",
            lambda: self._get_item(
                bundle.identifier,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if item.exists:
            self._assert_owned_item(item, bundle)
            if force:
                self.logger.info(
                    "Refreshing archive item %s (--force bypasses the complete shortcut)",
                    bundle.identifier,
                )
            elif self._remote_is_complete(item, bundle):
                self.logger.info("%s is already complete on archive.org", bundle.identifier)
                return ArchiveResult(
                    identifier=bundle.identifier,
                    item_url=self.item_url(bundle.identifier),
                    status="already_complete",
                    uploaded_files=0,
                    remote_files=len(self._remote_file_map(item)),
                    total_size=bundle.total_size,
                    manifest_sha256=(
                        bundle.manifest_artifact.sha256 if bundle.manifest_artifact else None
                    ),
                )
            self.logger.info("Resuming incomplete archive item %s", bundle.identifier)
            self._modify_metadata(item, {**metadata, "archive_status": "uploading"})
        else:
            self.logger.info("Creating archive item %s", bundle.identifier)

        upload_metadata = dict(metadata)
        upload_metadata["archive_status"] = "uploading"
        upload_metadata = _clean_metadata(upload_metadata)
        if item.exists:
            self._modify_metadata(item, upload_metadata)

        files = [str(artifact.path) for artifact in bundle.artifacts]
        self.logger.info(
            "Uploading %d files (%s) for %s",
            len(files),
            human_bytes(bundle.total_size),
            bundle.identifier,
        )
        uploaded = self._retry(
            "upload files",
            lambda: item.upload(
                files,
                metadata=upload_metadata,
                access_key=self.credentials.access_key,
                secret_key=self.credentials.secret_key,
                checksum=True,
                verify=True,
                queue_derive=False,
                retries=self.config.upload_retries,
                retries_sleep=self.config.upload_retries_sleep,
                request_kwargs={"timeout": self.config.upload_timeout_seconds},
            ),
        )
        uploaded_files = sum(
            1 for response in uploaded if getattr(response, "status_code", 200) in (200, 201)
        )

        item = self._wait_for_owned_item(bundle.identifier, bundle.video_id)
        remote_files = self._wait_for_remote_files(item, bundle)
        self._verify_remote_checksums(item, bundle)
        self._modify_metadata(
            item,
            {
                **upload_metadata,
                "archive_status": "uploaded",
                "archive_uploaded_at": utc_iso(),
            },
        )

        self.logger.info("Requesting archive.org derive for %s", bundle.identifier)
        self._retry("request derive", lambda: item.derive(priority=0))
        complete_metadata = {
            **upload_metadata,
            "archive_status": "complete",
            "archive_completed_at": utc_iso(),
            "archive_manifest_sha256": (
                bundle.manifest_artifact.sha256 if bundle.manifest_artifact else ""
            ),
            "archive_checksums_sha256": (
                bundle.checksum_artifact.sha256 if bundle.checksum_artifact else ""
            ),
            "archive_file_count": str(len(bundle.artifacts)),
            "archive_total_bytes": str(bundle.total_size),
        }
        self._modify_metadata(item, complete_metadata)
        self.logger.info(
            "Archive complete: %s (%d remote files)",
            self.item_url(bundle.identifier),
            remote_files,
        )
        return ArchiveResult(
            identifier=bundle.identifier,
            item_url=self.item_url(bundle.identifier),
            status="complete",
            uploaded_files=uploaded_files,
            remote_files=remote_files,
            total_size=bundle.total_size,
            manifest_sha256=(bundle.manifest_artifact.sha256 if bundle.manifest_artifact else None),
        )

    def add_to_collection(
        self,
        identifier: str,
        video_id: str,
        collection: str,
        *,
        keep_existing: bool = True,
    ) -> bool:
        item = self._retry(
            "read item metadata",
            lambda: self._get_item(
                identifier,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if not item.exists:
            raise UploadError(f"archive.org item does not exist: {identifier}")
        metadata = item.metadata or {}
        youtube_id = _metadata_first(metadata.get("youtube_id"))
        if youtube_id != video_id:
            raise ItemCollisionError(
                f"archive.org item {identifier} identifies {youtube_id!r}, not {video_id!r}"
            )
        current_raw = metadata.get("collection")
        if isinstance(current_raw, list):
            current = [str(value) for value in current_raw if str(value).strip()]
        elif isinstance(current_raw, str) and current_raw.strip():
            current = [current_raw]
        else:
            current = []
        if keep_existing:
            updated = [*current]
            if collection not in updated:
                updated.append(collection)
        else:
            updated = [collection]
        if updated == current:
            return False
        self._modify_metadata(item, {"collection": updated})
        return True

    def remote_state(self, identifier: str, video_id: str) -> RemoteState:
        item = self._retry(
            "read item metadata",
            lambda: self._get_item(
                identifier,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if not item.exists:
            return RemoteState(
                identifier=identifier,
                exists=False,
                owned=False,
                complete=False,
                youtube_id=None,
                item_url=self.item_url(identifier),
            )
        metadata = item.metadata or {}
        youtube_id = _metadata_first(metadata.get("youtube_id"))
        complete = _metadata_first(metadata.get("archive_status")) == "complete"
        return RemoteState(
            identifier=identifier,
            exists=True,
            owned=youtube_id == video_id,
            complete=complete,
            youtube_id=youtube_id,
            item_url=self.item_url(identifier),
        )

    def verify_remote_item(self, identifier: str, video_id: str) -> ArchiveResult:
        item = self._retry(
            "read item metadata",
            lambda: self._get_item(
                identifier,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if not item.exists:
            raise UploadVerificationError(f"archive.org item does not exist: {identifier}")
        metadata = item.metadata or {}
        remote_youtube_id = _metadata_first(metadata.get("youtube_id"))
        if remote_youtube_id != video_id:
            raise UploadVerificationError(
                f"archive.org item {identifier} identifies {remote_youtube_id!r}, not {video_id!r}"
            )
        if _metadata_first(metadata.get("archive_status")) != "complete":
            raise UploadVerificationError(f"archive.org item {identifier} is not complete")
        manifest_hash = _metadata_first(metadata.get("archive_manifest_sha256"))
        if not manifest_hash:
            raise UploadVerificationError(
                f"archive.org item {identifier} has no stored manifest hash"
            )
        manifest_bytes = self._download_remote_file(
            identifier, "archive-manifest.json", max_bytes=16 * 1024 * 1024
        )
        actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_manifest_hash != manifest_hash:
            raise UploadVerificationError(f"archive manifest hash mismatch for {identifier}")
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as exc:
            raise UploadVerificationError(
                f"Remote archive manifest is invalid JSON for {identifier}"
            ) from exc
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list) or not files:
            raise UploadVerificationError(
                f"Remote archive manifest lists no files for {identifier}"
            )
        expected: dict[str, int] = {}
        for entry in files:
            if not isinstance(entry, dict):
                raise UploadVerificationError(f"Invalid manifest file entry for {identifier}")
            name = entry.get("name")
            size = entry.get("size")
            if not isinstance(name, str) or not isinstance(size, int):
                raise UploadVerificationError(f"Invalid manifest file entry for {identifier}")
            expected[name] = size
        remote_files = self._wait_for_expected_files(item, identifier, expected)
        checksum_bytes = self._download_remote_file(
            identifier, "SHA256SUMS", max_bytes=4 * 1024 * 1024
        )
        checksum_entry = manifest.get("checksum_file") if isinstance(manifest, dict) else None
        if not isinstance(checksum_entry, dict):
            raise UploadVerificationError(
                f"Remote manifest has no checksum metadata for {identifier}"
            )
        expected_checksum_hash = checksum_entry.get("sha256")
        actual_checksum_hash = hashlib.sha256(checksum_bytes).hexdigest()
        if actual_checksum_hash != expected_checksum_hash:
            raise UploadVerificationError(f"Remote SHA256SUMS hash mismatch for {identifier}")
        metadata_checksum_hash = _metadata_first(metadata.get("archive_checksums_sha256"))
        if metadata_checksum_hash and metadata_checksum_hash != actual_checksum_hash:
            raise UploadVerificationError(f"Item metadata checksum hash differs for {identifier}")
        return ArchiveResult(
            identifier=identifier,
            item_url=self.item_url(identifier),
            status="verified",
            uploaded_files=0,
            remote_files=remote_files,
            total_size=sum(expected.values()),
            manifest_sha256=manifest_hash,
        )

    def verify_bundle(self, bundle: ArchiveBundle) -> ArchiveResult:
        item = self._retry(
            "read item metadata",
            lambda: self._get_item(
                bundle.identifier,
                config=self._ia_config,
                request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
            ),
        )
        if not item.exists:
            raise UploadVerificationError(f"archive.org item does not exist: {bundle.identifier}")
        self._assert_owned_item(item, bundle)
        remote_files = self._wait_for_remote_files(item, bundle)
        self._verify_remote_checksums(item, bundle)
        metadata = item.metadata or {}
        if _metadata_first(metadata.get("archive_status")) != "complete":
            raise UploadVerificationError(
                f"archive.org item {bundle.identifier} is not marked complete"
            )
        expected_manifest = bundle.manifest_artifact.sha256 if bundle.manifest_artifact else None
        remote_manifest = _metadata_first(metadata.get("archive_manifest_sha256"))
        if expected_manifest and remote_manifest != expected_manifest:
            raise UploadVerificationError(f"archive manifest hash mismatch for {bundle.identifier}")
        return ArchiveResult(
            identifier=bundle.identifier,
            item_url=self.item_url(bundle.identifier),
            status="verified",
            uploaded_files=0,
            remote_files=remote_files,
            total_size=bundle.total_size,
            manifest_sha256=expected_manifest,
        )

    def _wait_for_owned_item(
        self,
        identifier: str,
        video_id: str,
        *,
        timeout_seconds: int = 180,
    ) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_state = "not found"
        while True:
            try:
                item = self._get_item(
                    identifier,
                    config=self._ia_config,
                    request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT},
                )
            except Exception as exc:
                last_state = f"metadata read failed: {exc}"
            else:
                if item.exists:
                    youtube_id = _metadata_first((item.metadata or {}).get("youtube_id"))
                    if youtube_id == video_id:
                        return item
                    if youtube_id is not None:
                        raise ItemCollisionError(
                            f"archive.org identifier collision: {identifier} identifies "
                            f"{youtube_id!r}, not {video_id!r}"
                        )
                    last_state = "item exists but youtube_id metadata is not visible yet"
                else:
                    last_state = "item not found yet"
            if time.monotonic() >= deadline:
                raise UploadError(
                    f"Timed out waiting for archive.org item {identifier}: {last_state}"
                )
            self.logger.info("Waiting for archive.org item %s: %s", identifier, last_state)
            time.sleep(self.config.verify_poll_seconds)

    def _assert_owned_item(self, item: Any, bundle: ArchiveBundle) -> None:
        metadata = item.metadata or {}
        youtube_id = _metadata_first(metadata.get("youtube_id"))
        identifier = _metadata_first(metadata.get("identifier")) or item.identifier
        if youtube_id != bundle.video_id:
            raise ItemCollisionError(
                f"archive.org identifier collision: {bundle.identifier} exists but does "
                f"not identify YouTube video {bundle.video_id} (found {youtube_id!r})"
            )
        if identifier != bundle.identifier:
            raise ItemCollisionError(
                f"archive.org returned identifier {identifier!r} for {bundle.identifier!r}"
            )

    def _remote_is_complete(self, item: Any, bundle: ArchiveBundle) -> bool:
        metadata = item.metadata or {}
        if _metadata_first(metadata.get("archive_status")) != "complete":
            return False
        try:
            self._wait_for_remote_files(item, bundle, timeout_seconds=30)
            self._verify_remote_checksums(item, bundle)
        except UploadVerificationError:
            self.logger.warning(
                "Item %s is marked complete but remote files need repair",
                bundle.identifier,
            )
            return False
        return True

    def _wait_for_remote_files(
        self,
        item: Any,
        bundle: ArchiveBundle,
        *,
        timeout_seconds: int | None = None,
    ) -> int:
        expected = {artifact.remote_name: artifact.size for artifact in bundle.artifacts}
        return self._wait_for_expected_files(
            item,
            bundle.identifier,
            expected,
            timeout_seconds=timeout_seconds,
        )

    def _wait_for_expected_files(
        self,
        item: Any,
        identifier: str,
        expected: dict[str, int],
        *,
        timeout_seconds: int | None = None,
    ) -> int:
        deadline = time.monotonic() + (
            timeout_seconds if timeout_seconds is not None else self.config.verify_timeout_seconds
        )
        last_problem = "remote item has not indexed the uploaded files yet"
        while True:
            try:
                item.refresh(request_kwargs={"timeout": DEFAULT_REQUEST_TIMEOUT})
                remote = self._remote_file_map(item)
                missing: list[str] = []
                mismatched: list[str] = []
                for name, size in expected.items():
                    remote_file = remote.get(name)
                    if remote_file is None:
                        missing.append(name)
                    elif _file_size(remote_file) != size:
                        mismatched.append(f"{name} ({_file_size(remote_file)} != {size})")
                if not missing and not mismatched:
                    return len(remote)
                details = []
                if missing:
                    details.append(f"missing={missing[:5]}")
                if mismatched:
                    details.append(f"size_mismatch={mismatched[:5]}")
                last_problem = "; ".join(details)
            except Exception as exc:
                last_problem = f"remote file listing failed: {exc}"
            if time.monotonic() >= deadline:
                raise UploadVerificationError(
                    f"Timed out waiting for remote files for {identifier}: {last_problem}"
                )
            self.logger.info(
                "Waiting for archive.org indexing of %s: %s",
                identifier,
                last_problem,
            )
            time.sleep(self.config.verify_poll_seconds)

    def _verify_remote_checksums(self, item: Any, bundle: ArchiveBundle) -> None:
        checksum_artifact = bundle.checksum_artifact
        if checksum_artifact is None:
            raise UploadVerificationError("Local bundle has no SHA256SUMS artifact")
        remote_checksum = self._download_remote_file(
            bundle.identifier, checksum_artifact.remote_name, max_bytes=4 * 1024 * 1024
        )
        local_checksum = checksum_artifact.path.read_bytes()
        if remote_checksum != local_checksum:
            raise UploadVerificationError(
                f"Remote SHA256SUMS differs from the local file for {bundle.identifier}"
            )
        manifest_artifact = bundle.manifest_artifact
        if manifest_artifact is None:
            raise UploadVerificationError("Local bundle has no archive manifest")
        remote_manifest = self._download_remote_file(
            bundle.identifier, manifest_artifact.remote_name, max_bytes=16 * 1024 * 1024
        )
        if hashlib.sha256(remote_manifest).hexdigest() != manifest_artifact.sha256:
            raise UploadVerificationError(
                f"Remote archive manifest hash differs for {bundle.identifier}"
            )
        remote_meta_hash = _metadata_first((item.metadata or {}).get("archive_manifest_sha256"))
        if remote_meta_hash and remote_meta_hash != manifest_artifact.sha256:
            raise UploadVerificationError(
                f"Item metadata manifest hash differs for {bundle.identifier}"
            )
        remote_checksum_meta = _metadata_first(
            (item.metadata or {}).get("archive_checksums_sha256")
        )
        if remote_checksum_meta and remote_checksum_meta != checksum_artifact.sha256:
            raise UploadVerificationError(
                f"Item metadata checksum hash differs for {bundle.identifier}"
            )
        self.logger.info("Verified remote checksums for %s", bundle.identifier)

    def _download_remote_file(self, identifier: str, name: str, *, max_bytes: int) -> bytes:
        url = SCRIPT_URL.format(identifier=quote(identifier), name=quote(name))
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with self.http.get(
                    url,
                    timeout=(30, 180),
                    allow_redirects=True,
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(1024 * 128):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise UploadVerificationError(
                                f"Remote file {name} exceeded {max_bytes} bytes"
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
            except UploadVerificationError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 3:
                    self.logger.warning(
                        "Remote download of %s failed (attempt %d/3): %s",
                        name,
                        attempt,
                        exc,
                    )
                    time.sleep(5 * attempt)
        raise UploadVerificationError(
            f"Could not download remote {name} for {identifier}: {last_error}"
        )

    def _modify_metadata(self, item: Any, metadata: dict[str, Any]) -> None:
        cleaned = _clean_metadata(metadata)
        self._retry(
            "update item metadata",
            lambda: item.modify_metadata(
                cleaned,
                access_key=self.credentials.access_key,
                secret_key=self.credentials.secret_key,
                request_kwargs={"timeout": 120},
            ),
        )

    def _retry(self, label: str, function: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return function()
            except (ItemCollisionError, UploadVerificationError):
                raise
            except Exception as exc:
                last_error = exc
                if _is_auth_error(exc):
                    raise UploadError(
                        f"archive.org authentication failed while trying to {label}: {exc}"
                    ) from exc
                if attempt >= 3:
                    break
                sleep_seconds = min(30 * attempt, 120)
                self.logger.warning(
                    "archive.org %s failed (attempt %d/3): %s; retrying in %ds",
                    label,
                    attempt,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
        raise UploadError(f"archive.org failed to {label}: {last_error}") from last_error

    def _validate_bundle(self, bundle: ArchiveBundle) -> None:
        if not bundle.identifier.startswith(f"{self.config.identifier_prefix}-"):
            raise UploadError(
                f"Bundle identifier {bundle.identifier!r} does not start with "
                f"{self.config.identifier_prefix!r}"
            )
        names: set[str] = set()
        for artifact in bundle.artifacts:
            if artifact.remote_name in names:
                raise UploadError(f"Duplicate remote filename: {artifact.remote_name}")
            names.add(artifact.remote_name)
            if "/" in artifact.remote_name or "\\" in artifact.remote_name:
                raise UploadError(f"Unsafe remote filename requested: {artifact.remote_name!r}")
            if not artifact.path.is_file():
                raise UploadError(f"Local artifact disappeared: {artifact.path}")
        if bundle.checksum_artifact is None:
            raise UploadError("Bundle is missing SHA256SUMS")
        if bundle.manifest_artifact is None:
            raise UploadError("Bundle is missing archive-manifest.json")

    def _remote_file_map(self, item: Any) -> dict[str, Any]:
        try:
            files = list(item.get_files())
        except Exception as exc:
            raise UploadVerificationError(
                f"Could not list remote files for {item.identifier}: {exc}"
            ) from exc
        return {str(file.name): file for file in files}


def collection_exists(
    identifier: str,
    *,
    session: requests.Session | None = None,
) -> bool:
    client = session or requests
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.get(
                METADATA_URL.format(identifier=quote(identifier)),
                timeout=(30, 60),
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                return False
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                return False
            return bool(payload.get("is_collection") or metadata.get("mediatype") == "collection")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(5 * attempt)
    raise UploadError(
        f"Could not check archive.org collection {identifier!r}: {last_error}"
    ) from last_error


def _file_size(file: Any) -> int:
    value = getattr(file, "size", None)
    if value is None and isinstance(getattr(file, "metadata", None), dict):
        value = file.metadata.get("size")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _metadata_first(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    if value is None:
        return None
    return str(value)


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple)):
            cleaned = [str(item) for item in value if item is not None and str(item).strip()]
            if cleaned:
                result[key] = cleaned
            continue
        result[key] = value
    return result


def _is_auth_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "auth" in name or "authentication" in text or "access key" in text or "secret" in text
