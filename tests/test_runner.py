# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

import json
import os

import pytest

from plainbagel_archiver.archive_org import RemoteState
from plainbagel_archiver.errors import ArchiverError, LockError
from plainbagel_archiver.runner import (
    ArchiverRunner,
    RunLock,
    RunReport,
    build_collection_request,
)
from plainbagel_archiver.state import DiscoveredVideo, StateStore


def test_run_lock_excludes_concurrent_processes(tmp_path) -> None:
    path = tmp_path / "runner.lock"
    with RunLock(path), pytest.raises(LockError), RunLock(path):
        pass
    with RunLock(path):
        assert path.exists()
    assert not path.exists()


def test_run_lock_replaces_dead_pid(tmp_path) -> None:
    path = tmp_path / "runner.lock"
    path.write_text(json.dumps({"pid": 99999999, "started_at": "old"}))
    with RunLock(path):
        payload = json.loads(path.read_text())
        assert payload["pid"] == os.getpid()


def test_collection_request_requires_complete_items(app_config, tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(ArchiverError):
            build_collection_request(app_config, store)
        store.upsert_many(
            [
                DiscoveredVideo(
                    "AbC_-123xyZ",
                    "https://youtu.be/AbC_-123xyZ",
                    "A title",
                    "20240101",
                    10,
                    "videos",
                )
            ]
        )
        store.start_attempt("AbC_-123xyZ")
        store.mark_complete("AbC_-123xyZ", "theplainbagel-AbC_-123xyZ")
        text = build_collection_request(app_config, store)
    assert "info@archive.org" in text
    assert "https://archive.org/details/theplainbagel-AbC_-123xyZ" in text
    assert "non-commercial" in text
    assert "at least 50" in text


def test_cleanup_removes_completed_video_directory(app_config) -> None:
    from dataclasses import replace

    config = replace(app_config, cleanup_after_upload=True)
    runner = ArchiverRunner(config, __import__("logging").getLogger("test"))
    target = config.downloads_dir / "AbC_-123xyZ"
    target.mkdir(parents=True)
    (target / "x").write_text("x")
    runner._cleanup("AbC_-123xyZ")
    assert not target.exists()


class FakeArchiveClient:
    def __init__(self, state: RemoteState):
        self.state = state

    def remote_state(self, identifier: str, video_id: str) -> RemoteState:
        return self.state


def _prepared_store(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.open()
    store.upsert_many(
        [
            DiscoveredVideo(
                "AbC_-123xyZ",
                "https://youtu.be/AbC_-123xyZ",
                "A title",
                "20240101",
                10,
                "videos",
            )
        ]
    )
    store.start_attempt("AbC_-123xyZ")
    return store


def test_remote_complete_item_is_skipped(app_config, tmp_path) -> None:
    runner = ArchiverRunner(app_config, __import__("logging").getLogger("test"))
    report = RunReport()
    store = _prepared_store(tmp_path)
    try:
        state = RemoteState(
            identifier="theplainbagel-AbC_-123xyZ",
            exists=True,
            owned=True,
            complete=True,
            youtube_id="AbC_-123xyZ",
            item_url="https://archive.org/details/theplainbagel-AbC_-123xyZ",
        )
        assert runner._remote_already_complete(
            FakeArchiveClient(state), "AbC_-123xyZ", "running", False, store, report
        )
        assert store.get("AbC_-123xyZ").status == "complete"
        assert report.skipped == 1
    finally:
        store.close()


def test_remote_collision_marks_failed(app_config, tmp_path) -> None:
    runner = ArchiverRunner(app_config, __import__("logging").getLogger("test"))
    report = RunReport()
    store = _prepared_store(tmp_path)
    try:
        state = RemoteState(
            identifier="theplainbagel-AbC_-123xyZ",
            exists=True,
            owned=False,
            complete=True,
            youtube_id="OTHERVIDEO1",
            item_url="https://archive.org/details/theplainbagel-AbC_-123xyZ",
        )
        assert runner._remote_already_complete(
            FakeArchiveClient(state), "AbC_-123xyZ", "running", False, store, report
        )
        assert store.get("AbC_-123xyZ").status == "failed"
        assert report.failed == 1
    finally:
        store.close()
