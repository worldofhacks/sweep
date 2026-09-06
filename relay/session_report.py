"""Build durable, portable evidence reports from a relay session's audit records."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path


def load_jsonl_records(path: Path) -> list[dict[str, object]]:
    """Load the JSONL record shape emitted by ``SessionAuditLog``."""
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"cannot read session evidence: {error}") from None
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL evidence at line {line_number}: {error.msg}") from None
        if not isinstance(record, dict) or not isinstance(record.get("event"), dict):
            raise ValueError(f"invalid JSONL evidence at line {line_number}")
        records.append(record)
    return records


def build_session_report(
    session_id: str, records: Iterable[Mapping[str, object]]
) -> dict[str, object]:
    """Summarize commands, refusals, telemetry, and their recorded timing."""
    events: list[dict[str, object]] = []
    for record in records:
        event = record.get("event")
        if not isinstance(event, Mapping) or event.get("session") != session_id:
            raise ValueError("session evidence contains an event for another session")
        events.append(dict(event))

    commands = _events_of_type(events, "command")
    refusals = _events_of_type(events, "refusal")
    telemetry = _events_of_type(events, "telemetry")
    timestamps = [event["t"] for event in events if _timestamp(event.get("t"))]
    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None
    return {
        "v": 1,
        "type": "session_report",
        "session": session_id,
        "event_count": len(events),
        "commands": commands,
        "refusals": refusals,
        "telemetry": telemetry,
        "telemetry_summary": _telemetry_summary(telemetry),
        "timing": {
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": (
                None if started_at is None or ended_at is None else ended_at - started_at
            ),
            "command_acknowledgements": _command_timings(commands, events),
        },
    }


def report_path(log_dir: Path, session_id: str) -> Path:
    """Return the deterministic report path beside the session's audit log."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    return log_dir / f"{digest}.report.json"


def write_session_report(
    log_dir: Path, session_id: str, records: Iterable[Mapping[str, object]]
) -> Path:
    """Atomically replace the current report after all supplied records are summarized."""
    output = report_path(log_dir, session_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(build_session_report(session_id, records), indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, output)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return output


def _events_of_type(events: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    return [event for event in events if event.get("type") == event_type]


def _telemetry_summary(telemetry: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for sample in telemetry:
        drone_id = sample.get("drone")
        if not isinstance(drone_id, int) or isinstance(drone_id, bool):
            continue
        timestamp = sample.get("t")
        if not _timestamp(timestamp):
            continue
        entry = summary.setdefault(
            str(drone_id),
            {
                "samples": 0,
                "first_at": timestamp,
                "last_at": timestamp,
                "battery_min": None,
                "battery_max": None,
            },
        )
        entry["samples"] = int(entry["samples"]) + 1
        entry["first_at"] = min(int(entry["first_at"]), timestamp)
        entry["last_at"] = max(int(entry["last_at"]), timestamp)
        battery = sample.get("battery")
        if isinstance(battery, int | float) and not isinstance(battery, bool):
            minimum = entry["battery_min"]
            maximum = entry["battery_max"]
            entry["battery_min"] = battery if minimum is None else min(float(minimum), battery)
            entry["battery_max"] = battery if maximum is None else max(float(maximum), battery)
    return summary


def _command_timings(
    commands: list[dict[str, object]], events: list[dict[str, object]]
) -> list[dict[str, object]]:
    acknowledgements: dict[str, list[dict[str, object]]] = {}
    for event in events:
        command_id = event.get("command_id")
        if event.get("type") == "acknowledgement" and isinstance(command_id, str):
            acknowledgements.setdefault(command_id, []).append(event)
    timings: list[dict[str, object]] = []
    for command in commands:
        command_id = command.get("command_id")
        issued_at = command.get("t")
        if not isinstance(command_id, str) or not _timestamp(issued_at):
            continue
        related = acknowledgements.get(command_id, [])
        observed_at = [event["t"] for event in related if _timestamp(event.get("t"))]
        terminal_at = [
            event["t"]
            for event in related
            if event.get("status") in {"completed", "failed", "invalidated"}
            and _timestamp(event.get("t"))
        ]
        first_acknowledged_at = min(observed_at) if observed_at else None
        completed_at = max(terminal_at) if terminal_at else None
        timings.append(
            {
                "command_id": command_id,
                "issued_at": issued_at,
                "first_acknowledged_at": first_acknowledged_at,
                "completed_at": completed_at,
                "completion_latency_ms": (
                    None if completed_at is None else completed_at - issued_at
                ),
            }
        )
    return timings


def _timestamp(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
