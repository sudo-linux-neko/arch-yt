# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig
from .errors import MediaError
from .formats import ArchivePlan, AudioFormat, RenditionPlan, VideoFormat
from .media import (
    mux_rendition,
    probe_media,
    validate_media,
    write_chapter_metadata,
)
from .metadata import (
    build_item_metadata,
    full_description,
    upload_date_from_info,
    write_metadata_files,
)
from .utils import (
    atomic_write_text,
    check_free_space,
    human_bytes,
    sha256_file,
    utc_iso,
)
from .youtube import AuxiliaryFiles, YtDlpClient

ARTIFACT_ORDER = {
    "rights": 0,
    "description": 1,
    "metadata": 2,
    "source_info": 3,
    "chapters": 4,
    "thumbnail": 5,
    "subtitle": 6,
    "video": 7,
    "manifest": 8,
    "checksums": 9,
}


@dataclass(frozen=True)
class Artifact:
    path: Path
    remote_name: str
    kind: str
    sha256: str
    size: int
    description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.remote_name,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "description": self.description,
        }


@dataclass(frozen=True)
class ArchiveBundle:
    video_id: str
    identifier: str
    captured_at: str
    directory: Path
    artifacts: tuple[Artifact, ...]
    plan: ArchivePlan
    warnings: tuple[str, ...]

    @property
    def total_size(self) -> int:
        return sum(artifact.size for artifact in self.artifacts)

    def artifact(self, name: str) -> Artifact | None:
        return next((artifact for artifact in self.artifacts if artifact.remote_name == name), None)

    @property
    def checksum_artifact(self) -> Artifact | None:
        return self.artifact("SHA256SUMS")

    @property
    def manifest_artifact(self) -> Artifact | None:
        return self.artifact("archive-manifest.json")


