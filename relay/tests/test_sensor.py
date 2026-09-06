"""Sensor frames: bounds, per-device rate limiting, digest-only audit, fan-out, retention."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import relay.contracts as contracts_module
from planner.models import DeviceClass
from relay.app import RelayRuntime
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal
from relay.contracts import (
    MAX_SENSOR_FRAME_CANONICAL_BYTES,
    ContractError,
    DeviceIdentity,
    SensorFrame,
    SensorKind,
    parse_sensor,
)
from relay.session import (
    MAX_SENSOR_FRAMES_PER_SECOND,
    RelayLimits,
    RelaySession,
    _material_state_projection,
)
from relay.settings import RelaySettings
from relay.state import FleetRegistry
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    GROUND_ID,
    GROUND_KEY,
    SESSION,
    EventIds,
    MutableClock,
    ground_membership_payload,
    profiled_sink,
    sensor_payload,
)

GROUND = DeviceIdentity(DeviceClass.GROUND_VEHICLE, 1)
_MIN_INTERVAL_MS = 1_000 // MAX_SENSOR_FRAMES_PER_SECOND


def _session(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, **limits: int
) -> RelaySession:
    return RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(
            intent_max_age_ms=5_000,
            transport_event_max_age_ms=5_000,
            future_clock_skew_ms=1_000,
            telemetry_freshness_ms=1_000,
            **limits,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink=profiled_sink(lambda _intent, _state: None),
        devices={GROUND_ID: GROUND, 12: DeviceIdentity(DeviceClass.GROUND_VEHICLE, 2)},
    )


def _join(session: RelaySession, principal: Principal, event_id: str = "join-ground") -> None:
    assert principal.drone_id is not None
    events = session.process_frame(
        ground_membership_payload(
            action="join", event_id=event_id, timestamp=session.clock(), drone_id=principal.drone_id
        ),
        principal,
    )
    assert events[0]["type"] == "membership", events


def _audited(session: RelaySession) -> list[dict[str, object]]:
    return [record["event"] for record in session.replay()["events"]]


def test_parse_sensor_accepts_the_contract_frame_and_round_trips() -> None:
    raw = sensor_payload(event_id="scan-1")

    frame = parse_sensor(raw)

    assert isinstance(frame, SensorFrame)
    assert frame.kind is SensorKind.LIDAR_SCAN
    assert frame.pose.to_dict() == {"x": 1.2, "y": -0.4, "yaw_deg": 87.5}
    assert frame.angle_increment_deg == 1.0
    assert len(frame.ranges_cm) == 360
    assert frame.to_event() == raw
    digest = frame.digest_event()
    assert digest == {
        "v": 1,
        "t": raw["t"],
        "type": "sensor_digest",
        "event_id": "scan-1",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "connection_epoch": 1,
        "kind": "lidar_scan",
        "count": 360,
        "valid": 324,
        "min_cm": 200,
        "max_cm": 206,
    }
    for increment, count in ((0.5, 720), (2.0, 180)):
        parsed = parse_sensor(sensor_payload(event_id="scan", angle_increment_deg=increment))
        assert len(parsed.ranges_cm) == count


def test_parse_sensor_digest_reports_null_extrema_without_returns() -> None:
    frame = parse_sensor(sensor_payload(event_id="scan-empty", ranges_cm=[0] * 360))

    digest = frame.digest_event()

    assert (digest["count"], digest["valid"], digest["min_cm"], digest["max_cm"]) == (
        360,
        0,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"kind": "sonar"}, "kind must be one of"),
        ({"pose": {"x": 0.0, "y": 0.0}}, "fields do not match"),
        ({"pose": {"x": 0.0, "y": 0.0, "yaw_deg": 360.0}}, "yaw_deg"),
        ({"pose": {"x": 0.0, "y": 0.0, "yaw_deg": -1.0}}, "yaw_deg"),
        ({"pose": {"x": float("nan"), "y": 0.0, "yaw_deg": 0.0}}, "finite"),
        ({"angle_min_deg": 360.0}, "angle_min_deg"),
        ({"angle_increment_deg": 0.25}, "angle_increment_deg must be one of"),
        ({"angle_increment_deg": 3.0}, "angle_increment_deg must be one of"),
        ({"ranges_cm": [200] * 359}, "exactly 360 entries"),
        ({"ranges_cm": [200] * 361}, "exactly 360 entries"),
        ({"angle_increment_deg": 0.5, "ranges_cm": [200] * 360}, "exactly 720 entries"),
        ({"ranges_cm": [-1] + [200] * 359}, "0 through 65535"),
        ({"ranges_cm": [65_536] + [200] * 359}, "0 through 65535"),
        ({"ranges_cm": [1.5] + [200] * 359}, "0 through 65535"),
        ({"ranges_cm": [True] + [200] * 359}, "0 through 65535"),
        ({"ranges_cm": "200" * 360}, "must be a list"),
        ({"range_min_m": 12.0, "range_max_m": 0.15}, "range_min_m"),
        ({"range_min_m": -0.1}, "range_min_m"),
        ({"drone_id": 0}, "drone_id"),
        ({"connection_epoch": 0}, "connection_epoch"),
        ({"extra": 1}, "fields do not match"),
        ({"type": "telemetry"}, "type must be sensor"),
    ],
)
def test_parse_sensor_rejects_out_of_contract_frames(
    changes: dict[str, object], match: str
) -> None:
    raw = sensor_payload(event_id="scan-invalid", **changes)

    with pytest.raises(ContractError, match=match) as error:
        parse_sensor(raw)
    assert error.value.code == "invalid_sensor"


def test_parse_sensor_rejects_missing_fields_and_enforces_the_canonical_byte_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = sensor_payload(event_id="scan-missing")
    del raw["ranges_cm"]
    with pytest.raises(ContractError, match="fields do not match"):
        parse_sensor(raw)

    largest = sensor_payload(
        event_id="e" * 512,
        session="s" * 512,
        angle_increment_deg=0.5,
        ranges_cm=[65_535] * 720,
        pose={"x": -12345.678901, "y": 12345.678901, "yaw_deg": 359.999},
    )
    assert parse_sensor(largest).to_event() == largest
    monkeypatch.setattr(contracts_module, "MAX_SENSOR_FRAME_CANONICAL_BYTES", 4_096)
    with pytest.raises(ContractError, match="at most 4096 UTF-8 bytes"):
        parse_sensor(largest)
    assert MAX_SENSOR_FRAME_CANONICAL_BYTES == 8 * 1024


def test_session_fans_out_the_frame_retains_it_and_audits_only_a_digest(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, ground_principal)
    clock.advance(50)
    raw = sensor_payload(event_id="scan-1", timestamp=clock())

    events = session.process_frame(raw, ground_principal)

    assert events == [raw]
    assert [event["type"] for event in _audited(session)] == [
        "membership",
        "state",
        "sensor_digest",
    ]
    digest = _audited(session)[-1]
    assert (digest["event_id"], digest["count"], digest["valid"]) == ("scan-1", 360, 324)
    assert "ranges_cm" not in digest
    assert session.latest_sensor(GROUND_ID) is not None
    assert session.latest_sensor(GROUND_ID).to_event() == raw
    assert session.latest_sensor(1) is None
    assert session.metrics()["sensor_events"] == 1
    assert session.metrics()["sensor_frames_dropped_total"] == 0
    assert session.metrics()["node_events"] == 0
    drone = session.current_state()["drones"][0]
    assert drone["sensor"] == {"kind": "lidar_scan", "last_scan_at": clock()}


def test_frames_above_five_hertz_are_dropped_silently_and_counted(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, ground_principal)
    seen: list[SensorFrame] = []
    session.sensor_listeners.append(seen.append)
    clock.advance(1)

    accepted = session.process_frame(
        sensor_payload(event_id="scan-1", timestamp=clock()), ground_principal
    )
    clock.advance(_MIN_INTERVAL_MS - 1)
    dropped = session.process_frame(
        sensor_payload(event_id="scan-2", timestamp=clock()), ground_principal
    )
    clock.advance(1)
    accepted_again = session.process_frame(
        sensor_payload(event_id="scan-3", timestamp=clock()), ground_principal
    )

    assert [event["event_id"] for event in accepted] == ["scan-1"]
    assert dropped == []
    assert [event["event_id"] for event in accepted_again] == ["scan-3"]
    assert session.metrics()["sensor_events"] == 2
    assert session.metrics()["sensor_frames_dropped_total"] == 1
    assert [frame.event_id for frame in seen] == ["scan-1", "scan-3"]
    # A dropped frame was never claimed: its id and timestamp stay usable.
    clock.advance(_MIN_INTERVAL_MS)
    late = session.process_frame(
        sensor_payload(event_id="scan-2", timestamp=clock()), ground_principal
    )
    assert [event["event_id"] for event in late] == ["scan-2"]
    assert session.latest_sensor(GROUND_ID).event_id == "scan-2"
    # A relay clock that steps backwards reopens the window instead of dropping forever;
    # the transport ordering check, not the rate limiter, refuses the regressed frame.
    assert session._sensor_rate_exceeded(GROUND_ID, clock() - 5_000) is False
    assert session._sensor_rate_exceeded(GROUND_ID, clock() + 1) is True


def test_rate_limit_and_digest_sampling_are_per_device(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids, audit_state_interval_ms=1_000)
    other = Principal(source="adapter", drone_id=12, signing_key=GROUND_KEY)
    _join(session, ground_principal)
    _join(session, other, "join-other")
    clock.advance(1)

    for index in range(6):
        for principal in (ground_principal, other):
            session.process_frame(
                sensor_payload(
                    event_id=f"scan-{principal.drone_id}-{index}",
                    timestamp=clock(),
                    drone_id=principal.drone_id,
                ),
                principal,
            )
        clock.advance(_MIN_INTERVAL_MS)

    assert session.metrics()["sensor_events"] == 12
    assert session.metrics()["sensor_frames_dropped_total"] == 0
    digests = [event for event in _audited(session) if event["type"] == "sensor_digest"]
    assert [(digest["drone_id"], digest["event_id"]) for digest in digests] == [
        (GROUND_ID, "scan-11-0"),
        (12, "scan-12-0"),
        (GROUND_ID, "scan-11-5"),
        (12, "scan-12-5"),
    ]


def test_scan_timestamp_is_volatile_and_no_state_record_follows_a_scan(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, ground_principal)
    before = session.current_state()
    clock.advance(1)
    session.process_frame(sensor_payload(event_id="scan-1", timestamp=clock()), ground_principal)
    clock.advance(_MIN_INTERVAL_MS)
    session.process_frame(sensor_payload(event_id="scan-2", timestamp=clock()), ground_principal)
    after = session.current_state()

    assert before["drones"][0]["sensor"]["last_scan_at"] is None
    assert after["drones"][0]["sensor"]["last_scan_at"] == clock()
    assert _material_state_projection(before) == _material_state_projection(after)
    assert [event["type"] for event in _audited(session)].count("state") == 1

    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    state = registry.state_event(session=SESSION, t=1, event_id="s")
    state["drones"] = [dict(after["drones"][0])]
    state["drones"][0]["sensor"] = {"kind": "lidar_scan", "last_scan_at": None, "extra": 1}
    with pytest.raises(AuditLogError, match="sensor fields"):
        _material_state_projection(state)


def test_sensor_frames_are_checked_like_every_node_frame(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)

    unknown = session.process_frame(
        sensor_payload(event_id="scan-early", timestamp=clock()), ground_principal
    )
    assert unknown[0]["type"] == "refusal" and unknown[0]["reason"] == "unknown_aircraft"

    _join(session, ground_principal)
    clock.advance(1)
    aircraft = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    mismatch = session.process_frame(
        sensor_payload(event_id="scan-other", timestamp=clock()), aircraft
    )
    assert mismatch[0]["reason"] == "drone_identity_mismatch"
    stale = session.process_frame(
        sensor_payload(event_id="scan-stale", timestamp=clock(), connection_epoch=2),
        ground_principal,
    )
    assert stale[0]["reason"] == "stale_connection_epoch"
    malformed = session.process_frame(
        sensor_payload(event_id="scan-bad", timestamp=clock(), kind="sonar"), ground_principal
    )
    assert malformed[0]["reason"] == "invalid_sensor"
    console = session.process_frame(
        sensor_payload(event_id="scan-console", timestamp=clock()),
        Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY),
    )
    assert console[0]["reason"] == "frame_not_allowed"
    assert session.metrics()["sensor_events"] == 0
    assert session.metrics()["sensor_frames_dropped_total"] == 0
    assert session.latest_sensor(GROUND_ID) is None


def test_rejoin_clears_the_retained_scan(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, ground_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, ground_principal)
    clock.advance(1)
    session.process_frame(sensor_payload(event_id="scan-1", timestamp=clock()), ground_principal)
    assert session.latest_sensor(GROUND_ID) is not None

    clock.advance(1)
    session.handle_adapter_disconnect(drone_id=GROUND_ID, connection_epoch=1)
    assert session.latest_sensor(GROUND_ID) is None
    clock.advance(1)
    _join(session, ground_principal, "rejoin")

    assert session.latest_sensor(GROUND_ID) is None
    assert session.current_state()["drones"][0]["sensor"]["last_scan_at"] is None
    # The per-device rate window survives the rejoin: it bounds the device, not the epoch.
    clock.advance(_MIN_INTERVAL_MS)
    session.process_frame(
        sensor_payload(event_id="scan-2", timestamp=clock(), connection_epoch=2), ground_principal
    )
    assert session.latest_sensor(GROUND_ID).connection_epoch == 2


def test_listener_failures_are_logged_and_never_refuse_the_frame(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    ground_principal: Principal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, ground_principal)
    seen: list[int] = []

    def failing(frame: SensorFrame) -> None:
        raise RuntimeError("map update failed")

    session.sensor_listeners.extend((failing, lambda frame: seen.append(frame.t)))
    clock.advance(1)
    with caplog.at_level(logging.ERROR, logger="relay.session"):
        events = session.process_frame(
            sensor_payload(event_id="scan-1", timestamp=clock()), ground_principal
        )

    assert events[0]["event_id"] == "scan-1"
    assert seen == [clock()]
    assert "sensor listener failed" in caplog.text
    assert session.latest_sensor(GROUND_ID).event_id == "scan-1"


def test_runtime_routes_sensor_events_to_console_principals_only(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: DeviceClass.GROUND_VEHICLE},
        localization_keys={1: ADAPTER_KEY + b"-localization"},
        log_dir=tmp_path,
    )

    async def exercise() -> dict[str, list[str]]:
        runtime = RelayRuntime(settings, clock=clock, event_ids=event_ids)
        session = runtime.session(SESSION)
        ground = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
        principals = {
            "ground": ground,
            "aircraft": Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY),
            "console": Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY),
            "keyboard": Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY),
            "localization": Principal(
                source="localization", drone_id=1, signing_key=ADAPTER_KEY + b"-localization"
            ),
        }
        subscriptions = {
            name: await runtime.subscribe(SESSION, principal)
            for name, principal in principals.items()
        }
        _join(session, ground)
        clock.advance(1)
        events = session.process_frame(sensor_payload(event_id="scan-1", timestamp=clock()), ground)
        await runtime.publish(SESSION, events)
        received: dict[str, list[str]] = {}
        for name, subscription in subscriptions.items():
            types: list[str] = []
            while not subscription.queue.empty():
                types.append(str(subscription.queue.get_nowait().event["type"]))
            received[name] = types
        return received

    received = asyncio.run(exercise())

    assert received["console"] == ["sensor"]
    assert received["keyboard"] == ["sensor"]
    assert received["ground"] == []
    assert received["aircraft"] == []
    assert received["localization"] == []
