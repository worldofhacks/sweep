from __future__ import annotations

from dataclasses import replace

import pytest

from relay.captures import CaptureLedger, CaptureLedgerError
from relay.contracts import parse_capture_bundle, parse_media_file
from relay.tests.conftest import capture_bundle_payload, media_file_payload, media_record


def _file(capture_id: str, file_id: str):
    return parse_media_file(
        media_file_payload(
            event_id=f"event-{capture_id}-{file_id}",
            capture_id=capture_id,
            file_id=file_id,
        )
    ).file


def test_capture_ledger_bounds_capture_and_file_cardinality_without_partial_mutation() -> None:
    ledger = CaptureLedger(max_entries=2, max_media_per_capture=2)
    ledger.record_media(_file("capture-1", "file-1"), t=1)
    ledger.record_media(_file("capture-1", "file-2"), t=2)
    before = ledger.projection()

    with pytest.raises(CaptureLedgerError, match="2 files"):
        ledger.record_media(_file("capture-1", "file-3"), t=3)
    assert ledger.projection() == before

    ledger.record_media(_file("capture-2", "file-1"), t=4)
    before = ledger.projection()
    with pytest.raises(CaptureLedgerError, match="2 open captures"):
        ledger.record_media(_file("capture-3", "file-1"), t=5)
    assert ledger.projection() == before


def test_capture_ledger_evicts_the_oldest_closed_entry_but_never_an_open_one() -> None:
    ledger = CaptureLedger(max_entries=2)
    first = parse_capture_bundle(
        capture_bundle_payload(
            event_id="bundle-1",
            capture_id="capture-1",
            media=[media_record(capture_id="capture-1", file_id="file-1")],
        )
    )
    ledger.record_bundle(first, t=1)
    ledger.record_media(_file("capture-2", "file-1"), t=2)

    ledger.record_media(_file("capture-3", "file-1"), t=3)

    assert [entry["capture_id"] for entry in ledger.projection()] == ["capture-2", "capture-3"]


def test_authoritative_bundle_closes_and_labels_the_capture() -> None:
    ledger = CaptureLedger()
    record = media_record(capture_id="capture-1", file_id="file-1")
    bundle = parse_capture_bundle(
        capture_bundle_payload(
            event_id="bundle-1",
            capture_id="capture-1",
            room_id="forged-room",
            pattern="reconstruct_8",
            coverage="incomplete_vertical_coverage",
            media=[record],
        )
    )

    ledger.record_bundle(bundle, t=1)
    (autonomy_projection,) = ledger.projection()
    assert autonomy_projection["room_id"] == "forged-room"
    assert autonomy_projection["pattern"] == "reconstruct_8"
    assert autonomy_projection["status"] == "completed"


def test_entry_snapshot_restores_only_the_changed_capture() -> None:
    ledger = CaptureLedger()
    first = _file("capture-1", "file-1")
    ledger.record_media(first, t=1)
    key = (first.drone_id, first.connection_epoch, first.capture_id)
    before = ledger.snapshot_entry(key)
    ledger.record_media(_file("capture-1", "file-2"), t=2)

    ledger.restore_entry(key, before)

    assert [item["file_id"] for item in ledger.projection()[0]["files"]] == ["file-1"]


def test_media_retrieval_preserves_shutter_evidence_and_cannot_regress() -> None:
    ledger = CaptureLedger()
    pending = parse_media_file(
        media_file_payload(
            event_id="pending",
            retrieval_status="pending",
            checksum_sha256="0" * 64,
        )
    ).file
    ledger.record_media(pending, t=1)
    completed = replace(
        pending,
        checksum_sha256="b" * 64,
        storage_ref="file:///captures/capture-1/file-1.jpg",
        retrieval_status="completed",
    )
    ledger.record_media(completed, t=2)

    before = ledger.projection()
    with pytest.raises(CaptureLedgerError) as regression:
        ledger.record_media(pending, t=3)
    assert regression.value.code == "capture_status_regression"
    assert ledger.projection() == before

    changed_pose = replace(completed, timestamp_ms=completed.timestamp_ms + 1)
    with pytest.raises(CaptureLedgerError) as mismatch:
        ledger.record_media(changed_pose, t=4)
    assert mismatch.value.code == "capture_evidence_mismatch"
    assert ledger.projection() == before

    changed_checksum = replace(completed, checksum_sha256="c" * 64)
    with pytest.raises(CaptureLedgerError) as conflict:
        ledger.record_media(changed_checksum, t=5)
    assert conflict.value.code == "capture_record_conflict"
    assert ledger.projection() == before


def test_conflicting_bundle_is_atomic() -> None:
    ledger = CaptureLedger()
    first = _file("capture-1", "file-1")
    second = _file("capture-1", "file-2")
    ledger.record_media(first, t=1)
    before = ledger.projection()
    bundle = parse_capture_bundle(
        capture_bundle_payload(
            event_id="bundle-conflict",
            capture_id="capture-1",
            media=[second.to_dict(), replace(first, timestamp_ms=first.timestamp_ms + 1).to_dict()],
        )
    )

    with pytest.raises(CaptureLedgerError) as mismatch:
        ledger.record_bundle(bundle, t=2)
    assert mismatch.value.code == "capture_evidence_mismatch"
    assert ledger.projection() == before
