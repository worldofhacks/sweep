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
