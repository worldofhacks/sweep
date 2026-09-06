from __future__ import annotations

import json
from pathlib import Path

from relay.session_report import (
    build_session_report,
    load_jsonl_records,
    report_path,
    write_session_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "session-report.jsonl"
SESSION = "session-report"


def test_jsonl_fixture_preserves_command_refusal_telemetry_and_timing(tmp_path: Path) -> None:
    report = build_session_report(SESSION, load_jsonl_records(FIXTURE))

    assert [event["command_id"] for event in report["commands"]] == ["command-1"]
    assert [event["reason"] for event in report["refusals"]] == ["operator_absent"]
    assert [event["battery"] for event in report["telemetry"]] == [0.8, 0.7]
    assert report["telemetry_summary"] == {
        "1": {
            "samples": 2,
            "first_at": 1000,
            "last_at": 1020,
            "battery_min": 0.7,
            "battery_max": 0.8,
        }
    }
    assert report["timing"] == {
        "started_at": 1000,
        "ended_at": 1060,
        "duration_ms": 60,
        "command_acknowledgements": [
            {
                "command_id": "command-1",
                "issued_at": 1030,
                "first_acknowledged_at": 1040,
                "completed_at": 1060,
                "completion_latency_ms": 30,
            }
        ],
    }

    output = write_session_report(tmp_path, SESSION, load_jsonl_records(FIXTURE))

    assert output == report_path(tmp_path, SESSION)
    assert json.loads(output.read_text()) == report


def test_report_reads_commands_and_acknowledgements_from_real_relay_audit(
    relay_session, adapter_principal, console_principal, clock
) -> None:
    from relay.tests.conftest import acknowledgement_payload, intent_payload, telemetry_payload
    from relay.tests.test_session_bridge import _issue_hover, _join

    _join(relay_session, adapter_principal)
    relay_session.process_telemetry(
        telemetry_payload(event_id="report-telemetry"), adapter_principal
    )
    relay_session.process_intent(intent_payload(), console_principal)
    _issue_hover(relay_session, "report-command")
    clock.advance(50)
    events = relay_session.process_acknowledgement(
        acknowledgement_payload(
            timestamp=clock(),
            event_id="report-ack",
            command_id="report-command",
            status="completed",
        ),
        adapter_principal,
    )
    assert events[0]["status"] == "completed"
    report = build_session_report(relay_session.session_id, relay_session.audit_log.replay())
    assert report["commands"][0]["command_id"] == "report-command"
    assert report["timing"]["command_acknowledgements"][0]["completion_latency_ms"] == 50
    assert report["telemetry_summary"]["1"]["samples"] == 1
