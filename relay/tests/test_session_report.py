from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import monotonic

import pytest

from planner.models import CommandOperation
from relay.audit import AuditLogError, SessionAuditLog
from relay.contracts import LifecycleStatus, acknowledgement_event, command_event, refusal_event
from relay.session_report import MAX_REPORT_RECORDS, report_path, write_session_report
from relay.tests.conftest import SESSION, telemetry_payload


def _event(event_id: str, timestamp: int) -> dict[str, object]:
    return {
        "v": 1,
        "t": timestamp,
        "type": "operator_presence",
        "event_id": event_id,
        "session": SESSION,
        "source": "console",
        "activity": "interaction",
    }


def _evidence(audit: SessionAuditLog) -> None:
    audit.append_batch(
        [
            telemetry_payload(event_id="telemetry-1", timestamp=1_000),
            telemetry_payload(event_id="telemetry-2", timestamp=1_020),
            command_event(
                t=1_030,
                event_id="command-event-1",
                session=SESSION,
                command_id="command-1",
                intent_id="intent-1",
                roster_version=1,
                drone_id=1,
                connection_epoch=1,
                seq=1,
                issued_at=1_030,
                ttl_ms=2_000,
                operation=CommandOperation.HOVER,
                args={},
            ),
            acknowledgement_event(
                t=1_040,
                event_id="ack-accepted",
                session=SESSION,
                intent_id="intent-1",
                command_id="command-1",
                status=LifecycleStatus.ACCEPTED,
                roster_version=1,
                source="adapter",
                drone_id=1,
                connection_epoch=1,
            ),
            refusal_event(
                t=1_050,
                event_id="refusal-1",
                session=SESSION,
                intent_id="intent-2",
                reason="operator_absent",
                detail="operator activity is stale",
                roster_version=1,
            ),
            acknowledgement_event(
                t=1_060,
                event_id="ack-completed",
                session=SESSION,
                intent_id="intent-1",
                command_id="command-1",
                status=LifecycleStatus.COMPLETED,
                roster_version=1,
                source="adapter",
                drone_id=1,
                connection_epoch=1,
            ),
        ]
    )


def test_report_uses_exact_audit_snapshot_with_provenance_and_timing(tmp_path: Path) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    _evidence(audit)

    output = write_session_report(
        audit,
        generated_at_ms=2_000,
        complete=True,
        completion_reason="orderly_shutdown",
    )
    report = json.loads(output.read_text())

    assert output == report_path(tmp_path, SESSION)
    assert [event["command_id"] for event in report["commands"]] == ["command-1"]
    assert [event["reason"] for event in report["refusals"]] == ["operator_absent"]
    assert [event["battery"] for event in report["telemetry"]] == [0.8, 0.8]
    assert report["completion"] == {
        "status": "complete",
        "reason": "orderly_shutdown",
        "generated_at_ms": 2_000,
    }
    assert report["provenance"] == {
        "source": "session_audit_log",
        "audit_sha256": hashlib.sha256(audit.path.read_bytes()).hexdigest(),
        "last_sequence": 6,
        "record_count": 6,
    }
    assert report["timing"]["duration_ms"] == 60
    assert report["timing"]["command_acknowledgements"] == [
        {
            "command_id": "command-1",
            "intent_id": "intent-1",
            "drone_id": 1,
            "connection_epoch": 1,
            "issued_at": 1_030,
            "first_acknowledged_at": 1_040,
            "ack_timestamp_precedes_issuance": False,
            "terminal_at": 1_060,
            "terminal_status": "completed",
            "terminal_latency_ms": 30,
        }
    ]


def test_report_reads_real_relay_audit(
    relay_session, adapter_principal, console_principal, clock
) -> None:
    from relay.tests.conftest import acknowledgement_payload, intent_payload
    from relay.tests.test_session_bridge import _issue_hover, _join

    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(
        telemetry_payload(event_id="report-telemetry"), adapter_principal
    )
    relay_session.process_intent(intent_payload(), console_principal)
    _issue_hover(relay_session, "report-command")
    clock.advance(50)
    relay_session.process_acknowledgement(
        acknowledgement_payload(
            timestamp=clock(),
            event_id="report-ack",
            command_id="report-command",
            status="completed",
        ),
        adapter_principal,
    )

    output = write_session_report(
        relay_session.audit_log,
        generated_at_ms=clock(),
        complete=True,
        completion_reason="orderly_shutdown",
    )
    report = json.loads(output.read_text())
    assert report["commands"][0]["command_id"] == "report-command"
    assert report["timing"]["command_acknowledgements"][0]["terminal_latency_ms"] == 50
    assert report["telemetry_summary"]["1"]["samples"] == 1


