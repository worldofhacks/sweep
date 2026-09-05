"""The committed bridge-core vectors must be exactly what the relay code renders."""

import json

from adapters.dji_mini3 import vectors
from relay.auth import verify_event_signature
from relay.contracts import (
    COMMAND_ARGUMENT_FIELDS,
    CommandOperation,
    WatchdogState,
    parse_adapter_acknowledgement,
    parse_capabilities,
    parse_capture_readiness,
    parse_command,
    parse_membership_request,
    parse_node_status,
    parse_telemetry,
)
from relay.settings import RelaySettings


def test_fixture_files_are_current() -> None:
    for name, text in vectors.render().items():
        path = vectors.FIXTURE_DIR / name
        assert path.is_file(), f"{path} missing; run `uv run python -m adapters.dji_mini3.vectors`"
        assert path.read_text(encoding="utf-8") == text, (
            f"{path} is stale; run `uv run python -m adapters.dji_mini3.vectors`"
        )


def test_fixture_files_are_plain_json() -> None:
    for name, text in vectors.render().items():
        json.loads(text)
        assert name.endswith(".json")


def test_membership_vectors_are_valid_relay_frames() -> None:
    frames = vectors.frame_vectors()
    for name in ("membership_join", "membership_readiness", "membership_graceful_leave"):
        entry = frames[name]
        request = parse_membership_request(entry["wire"])
        assert verify_event_signature(
            request.unsigned_event(), entry["wire"]["signature"], entry["key"].encode()
        )


def test_telemetry_and_acknowledgement_vectors_parse() -> None:
    frames = vectors.frame_vectors()
    telemetry = parse_telemetry(frames["telemetry"]["wire"])
    assert telemetry.drone == 1
    acknowledgement = parse_adapter_acknowledgement(frames["acknowledgement"]["wire"])
    assert acknowledgement.reason == "stale_command"


def test_command_vector_carries_every_phase_a_field() -> None:
    wire = vectors.frame_vectors()["command"]["wire"]
    assert set(wire) == {
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
        "signature",
    }
    unsigned = {key: value for key, value in wire.items() if key != "signature"}
    assert verify_event_signature(unsigned, wire["signature"], vectors.NODE_KEY.encode())


def test_command_and_args_vectors_parse_with_the_relay_contract() -> None:
    frames = vectors.frame_vectors()
    command = parse_command(frames["command"]["wire"])
    assert command.operation is CommandOperation.GOTO
    assert command.args == {"x_mm": 1000, "y_mm": 2500, "z_mm": 1200, "speed_mm_s": 500}
    assert verify_event_signature(
        command.unsigned_event(), command.signature, vectors.NODE_KEY.encode()
    )
    samples = frames["command_args"]
    assert set(samples) == {operation.value for operation in CommandOperation}
    for operation, fields in COMMAND_ARGUMENT_FIELDS.items():
        unsigned = {**vectors.command_unsigned(), "operation": operation.value}
        unsigned["args"] = samples[operation.value]
        parsed = parse_command({**unsigned, "signature": "x"})
        assert set(parsed.args) == set(fields), operation


def test_node_authored_vectors_parse_with_the_relay_contract() -> None:
    frames = vectors.frame_vectors()
    capabilities = parse_capabilities(frames["capabilities"]["wire"])
    readiness = parse_capture_readiness(frames["capture_readiness"]["wire"])
    status = parse_node_status(frames["node_status"]["wire"])

    assert capabilities.sdk_version == "5.18.0"
    assert capabilities.measured_hfov_deg is None
    assert readiness.storage_ok is True
    assert readiness.coverage_missing == (90.0, 135.0)
    assert status.watchdog_state is WatchdogState.NOMINAL
    assert status.phone_battery_percent == 72


def test_node_settings_vector_is_the_relay_default() -> None:
    settings = RelaySettings(relay_token=vectors.NODE_KEY.encode())
    accepted = vectors.frame_vectors()["auth_accepted"]["wire"]

    assert vectors.node_settings() == settings.node_settings()
    assert accepted["node"] == settings.node_settings()


def test_canonical_cases_are_distinct_and_named() -> None:
    cases = vectors.canonical_json_cases()
    names = [case["name"] for case in cases]
    assert len(names) == len(set(names))
    assert len(cases) >= 20
