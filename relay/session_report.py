"""Bounded evidence reports derived only from a validated relay audit snapshot."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from time import monotonic

from relay.audit import AuditLogError, SessionAuditLog
from relay.contracts import ContractError, parse_command, parse_telemetry

MAX_REPORT_RECORDS = 50_000
MAX_REPORT_SOURCE_BYTES = 32 * 1024 * 1024
MAX_REPORT_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_JSON_DEPTH = 12
_MAX_CONTAINER_ITEMS = 4_096
_MAX_STRING_CHARS = 8_192
_MAX_IDENTIFIER_CHARS = 512
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_DEFAULT_REPORT_DEADLINE_SECONDS = 5.0
_COMMAND_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "command_id",
        "intent_id",
        "roster_version",
        "drone_id",
        "connection_epoch",
        "seq",
        "issued_at",
        "ttl_ms",
        "operation",
        "args",
    }
)
_OUTCOME_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "intent_id",
        "command_id",
        "status",
        "source",
        "drone_id",
        "connection_epoch",
        "roster_version",
        "reason",
        "detail",
    }
)
_NODE_SAFETY_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "drone_id",
        "connection_epoch",
        "reason",
        "action",
        "loss_behavior",
    }
)
_PRESENCE_SAFETY_FIELDS = frozenset(
    {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "reason",
        "action",
        "operator_last_seen_ms",
        "status",
        "attempt",
        "intent_id",
        "targets",
    }
)


def report_path(log_dir: Path, session_id: str) -> Path:
    """Return the deterministic report path beside the session's audit log."""
    digest = hashlib.sha256(session_id.encode()).hexdigest()
    return log_dir / f"{digest}.report.json"