def test_report_accepts_repeated_executing_progress_acknowledgements(tmp_path: Path) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    command = command_event(
        t=1_000,
        event_id="command-event-1",
        session=SESSION,
        command_id="command-1",
        intent_id="intent-1",
        roster_version=1,
        drone_id=1,
        connection_epoch=1,
        seq=1,
        issued_at=1_000,
        ttl_ms=5_000,
        operation=CommandOperation.HOVER,
        args={},
    )
    acknowledgements = [
        acknowledgement_event(
            t=timestamp,
            event_id=event_id,
            session=SESSION,
            intent_id="intent-1",
            command_id="command-1",
            status=status,
            roster_version=1,
            source="adapter",
            drone_id=1,
            connection_epoch=1,
            detail=detail,
        )
        for timestamp, event_id, status, detail in (
            (1_010, "ack-accepted", LifecycleStatus.ACCEPTED, None),
            (1_020, "ack-executing-1", LifecycleStatus.EXECUTING, "hovering: 1 second"),
            (1_030, "ack-executing-2", LifecycleStatus.EXECUTING, "hovering: 2 seconds"),
            (1_040, "ack-completed", LifecycleStatus.COMPLETED, None),
        )
    ]
    audit.append_batch([command, *acknowledgements])

    output = write_session_report(
        audit,
        generated_at_ms=2_000,
        complete=True,
        completion_reason="orderly_shutdown",
    )

    report = json.loads(output.read_text())
    assert report["timing"]["command_acknowledgements"] == [
        {
            "command_id": "command-1",
            "intent_id": "intent-1",
            "drone_id": 1,
            "connection_epoch": 1,
            "issued_at": 1_000,
            "first_acknowledged_at": 1_010,
            "ack_timestamp_precedes_issuance": False,
            "terminal_at": 1_040,
            "terminal_status": "completed",
            "terminal_latency_ms": 40,
        }
    ]


def test_incomplete_shutdown_is_explicit(tmp_path: Path) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    audit.append(_event("presence-1", 1_000))

    output = write_session_report(
        audit,
        generated_at_ms=1_001,
        complete=False,
        completion_reason="worker_deadline_exceeded",
    )

    assert json.loads(output.read_text())["completion"]["status"] == "incomplete"


def test_report_record_bound_covers_the_six_aircraft_acceptance_session() -> None:
    assert MAX_REPORT_RECORDS > 6 * 10 * 15 * 60


def test_report_accepts_presence_evidence_through_the_six_aircraft_sim_ceiling(
    tmp_path: Path,
) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    targets = [{"drone_id": drone_id, "connection_epoch": 1} for drone_id in range(1, 7)]
    audit.append(
        {
            "v": 1,
            "t": 1_000,
            "type": "safety_action",
            "event_id": "presence-six",
            "session": SESSION,
            "reason": "operator_presence_expired",
            "action": "hold",
            "operator_last_seen_ms": 900,
            "status": "requested",
            "attempt": 1,
            "intent_id": "safety:operator-presence:900:1",
            "targets": targets,
        }
    )

    output = write_session_report(
        audit,
        generated_at_ms=1_001,
        complete=True,
        completion_reason="orderly_shutdown",
    )

    assert json.loads(output.read_text())["safety_actions"][0]["targets"] == targets


def test_report_rejects_presence_evidence_above_the_simulator_ceiling(tmp_path: Path) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    audit.append(
        {
            "v": 1,
            "t": 1_000,
            "type": "safety_action",
            "event_id": "presence-seven",
            "session": SESSION,
            "reason": "operator_presence_expired",
            "action": "hold",
            "operator_last_seen_ms": 900,
            "status": "requested",
            "attempt": 1,
            "intent_id": "safety:operator-presence:900:1",
            "targets": [{"drone_id": drone_id, "connection_epoch": 1} for drone_id in range(1, 8)],
        }
    )

    with pytest.raises(ValueError, match="invalid presence safety action"):
        write_session_report(
            audit,
            generated_at_ms=1_001,
            complete=True,
            completion_reason="orderly_shutdown",
        )