class BundleBuilder:
    def __init__(self, config: AppConfig, youtube: YtDlpClient, logger: logging.Logger):
        self.config = config
        self.youtube = youtube
        self.logger = logger

    def build(
        self,
        info: dict[str, Any],
        plan: ArchivePlan,
        *,
        identifier: str,
        captured_at: str,
        auxiliary: AuxiliaryFiles,
    ) -> ArchiveBundle:
        video_id = str(info.get("id") or "")
        if not video_id:
            raise MediaError("Cannot build an archive bundle without a video ID")
        video_root = self.config.downloads_dir / video_id
        bundle_dir = video_root / "bundle"
        source_dir = video_root / "source"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        source_dir.mkdir(parents=True, exist_ok=True)
        check_free_space(self.config.work_dir, self.config.min_free_gb)

        warnings = list(plan.warnings) + list(auxiliary.warnings)
        source_map: dict[str, Path] = {}
        audio_map: dict[str, Path] = {}
        chapter_metadata = write_chapter_metadata(info, video_root)

        entries: list[RenditionPlan] = list(plan.renditions)
        if plan.master:
            entries.append(plan.master)

        for rendition in entries:
            video_source = self._ensure_video_source(
                video_id, rendition.video, source_dir, source_map
            )
            audio_source: Path | None = None
            if rendition.audio is not None:
                audio_source = self._ensure_audio_source(
                    video_id, rendition.audio, source_dir, audio_map
                )
            output = bundle_dir / rendition.filename
            if output.exists():
                try:
                    validate_media(
                        output,
                        expected_codec=rendition.video.codec,
                        expected_width=rendition.video.width,
                        expected_height=rendition.video.height,
                        expected_duration=_duration_float(info),
                        require_audio=True,
                    )
                    self.logger.info("Reusing existing validated output %s", output.name)
                    continue
                except MediaError:
                    self.logger.warning("Removing invalid existing output %s", output.name)
                    output.unlink(missing_ok=True)
            check_free_space(self.config.work_dir, self.config.min_free_gb)
            mux_rendition(
                video_path=video_source,
                audio_path=audio_source,
                chapters_path=chapter_metadata,
                output_path=output,
                container=rendition.container,
                expected_codec=rendition.video.codec,
                expected_width=rendition.video.width,
                expected_height=rendition.video.height,
                expected_duration=_duration_float(info),
                video_has_audio=rendition.video.has_audio,
                audio_codec=rendition.audio.codec if rendition.audio else None,
                metadata=_container_metadata(self.config, info, rendition),
                logger=self.logger,
            )
            check_free_space(self.config.work_dir, self.config.min_free_gb)

        auxiliary_outputs = self._copy_auxiliary(auxiliary, bundle_dir)
        metadata_outputs = write_metadata_files(
            self.config,
            info,
            plan,
            bundle_dir,
            identifier=identifier,
            captured_at=captured_at,
            auxiliary_files=auxiliary_outputs,
        )

        payload_files: list[tuple[Path, str, str, str]] = []
        for name in (
            "RIGHTS-AND-ATTRIBUTION.txt",
            "DESCRIPTION.txt",
            "metadata.json",
            "source.info.json",
            "chapters.txt",
        ):
            path = metadata_outputs.get(name)
            if path:
                payload_files.append((path, name, _kind_for_name(name), _file_description(name)))
        if auxiliary_outputs.get("thumbnail.jpg"):
            payload_files.append(
                (
                    auxiliary_outputs["thumbnail.jpg"],
                    "thumbnail.jpg",
                    "thumbnail",
                    "Source thumbnail supplied by YouTube.",
                )
            )
        for name, path in sorted(auxiliary_outputs.items()):
            if name.startswith("captions-"):
                payload_files.append(
                    (
                        path,
                        name,
                        "subtitle",
                        "Captions captured from YouTube at archive time.",
                    )
                )
        for rendition in entries:
            path = bundle_dir / rendition.filename
            payload_files.append(
                (
                    path,
                    rendition.filename,
                    "video",
                    _video_description(rendition),
                )
            )

        payload_artifacts = [
            _artifact(path, name, kind, description)
            for path, name, kind, description in payload_files
        ]
        checksum_lines = [
            f"{artifact.sha256}  {artifact.remote_name}" for artifact in payload_artifacts
        ]
        checksum_path = bundle_dir / "SHA256SUMS"
        atomic_write_text(checksum_path, "\n".join(checksum_lines) + "\n")
        checksum_artifact = _artifact(
            checksum_path,
            "SHA256SUMS",
            "checksums",
            "SHA-256 checksums for all payload files in this item.",
        )

        manifest = _build_manifest(
            config=self.config,
            info=info,
            plan=plan,
            identifier=identifier,
            captured_at=captured_at,
            artifacts=payload_artifacts,
            checksum_artifact=checksum_artifact,
            warnings=warnings,
        )
        manifest_path = bundle_dir / "archive-manifest.json"
        atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        manifest_artifact = _artifact(
            manifest_path,
            "archive-manifest.json",
            "manifest",
            "Machine-readable manifest describing this preservation item.",
        )

        artifacts = [
            *payload_artifacts,
            manifest_artifact,
            checksum_artifact,
        ]
        artifacts.sort(
            key=lambda artifact: (ARTIFACT_ORDER.get(artifact.kind, 99), artifact.remote_name)
        )
        self.logger.info(
            "Built bundle for %s: %d files, %s",
            video_id,
            len(artifacts),
            human_bytes(sum(artifact.size for artifact in artifacts)),
        )
        return ArchiveBundle(
            video_id=video_id,
            identifier=identifier,
            captured_at=captured_at,
            directory=bundle_dir,
            artifacts=tuple(artifacts),
            plan=plan,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _ensure_video_source(
        self,
        video_id: str,
        video: VideoFormat,
        source_dir: Path,
        source_map: dict[str, Path],
    ) -> Path:
        existing = source_map.get(video.format_id)
        if existing:
            return existing
        path = source_dir / f"video-{video.format_id}.{video.ext}"
        if path.exists():
            try:
                probe = probe_media(path)
                if (
                    probe.video_codec == video.codec
                    and probe.width == video.width
                    and probe.height == video.height
                ):
                    source_map[video.format_id] = path
                    return path
            except MediaError:
                pass
            self.logger.warning("Removing invalid partial source %s", path.name)
            path.unlink(missing_ok=True)
        check_free_space(self.config.work_dir, self.config.min_free_gb)
        downloaded = self.youtube.download_format(
            video_id,
            video.format_id,
            path,
            expected_ext=video.ext,
        )
        probe = probe_media(downloaded)
        if probe.video_codec != video.codec:
            downloaded.unlink(missing_ok=True)
            downloaded = self.youtube.download_format(
                video_id,
                video.format_id,
                path,
                expected_ext=video.ext,
            )
            probe = probe_media(downloaded)
        if (
            probe.video_codec != video.codec
            or probe.width != video.width
            or probe.height != video.height
        ):
            raise MediaError(
                f"Downloaded source {downloaded.name} does not match the selected "
                f"{video.width}x{video.height} {video.codec} stream"
            )
        source_map[video.format_id] = downloaded
        return downloaded

    def _ensure_audio_source(
        self,
        video_id: str,
        audio: AudioFormat,
        source_dir: Path,
        audio_map: dict[str, Path],
    ) -> Path:
        existing = audio_map.get(audio.format_id)
        if existing:
            return existing
        path = source_dir / f"audio-{audio.format_id}.{audio.ext}"
        if path.exists():
            try:
                probe = probe_media(path, require_video=False)
                if probe.has_audio:
                    audio_map[audio.format_id] = path
                    return path
            except MediaError:
                pass
            self.logger.warning("Removing invalid partial audio %s", path.name)
            path.unlink(missing_ok=True)
        check_free_space(self.config.work_dir, self.config.min_free_gb)
        downloaded = self.youtube.download_format(
            video_id,
            audio.format_id,
            path,
            expected_ext=audio.ext,
        )
        probe = probe_media(downloaded, require_video=False)
        if not probe.has_audio:
            downloaded.unlink(missing_ok=True)
            downloaded = self.youtube.download_format(
                video_id,
                audio.format_id,
                path,
                expected_ext=audio.ext,
            )
            probe = probe_media(downloaded, require_video=False)
        if not probe.has_audio:
            raise MediaError(f"Downloaded audio {downloaded.name} has no audio stream")
        audio_map[audio.format_id] = downloaded
        return downloaded

    def _copy_auxiliary(
        self,
        auxiliary: AuxiliaryFiles,
        bundle_dir: Path,
    ) -> dict[str, Path]:
        outputs: dict[str, Path] = {}
        if auxiliary.thumbnail and auxiliary.thumbnail.is_file():
            target = bundle_dir / "thumbnail.jpg"
            shutil.copy2(auxiliary.thumbnail, target)
            outputs["thumbnail.jpg"] = target
        for subtitle in auxiliary.subtitles:
            if not subtitle.is_file() or not subtitle.name.startswith("captions-"):
                continue
            target = bundle_dir / subtitle.name
            shutil.copy2(subtitle, target)
            outputs[subtitle.name] = target
        return outputs


def build_item_metadata_for_bundle(
    config: AppConfig,
    info: dict[str, Any],
    bundle: ArchiveBundle,
    *,
    archive_status: str = "uploading",
) -> dict[str, Any]:
    return build_item_metadata(
        config,
        info,
        bundle.plan,
        identifier=bundle.identifier,
        captured_at=bundle.captured_at,
        archive_status=archive_status,
    )


def remove_video_workdir(config: AppConfig, video_id: str) -> None:
    root = config.downloads_dir.resolve()
    target = (config.downloads_dir / video_id).resolve()
    if root not in target.parents:
        raise MediaError(f"Refusing to delete path outside work directory: {target}")
    if target.exists():
        shutil.rmtree(target)
        with contextlib.suppress(OSError):
            target.parent.rmdir()


def _artifact(path: Path, name: str, kind: str, description: str) -> Artifact:
    if not path.is_file():
        raise MediaError(f"Artifact is missing: {path}")
    if path.stat().st_size <= 0 and kind != "description":
        raise MediaError(f"Artifact is empty: {path}")
    return Artifact(
        path=path,
        remote_name=name,
        kind=kind,
        sha256=sha256_file(path),
        size=path.stat().st_size,
        description=description,
    )


def _build_manifest(
    *,
    config: AppConfig,
    info: dict[str, Any],
    plan: ArchivePlan,
    identifier: str,
    captured_at: str,
    artifacts: list[Artifact],
    checksum_artifact: Artifact,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "schema": "plainbagel-preservation-bundle/v1",
        "identifier": identifier,
        "video_id": info.get("id"),
        "generated_at": utc_iso(),
        "captured_at": captured_at,
        "source": {
            "url": f"https://www.youtube.com/watch?v={info.get('id')}",
            "channel_url": config.channel_url,
            "title": info.get("title"),
            "upload_date": upload_date_from_info(info),
            "description_file": "DESCRIPTION.txt",
            "description_length": len(full_description(info)),
            "source_metadata_file": "source.info.json",
        },
        "rights": {
            "copyright_holder": f"{config.creator_name} / {config.channel_name}",
            "rights_file": "RIGHTS-AND-ATTRIBUTION.txt",
            "public_domain": False,
            "commercial_license": False,
            "permission_summary_only": True,
        },
        "plan": plan.as_dict(),
        "files": [artifact.as_dict() for artifact in artifacts],
        "checksum_file": checksum_artifact.as_dict(),
        "warnings": warnings,
    }


def _container_metadata(
    config: AppConfig,
    info: dict[str, Any],
    rendition: RenditionPlan,
) -> dict[str, str]:
    video_id = str(info.get("id") or "")
    title = str(info.get("title") or video_id)
    description = (
        f"Non-commercial preservation copy of The Plain Bagel video {video_id}. "
        f"Original: https://www.youtube.com/watch?v={video_id}. "
        "Copyright remains with Richard Coffin / The Plain Bagel. All rights reserved. "
        "Not public domain. No commercial license granted. "
        f"Archiver: {config.archiver_email}."
    )
    date = upload_date_from_info(info)
    metadata = {
        "title": title,
        "artist": f"{config.creator_name} / {config.channel_name}",
        "album": config.channel_name,
        "comment": description,
        "copyright": f"(c) {config.creator_name} / {config.channel_name}. All rights reserved.",
        "encoder": "plainbagel-archiver / ffmpeg",
        "genre": "Education",
    }
    if date:
        metadata["date"] = date
    metadata["description"] = (
        f"Archived rendition: requested {rendition.requested_height}p "
        f"{rendition.requested_codec.upper()}, actual {rendition.video.quality_height}p "
        f"{rendition.video.codec.upper()}."
    )
    return metadata


def _duration_float(info: dict[str, Any]) -> float | None:
    value = info.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _video_description(rendition: RenditionPlan) -> str:
    if rendition.slot == "master":
        return (
            f"Preservation master: best available source stream "
            f"({rendition.video.quality_height}p {rendition.video.codec.upper()}), "
            "remuxed without video re-encoding."
        )
    return (
        f"Requested {rendition.requested_height}p {rendition.requested_codec.upper()} "
        f"rendition, sourced as {rendition.video.quality_height}p "
        f"{rendition.video.codec.upper()}, remuxed without video re-encoding."
    )


def _kind_for_name(name: str) -> str:
    return {
        "RIGHTS-AND-ATTRIBUTION.txt": "rights",
        "DESCRIPTION.txt": "description",
        "metadata.json": "metadata",
        "source.info.json": "source_info",
        "chapters.txt": "chapters",
    }[name]


def _file_description(name: str) -> str:
    return {
        "RIGHTS-AND-ATTRIBUTION.txt": "Copyright, permission, contacts, and source attribution.",
        "DESCRIPTION.txt": "Complete unmodified YouTube description.",
        "metadata.json": "Structured archive and source metadata.",
        "source.info.json": "Complete yt-dlp metadata captured at download time.",
        "chapters.txt": "Chapter timestamps from the source metadata.",
    }[name]