def write_session_report(
    audit_log: SessionAuditLog,
    *,
    generated_at_ms: int,
    complete: bool,
    completion_reason: str,
    deadline: float | None = None,
) -> Path:
    """Write one bounded report from the audit log's canonical committed snapshot."""
    if not _timestamp(generated_at_ms):
        raise ValueError("generated_at_ms must be a non-negative safe integer")
    expected_reason = "orderly_shutdown" if complete else "worker_deadline_exceeded"
    if completion_reason != expected_reason:
        raise ValueError(f"completion_reason must be {expected_reason}")
    deadline = monotonic() + _DEFAULT_REPORT_DEADLINE_SECONDS if deadline is None else deadline
    _check_deadline(deadline)
    records, last_sequence = audit_log.replay_snapshot(
        deadline=deadline,
        max_records=MAX_REPORT_RECORDS,
        max_bytes=MAX_REPORT_SOURCE_BYTES,
    )
    report = _build_session_report(
        audit_log.session,
        records,
        last_sequence=last_sequence,
        generated_at_ms=generated_at_ms,
        complete=complete,
        completion_reason=completion_reason,
    )
    try:
        encoded = (json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n").encode()
    except (TypeError, ValueError) as error:
        raise ValueError(f"session report is not strict JSON: {error}") from None
    if len(encoded) > MAX_REPORT_OUTPUT_BYTES:
        raise ValueError("session report exceeds the output byte limit")
    _check_deadline(deadline)

    output = report_path(audit_log.root, audit_log.session)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _check_deadline(deadline)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def _build_session_report(
    session_id: str,
    records: list[dict[str, object]],
    *,
    last_sequence: int,
    generated_at_ms: int,
    complete: bool,
    completion_reason: str,
) -> dict[str, object]:
    if not session_id or len(session_id) > _MAX_IDENTIFIER_CHARS:
        raise ValueError("session report requires a bounded session identity")
    if last_sequence != len(records):
        raise ValueError("audit snapshot cursor does not match its record count")

    source_digest = hashlib.sha256()
    event_ids: set[str] = set()
    commands: list[dict[str, object]] = []
    refusals: list[dict[str, object]] = []
    telemetry: list[dict[str, object]] = []
    safety_actions: list[dict[str, object]] = []
    acknowledgements: list[tuple[int, dict[str, object]]] = []
    command_sequences: dict[str, int] = {}
    timestamps: list[int] = []
    source_bytes = 0

    for expected_sequence, record in enumerate(records, start=1):
        if set(record) != {"seq", "event"} or record.get("seq") != expected_sequence:
            raise ValueError("audit snapshot contains a non-contiguous record")
        _bounded_json(record)
        event = record.get("event")
        if not isinstance(event, dict):
            raise ValueError("audit snapshot record is missing its event")
        _validate_base_event(event, session_id)
        event_id = str(event["event_id"])
        if event_id in event_ids:
            raise ValueError("audit snapshot contains a duplicate event_id")
        event_ids.add(event_id)
        timestamps.append(int(event["t"]))
        canonical = (
            json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode()
        source_bytes += len(canonical)
        if source_bytes > MAX_REPORT_SOURCE_BYTES:
            raise ValueError("audit snapshot exceeds the report source byte limit")
        source_digest.update(canonical)

        event_type = event["type"]
        if event_type == "command":
            _validate_command(event)
            command_id = str(event["command_id"])
            if command_id in command_sequences:
                raise ValueError("audit snapshot contains a duplicate command_id")
            command_sequences[command_id] = expected_sequence
            commands.append(dict(event))
        elif event_type == "acknowledgement":
            _validate_acknowledgement(event)
            acknowledgements.append((expected_sequence, dict(event)))
        elif event_type == "refusal":
            _validate_refusal(event)
            refusals.append(dict(event))
        elif event_type == "telemetry":
            _validate_telemetry(event)
            telemetry.append(dict(event))
        elif event_type == "safety_action":
            _validate_safety_action(event)
            safety_actions.append(dict(event))

    return {
        "v": 1,
        "type": "session_report",
        "session": session_id,
        "completion": {
            "status": "complete" if complete else "incomplete",
            "reason": completion_reason,
            "generated_at_ms": generated_at_ms,
        },
        "provenance": {
            "source": "session_audit_log",
            "audit_sha256": source_digest.hexdigest(),
            "last_sequence": last_sequence,
            "record_count": len(records),
        },
        "event_count": len(records),
        "commands": commands,
        "refusals": refusals,
        "telemetry": telemetry,
        "safety_actions": safety_actions,
        "telemetry_summary": _telemetry_summary(telemetry),
        "timing": {
            "started_at": min(timestamps) if timestamps else None,
            "ended_at": max(timestamps) if timestamps else None,
            "duration_ms": max(timestamps) - min(timestamps) if timestamps else None,
            "command_acknowledgements": _command_timings(
                commands, command_sequences, acknowledgements
            ),
        },
    }


def _validate_base_event(event: Mapping[str, object], session_id: str) -> None:
    if (
        event.get("v") != 1
        or not _timestamp(event.get("t"))
        or not _identifier(event.get("event_id"))
        or event.get("session") != session_id
        or not _identifier(event.get("type"))
    ):
        raise ValueError("audit snapshot contains an invalid event envelope")


def _validate_command(event: Mapping[str, object]) -> None:
    if set(event) != _COMMAND_FIELDS:
        raise ValueError("audit snapshot contains an invalid command shape")
    try:
        parse_command({**event, "signature": "report-validation"})
    except ContractError as error:
        raise ValueError(f"audit snapshot contains an invalid command: {error.code}") from None


def _validate_telemetry(event: Mapping[str, object]) -> None:
    try:
        parse_telemetry(event)
    except ContractError as error:
        raise ValueError(f"audit snapshot contains invalid telemetry: {error.code}") from None


def _validate_acknowledgement(event: Mapping[str, object]) -> None:
    if set(event) != _OUTCOME_FIELDS:
        raise ValueError("audit snapshot contains an invalid acknowledgement shape")
    status = event.get("status")
    command_id = event.get("command_id")
    if (
        not _identifier(event.get("intent_id"))
        or status not in {"accepted", "executing", "completed", "failed", "invalidated"}
        or not _identifier(event.get("source"))
        or not _nonnegative_int(event.get("roster_version"))
        or not _nullable_identifier(event.get("reason"))
        or not _nullable_string(event.get("detail"))
    ):
        raise ValueError("audit snapshot contains an invalid acknowledgement")
    if status in {"failed", "invalidated"} and event.get("reason") is None:
        raise ValueError("failed acknowledgements require a reason")
    if command_id is None:
        if not _nullable_positive_int(event.get("drone_id")) or not _nullable_positive_int(
            event.get("connection_epoch")
        ):
            raise ValueError("intent acknowledgement has invalid optional identity")
        return
    if (
        event.get("source") != "adapter"
        or not _identifier(command_id)
        or not _positive_int(event.get("drone_id"))
        or not _positive_int(event.get("connection_epoch"))
    ):
        raise ValueError("command acknowledgement lacks its adapter identity")


def _validate_refusal(event: Mapping[str, object]) -> None:
    if set(event) != _OUTCOME_FIELDS or event.get("status") != "refused":
        raise ValueError("audit snapshot contains an invalid refusal shape")
    if (
        not _nullable_identifier(event.get("intent_id"))
        or not _nullable_identifier(event.get("command_id"))
        or not _identifier(event.get("source"))
        or not _identifier(event.get("reason"))
        or not _bounded_string(event.get("detail"))
        or not _nonnegative_int(event.get("roster_version"))
        or not _nullable_positive_int(event.get("drone_id"))
        or not _nullable_positive_int(event.get("connection_epoch"))
    ):
        raise ValueError("audit snapshot contains an invalid refusal")


def _validate_safety_action(event: Mapping[str, object]) -> None:
    if event.get("reason") == "link_loss":
        if (
            set(event) != _NODE_SAFETY_FIELDS
            or event.get("action") not in {"hold", "failsafe"}
            or event.get("loss_behavior") not in {"hold", "failsafe"}
            or not _positive_int(event.get("drone_id"))
            or not _positive_int(event.get("connection_epoch"))
        ):
            raise ValueError("audit snapshot contains an invalid node safety action")
        return
    if event.get("reason") != "operator_presence_expired" or set(event) != _PRESENCE_SAFETY_FIELDS:
        raise ValueError("audit snapshot contains an unknown safety action")
    status = event.get("status")
    attempt = event.get("attempt")
    targets = event.get("targets")
    if (
        event.get("action") not in {"hold", "estop"}
        or status
        not in {"requested", "retrying", "awaiting", "confirmed", "failed", "not_required"}
        or not _nonnegative_int(event.get("operator_last_seen_ms"))
        or not _nonnegative_int(attempt)
        or not isinstance(targets, list)
        or len(targets) > 4
    ):
        raise ValueError("audit snapshot contains an invalid presence safety action")
    seen: set[int] = set()
    for target in targets:
        if (
            not isinstance(target, Mapping)
            or set(target) != {"drone_id", "connection_epoch"}
            or not _positive_int(target.get("drone_id"))
            or not _positive_int(target.get("connection_epoch"))
            or int(target["drone_id"]) in seen
        ):
            raise ValueError("presence safety action has an invalid target identity")
        seen.add(int(target["drone_id"]))
    if status == "not_required":
        if attempt != 0 or event.get("intent_id") is not None or targets:
            raise ValueError("not-required presence action cannot identify an attempt")
    elif not _positive_int(attempt) or not _identifier(event.get("intent_id")) or not targets:
        raise ValueError("active presence action requires an attempt, intent, and targets")


def _command_timings(
    commands: list[dict[str, object]],
    command_sequences: Mapping[str, int],
    acknowledgements: list[tuple[int, dict[str, object]]],
) -> list[dict[str, object]]:
    by_command: dict[str, list[tuple[int, dict[str, object]]]] = {}
    for sequence, acknowledgement in acknowledgements:
        command_id = acknowledgement["command_id"]
        if command_id is not None:
            by_command.setdefault(str(command_id), []).append((sequence, acknowledgement))
    if set(by_command) - set(command_sequences):
        raise ValueError("audit snapshot acknowledges an unknown command_id")

    timings: list[dict[str, object]] = []
    for command in commands:
        command_id = str(command["command_id"])
        issued_at = int(command["issued_at"])
        related = by_command.get(command_id, [])
        terminal: dict[str, object] | None = None
        last_rank = -1
        seen_statuses: set[str] = set()
        for sequence, acknowledgement in related:
            if sequence <= command_sequences[command_id]:
                raise ValueError("command acknowledgement precedes its command")
            if any(
                acknowledgement[field] != command[field]
                for field in ("intent_id", "roster_version", "drone_id", "connection_epoch")
            ):
                raise ValueError("command acknowledgement identity does not match its command")
            acknowledged_at = int(acknowledgement["t"])
            if acknowledged_at < issued_at:
                raise ValueError("command acknowledgement timestamp precedes issuance")
            status = str(acknowledgement["status"])
            rank = 0 if status == "accepted" else 1 if status == "executing" else 2
            if status in seen_statuses or rank < last_rank or terminal is not None:
                raise ValueError("command acknowledgement lifecycle is out of order")
            seen_statuses.add(status)
            last_rank = rank
            if rank == 2:
                terminal = acknowledgement
        first_at = None if not related else int(related[0][1]["t"])
        terminal_at = None if terminal is None else int(terminal["t"])
        timings.append(
            {
                "command_id": command_id,
                "intent_id": command["intent_id"],
                "drone_id": command["drone_id"],
                "connection_epoch": command["connection_epoch"],
                "issued_at": issued_at,
                "first_acknowledged_at": first_at,
                "terminal_at": terminal_at,
                "terminal_status": None if terminal is None else terminal["status"],
                "terminal_latency_ms": None if terminal_at is None else terminal_at - issued_at,
            }
        )
    return timings


def _telemetry_summary(telemetry: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for sample in telemetry:
        drone_id = int(sample["drone"])
        timestamp = int(sample["t"])
        battery = float(sample["battery"])
        entry = summary.setdefault(
            str(drone_id),
            {
                "samples": 0,
                "first_at": timestamp,
                "last_at": timestamp,
                "battery_min": battery,
                "battery_max": battery,
            },
        )
        entry["samples"] = int(entry["samples"]) + 1
        entry["first_at"] = min(int(entry["first_at"]), timestamp)
        entry["last_at"] = max(int(entry["last_at"]), timestamp)
        entry["battery_min"] = min(float(entry["battery_min"]), battery)
        entry["battery_max"] = max(float(entry["battery_max"]), battery)
    return summary


def _bounded_json(value: object, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("audit snapshot exceeds the JSON depth limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError("audit snapshot integer exceeds the portable JSON range")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("audit snapshot contains a non-finite number")
        return
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            raise ValueError("audit snapshot string exceeds the report limit")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError("audit snapshot object exceeds the field limit")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > _MAX_IDENTIFIER_CHARS:
                raise ValueError("audit snapshot contains an invalid object key")
            _bounded_json(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise ValueError("audit snapshot array exceeds the item limit")
        for item in value:
            _bounded_json(item, depth + 1)
        return
    raise ValueError("audit snapshot contains a non-JSON value")


def _timestamp(value: object) -> bool:
    return _nonnegative_int(value)


def _nonnegative_int(value: object) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_SAFE_INTEGER
    )


def _positive_int(value: object) -> bool:
    return _nonnegative_int(value) and int(value) > 0


def _nullable_positive_int(value: object) -> bool:
    return value is None or _positive_int(value)


def _bounded_string(value: object) -> bool:
    return isinstance(value, str) and len(value) <= _MAX_STRING_CHARS


def _nullable_string(value: object) -> bool:
    return value is None or _bounded_string(value)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _MAX_IDENTIFIER_CHARS


def _nullable_identifier(value: object) -> bool:
    return value is None or _identifier(value)


def _check_deadline(deadline: float) -> None:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, int | float)
        or not math.isfinite(deadline)
    ):
        raise AuditLogError("session report has an invalid shutdown deadline")
    if monotonic() >= deadline:
        raise AuditLogError("session report exceeded the shutdown deadline")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