def test_report_rejects_a_mispaired_acknowledgement(tmp_path: Path) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    command = command_event(
        t=2_000,
        event_id="command-event-1",
        session=SESSION,
        command_id="command-1",
        intent_id="intent-1",
        roster_version=1,
        drone_id=1,
        connection_epoch=1,
        seq=1,
        issued_at=2_000,
        ttl_ms=2_000,
        operation=CommandOperation.HOVER,
        args={},
    )
    acknowledgement = acknowledgement_event(
        t=2_010,
        event_id="ack-1",
        session=SESSION,
        intent_id="intent-1",
        command_id="command-1",
        status=LifecycleStatus.COMPLETED,
        roster_version=1,
        source="adapter",
        drone_id=1,
        connection_epoch=1,
    )
    acknowledgement["connection_epoch"] = 2
    audit.append_batch([command, acknowledgement])

    with pytest.raises(ValueError, match="identity does not match"):
        write_session_report(
            audit,
            generated_at_ms=3_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )


def test_report_flags_an_accepted_adapter_clock_inversion(
    relay_session, adapter_principal, clock
) -> None:
    from relay.tests.conftest import acknowledgement_payload
    from relay.tests.test_session_bridge import _issue_hover, _join

    _join(relay_session, adapter_principal)
    clock.advance(100)
    command = _issue_hover(relay_session, "clock-inversion")
    acknowledgement = relay_session.process_acknowledgement(
        acknowledgement_payload(
            timestamp=clock() - 5,
            event_id="clock-inversion-ack",
            command_id="clock-inversion",
            status="completed",
        ),
        adapter_principal,
    )
    assert acknowledgement[0]["status"] == "completed"

    output = write_session_report(
        relay_session.audit_log,
        generated_at_ms=clock(),
        complete=True,
        completion_reason="orderly_shutdown",
    )
    timing = json.loads(output.read_text())["timing"]["command_acknowledgements"][0]

    assert timing["issued_at"] == command["issued_at"]
    assert timing["first_acknowledged_at"] == clock() - 5
    assert timing["terminal_at"] == clock() - 5
    assert timing["ack_timestamp_precedes_issuance"] is True
    assert timing["terminal_latency_ms"] is None


def test_report_rejects_structural_drift_and_duplicate_event_identity(tmp_path: Path) -> None:
    malformed = SessionAuditLog(tmp_path / "malformed", SESSION)
    malformed.append({**telemetry_payload(event_id="telemetry-1"), "extra": True})
    with pytest.raises(ValueError, match="invalid telemetry"):
        write_session_report(
            malformed,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )

    duplicate = SessionAuditLog(tmp_path / "duplicate", SESSION)
    duplicate.append_batch([_event("same", 1_000), _event("same", 1_001)])
    with pytest.raises(ValueError, match="duplicate event_id"):
        write_session_report(
            duplicate,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )


def test_report_enforces_source_bounds_and_deadline(tmp_path: Path, monkeypatch) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    audit.append_batch([_event("presence-1", 1_000), _event("presence-2", 1_001)])
    monkeypatch.setattr("relay.session_report.MAX_REPORT_RECORDS", 1)
    with pytest.raises(AuditLogError, match="record limit"):
        write_session_report(
            audit,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )
    monkeypatch.setattr("relay.session_report.MAX_REPORT_RECORDS", 10)
    monkeypatch.setattr("relay.session_report.MAX_REPORT_SOURCE_BYTES", 1)
    with pytest.raises(AuditLogError, match="byte limit"):
        write_session_report(
            audit,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )
    with pytest.raises(AuditLogError, match="deadline"):
        write_session_report(
            audit,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
            deadline=monotonic() - 1,
        )


def test_report_bounds_the_jsonl_mirror_before_reading_it(tmp_path: Path, monkeypatch) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    event = _event("presence-1", 1_000)
    audit.append(event)
    event_bytes = len(json.dumps(event, separators=(",", ":"), sort_keys=True).encode())
    assert audit.path.stat().st_size > event_bytes
    monkeypatch.setattr("relay.session_report.MAX_REPORT_SOURCE_BYTES", event_bytes)

    with pytest.raises(AuditLogError, match="mirror exceeds"):
        write_session_report(
            audit,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
        )


def test_report_deadline_is_checked_inside_record_transformation(
    tmp_path: Path, monkeypatch
) -> None:
    audit = SessionAuditLog(tmp_path, SESSION)
    audit.append_batch([_event(f"presence-{index}", 1_000 + index) for index in range(20)])
    deadline = monotonic() + 60
    calls = 0

    def expiring_clock() -> float:
        nonlocal calls
        calls += 1
        return deadline - 1 if calls < 8 else deadline + 1

    monkeypatch.setattr("relay.session_report.monotonic", expiring_clock)

    with pytest.raises(AuditLogError, match="deadline"):
        write_session_report(
            audit,
            generated_at_ms=2_000,
            complete=True,
            completion_reason="orderly_shutdown",
            deadline=deadline,
        )
    assert calls == 8
