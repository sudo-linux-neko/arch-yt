# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from __future__ import annotations

from plainbagel_archiver.state import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_LIVE,
    STATUS_RUNNING,
    DiscoveredVideo,
    StateStore,
)


def _videos() -> list[DiscoveredVideo]:
    return [
        DiscoveredVideo("aaaaaaaaaaa", "https://youtu.be/a", "A", "20240101", 10, "videos"),
        DiscoveredVideo("bbbbbbbbbbb", "https://youtu.be/b", "B", "20240102", 20, "shorts"),
    ]


def test_state_lifecycle_and_candidates(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        assert store.upsert_many(_videos()) == 2
        assert store.counts() == {"pending": 2}
        assert len(store.candidates(max_attempts=4)) == 2
        store.start_attempt("aaaaaaaaaaa")
        assert store.get("aaaaaaaaaaa").status == STATUS_RUNNING
        store.set_status("aaaaaaaaaaa", STATUS_FAILED, error="boom")
        assert store.get("aaaaaaaaaaa").last_error == "boom"
        store.start_attempt("bbbbbbbbbbb")
        store.set_status("bbbbbbbbbbb", STATUS_LIVE, error="live")
        candidates = store.candidates(max_attempts=1)
        assert {record.video_id for record in candidates} == {"bbbbbbbbbbb"}
        assert store.reset_failed() == 1
        store.mark_complete("aaaaaaaaaaa", "theplainbagel-aaaaaaaaaaa")
        record = store.get("aaaaaaaaaaa")
        assert record.status == STATUS_COMPLETE
        assert record.item_id == "theplainbagel-aaaaaaaaaaa"
        assert len(store.complete_records()) == 1
        assert store.reset_stale() == 0


def test_stale_and_retry_reset(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_many(_videos())
        store.start_attempt("aaaaaaaaaaa")
        assert store.reset_stale() == 1
        assert store.get("aaaaaaaaaaa").status == "interrupted"
        store.start_attempt("aaaaaaaaaaa")
        store.set_status("aaaaaaaaaaa", STATUS_FAILED, error="x")
        assert store.reset_failed() == 1
        assert store.get("aaaaaaaaaaa").status == "pending"


def test_only_ids_filter(tmp_path) -> None:
    with StateStore(tmp_path / "state.sqlite3") as store:
        store.upsert_many(_videos())
        records = store.candidates(max_attempts=4, only_ids=["bbbbbbbbbbb"])
        assert [record.video_id for record in records] == ["bbbbbbbbbbb"]
