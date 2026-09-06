"""Device classes: configuration, the join claim, per-class readiness, units, and capacity."""

from __future__ import annotations

from pathlib import Path

import pytest

from language.contracts import build_grounding_facts
from planner.models import DeviceClass, DriveState, FlightState
from relay.app import RelayRuntime
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal, sign_event
from relay.autonomy import relay_snapshot
from relay.contracts import (
    ContractError,
    DeviceIdentity,
    Membership,
    declared_device_class,
    parse_membership_request,
    parse_telemetry,
)
from relay.session import RelayLimits, RelaySession, _material_state_projection
from relay.settings import RelaySettings, SettingsError
from relay.state import MAX_PHYSICAL_DEVICES, FleetRegistry, RegistryError
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    GROUND_ID,
    GROUND_KEY,
    SESSION,
    EventIds,
    MutableClock,
    ground_membership_payload,
    ground_telemetry_payload,
    membership_payload,
    profiled_sink,
    telemetry_payload,
)

T0 = 1_756_700_000_000
GROUND = DeviceClass.GROUND_VEHICLE
AIRCRAFT = DeviceClass.AIRCRAFT


def _ground_registry(*ground_ids: int) -> FleetRegistry:
    devices = {
        drone_id: DeviceIdentity(GROUND, unit)
        for unit, drone_id in enumerate(sorted(ground_ids or (GROUND_ID,)), start=1)
    }
    return FleetRegistry(telemetry_freshness_ms=1_000, devices=devices)


def _resign(payload: dict[str, object], key: bytes) -> dict[str, object]:
    unsigned = {key_: value for key_, value in payload.items() if key_ != "signature"}
    return {**unsigned, "signature": sign_event(unsigned, key)}


def _ground_join(registry: FleetRegistry, event_id: str = "join-ground", **overrides: object):
    request = parse_membership_request(
        ground_membership_payload(action="join", event_id=event_id, **overrides)
    )
    return registry.apply_join(request)


def test_settings_read_device_classes_and_number_units_per_class() -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": (
                '{"3":"key-three-that-is-at-least-32-bytes-long",'
                '"7":"key-seven-that-is-at-least-32-bytes-long",'
                '"13":"key-thirteen-that-is-at-least-32-bytes-l",'
                '"11":"key-eleven-that-is-at-least-32-bytes-lon"}'
            ),
            "SWEEP_DEVICE_CLASSES_JSON": '{"11":"ground_vehicle","13":"ground_vehicle"}',
        }
    )

    assert settings.device_classes == {11: GROUND, 13: GROUND}
    # Units count within a class, sorted by id, independent of key order and id gaps.
    assert dict(settings.device_identities()) == {
        3: DeviceIdentity(AIRCRAFT, 1),
        7: DeviceIdentity(AIRCRAFT, 2),
        11: DeviceIdentity(GROUND, 1),
        13: DeviceIdentity(GROUND, 2),
    }
    assert settings.media_devices() == settings.device_identities()
    with pytest.raises(TypeError):
        settings.device_classes[1] = GROUND  # type: ignore[index]


def test_settings_default_to_aircraft_and_the_four_default_media_paths() -> None:
    settings = RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode()})

    assert settings.device_classes == {}
    assert settings.device_identities() == {}
    assert {drone_id: identity.unit for drone_id, identity in settings.media_devices().items()} == {
        1: 1,
        2: 2,
        3: 3,
        4: 4,
    }
    assert all(identity.device_class is AIRCRAFT for identity in settings.media_devices().values())


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ('{"11":"ground_vehicle"}', "device_class_without_key"),
        ('{"1":"submarine"}', "must be one of"),
        ('{"01":"aircraft"}', "canonical positive"),
        ('{"0":"aircraft"}', "canonical positive"),
        ("[]", "must be an object"),
        ("{", "valid JSON"),
    ],
)
def test_settings_reject_unkeyed_ids_and_unknown_classes(raw: str, match: str) -> None:
    with pytest.raises(SettingsError, match=match):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
                "SWEEP_DEVICE_CLASSES_JSON": raw,
            }
        )


def test_shared_token_requires_aircraft_ids_that_match_their_units() -> None:
    """An ID admitted without a key takes its ID as its unit, so it must not be able to
    take a configured aircraft's unit, D-NN label, and drone{unit} media path."""
    environment = {
        "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
        "SWEEP_ALLOW_SHARED_ADAPTER_TOKEN": "true",
        "SWEEP_ADAPTER_KEYS_JSON": (
            '{"5":"key-five-that-is-at-least-32-bytes-long",'
            '"7":"key-seven-that-is-at-least-32-bytes-long"}'
        ),
    }

    with pytest.raises(SettingsError, match="shared_token_unit_collision"):
        RelaySettings.from_env(environment)

    settings = RelaySettings.from_env(
        {
            **environment,
            "SWEEP_ADAPTER_KEYS_JSON": (
                '{"1":"key-one-that-is-at-least-32-bytes-longg",'
                '"2":"key-two-that-is-at-least-32-bytes-longg",'
                '"11":"key-eleven-that-is-at-least-32-bytes-lon"}'
            ),
            "SWEEP_DEVICE_CLASSES_JSON": '{"11":"ground_vehicle"}',
        }
    )

    # Contiguous aircraft IDs make unit and ID the same number, so the fallback unit of an
    # unkeyed joiner (3) lands above every configured aircraft unit instead of on one.
    registry = FleetRegistry(telemetry_freshness_ms=1_000, devices=settings.device_identities())
    assert registry.device_identity(1) == DeviceIdentity(AIRCRAFT, 1)
    assert registry.device_identity(11) == DeviceIdentity(GROUND, 1)
    assert registry.device_identity(3) == DeviceIdentity(AIRCRAFT, 3)


def test_aircraft_ids_that_are_not_their_units_warn_about_the_publishers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pilot app and the console still derive drone{id}; the relay polls drone{unit}."""
    with caplog.at_level("WARNING", logger="relay.settings"):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_ADAPTER_KEYS_JSON": (
                    '{"2":"key-two-that-is-at-least-32-bytes-longg",'
                    '"3":"key-three-that-is-at-least-32-bytes-long"}'
                ),
            }
        )

    assert "configured aircraft IDs 2, 3 do not equal their units" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="relay.settings"):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
            }
        )

    assert caplog.text == ""


def test_join_declares_its_class_through_one_capability_entry() -> None:
    assert declared_device_class(("flight", "pano_360")) is None
    assert declared_device_class(("class:aircraft", "flight")) is AIRCRAFT
    assert declared_device_class(("ground_drive", "class:ground_vehicle")) is GROUND
    with pytest.raises(ContractError) as duplicated:
        declared_device_class(("class:aircraft", "class:ground_vehicle"))
    assert duplicated.value.code == "device_class_mismatch"
    with pytest.raises(ContractError) as unknown:
        parse_membership_request(
            ground_membership_payload(
                action="join", event_id="join-x", capabilities=["class:submarine", "ground_drive"]
            )
        )
    assert unknown.value.code == "device_class_mismatch"

    request = parse_membership_request(ground_membership_payload(action="join", event_id="join"))
    assert request.device_class is GROUND
    # The class rides inside the signed capability list; the frame gains no key.
    assert "device_class" not in request.unsigned_event()
    assert "class:ground_vehicle" in request.capabilities


def test_join_class_must_agree_with_configuration() -> None:
    registry = _ground_registry(GROUND_ID)

    transition = _ground_join(registry)
    assert transition.membership is Membership.REGISTERED
    assert registry.device_identity(GROUND_ID) == DeviceIdentity(GROUND, 1)

    # A configured ground vehicle joining without its class is refused.
    silent = ground_membership_payload(
        action="join", event_id="join-silent", drone_id=12, capabilities=["ground_drive"]
    )
    registry_two = _ground_registry(12)
    with pytest.raises(RegistryError) as refused:
        registry_two.apply_join(parse_membership_request(silent))
    assert refused.value.code == "device_class_mismatch"

    # An unconfigured (aircraft) id claiming the ground class is refused.
    claimed = membership_payload(
        action="join",
        event_id="join-claimed",
        drone_id=2,
        capabilities=["class:ground_vehicle", "ground_drive"],
    )
    with pytest.raises(RegistryError) as mismatch:
        registry.apply_join(parse_membership_request(claimed))
    assert mismatch.value.code == "device_class_mismatch"
    assert "aircraft" in mismatch.value.detail and "ground_vehicle" in mismatch.value.detail

    # An explicit class:aircraft claim is accepted for an aircraft.
    explicit = membership_payload(
        action="join",
        event_id="join-explicit",
        drone_id=2,
        capabilities=["class:aircraft", "flight"],
    )
    assert registry.apply_join(parse_membership_request(explicit)).membership is (
        Membership.REGISTERED
    )


def test_rejoin_keeps_the_configured_class_and_unit() -> None:
    registry = _ground_registry(GROUND_ID)
    _ground_join(registry)
    registry.disconnect(drone_id=GROUND_ID, t=T0 + 1, event_id="loss")
    with pytest.raises(RegistryError) as mismatch:
        registry.apply_join(
            parse_membership_request(
                ground_membership_payload(
                    action="join", event_id="rejoin-wrong", capabilities=["flight"]
                )
            )
        )
    assert mismatch.value.code == "device_class_mismatch"

    _ground_join(registry, "rejoin")
    drone = registry.state_event(session=SESSION, t=T0 + 2, event_id="state")["drones"][0]
    assert (drone["device_class"], drone["unit"], drone["connection_epoch"]) == (
        "ground_vehicle",
        1,
        2,
    )


def test_ground_vehicle_readiness_requires_ground_drive_and_ignores_flight() -> None:
    registry = _ground_registry(GROUND_ID)
    _ground_join(registry, capabilities=["class:ground_vehicle", "lidar"])
    registry.apply_telemetry(
        parse_telemetry(ground_telemetry_payload(event_id="telemetry-1")),
        transition_event_id="unused",
    )

    degraded = registry.apply_readiness(
        parse_membership_request(ground_membership_payload(action="readiness", event_id="ready-1"))
    )
    assert degraded.membership is Membership.DEGRADED
    assert degraded.readiness_reasons == ("drive_capability_missing",)
    assert "flight_capability_missing" not in degraded.readiness_reasons

    registry.disconnect(drone_id=GROUND_ID, t=T0 + 1, event_id="loss")
    _ground_join(registry, "rejoin")
    registry.apply_telemetry(
        parse_telemetry(
            ground_telemetry_payload(event_id="telemetry-2", timestamp=T0 + 2, connection_epoch=2)
        ),
        transition_event_id="unused",
    )
    ready = registry.apply_readiness(
        parse_membership_request(
            ground_membership_payload(
                action="readiness", event_id="ready-2", timestamp=T0 + 2, connection_epoch=2
            )
        )
    )
    assert ready.membership is Membership.READY
    assert ready.readiness_reasons == ()
    drone = registry.state_event(session=SESSION, t=T0 + 2, event_id="state")["drones"][0]
    assert drone["selectable"] is True
    assert drone["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert drone["flight_state"] == "idle"


def test_aircraft_readiness_still_requires_flight() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    join = membership_payload(action="join", event_id="join-1", capabilities=["ground_drive"])
    registry.apply_join(parse_membership_request(join))
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-1")), transition_event_id="unused"
    )

    transition = registry.apply_readiness(
        parse_membership_request(membership_payload(action="readiness", event_id="ready-1"))
    )

    assert "flight_capability_missing" in transition.readiness_reasons
    assert "drive_capability_missing" not in transition.readiness_reasons


def test_ground_home_pose_clears_only_while_docked_idle_or_stopped() -> None:
    registry = _ground_registry(GROUND_ID)
    _ground_join(registry)
    registry.apply_telemetry(
        parse_telemetry(ground_telemetry_payload(event_id="telemetry-1", state="idle")),
        transition_event_id="unused",
    )
    registry.apply_readiness(
        parse_membership_request(ground_membership_payload(action="readiness", event_id="ready-1"))
    )
    assert registry.state_event(session=SESSION, t=T0, event_id="s1")["drones"][0]["home_pose"] == {
        "x": 1.0,
        "y": 2.0,
        "z": 0.0,
    }

    def unconfirm(event_id: str, timestamp: int) -> None:
        raw = ground_membership_payload(
            action="readiness", event_id=event_id, timestamp=timestamp, home_pose_confirmed=False
        )
        registry.apply_readiness(parse_membership_request(_resign(raw, GROUND_KEY)))

    registry.apply_telemetry(
        parse_telemetry(
            ground_telemetry_payload(event_id="telemetry-2", timestamp=T0 + 100, state="moving")
        ),
        transition_event_id="unused",
    )
    unconfirm("ready-moving", T0 + 100)
    assert registry.state_event(session=SESSION, t=T0 + 100, event_id="s2")["drones"][0][
        "home_pose"
    ] == {"x": 1.0, "y": 2.0, "z": 0.0}

    registry.apply_telemetry(
        parse_telemetry(
            ground_telemetry_payload(event_id="telemetry-3", timestamp=T0 + 200, state="stopped")
        ),
        transition_event_id="unused",
    )
    unconfirm("ready-stopped", T0 + 200)
    assert (
        registry.state_event(session=SESSION, t=T0 + 200, event_id="s3")["drones"][0]["home_pose"]
        is None
    )


def test_capacity_is_enforced_per_class_and_the_audit_bound_is_the_sum() -> None:
    ground_ids = (11, 12, 13, 14, 15)
    registry = _ground_registry(*ground_ids)
    for drone_id in range(1, 5):
        registry.apply_join(
            parse_membership_request(
                membership_payload(action="join", event_id=f"join-{drone_id}", drone_id=drone_id)
            )
        )
    for drone_id in ground_ids[:4]:
        _ground_join(registry, f"join-{drone_id}", drone_id=drone_id)

    with pytest.raises(RegistryError) as ground_full:
        _ground_join(registry, "join-15", drone_id=15)
    assert ground_full.value.code == "fleet_capacity"
    assert "ground_vehicle" in ground_full.value.detail
    with pytest.raises(RegistryError) as aircraft_full:
        registry.apply_join(
            parse_membership_request(
                membership_payload(action="join", event_id="join-5", drone_id=5)
            )
        )
    assert aircraft_full.value.code == "fleet_capacity"
    assert "aircraft" in aircraft_full.value.detail

    state = registry.state_event(session=SESSION, t=T0, event_id="state")
    assert len(state["drones"]) == sum(MAX_PHYSICAL_DEVICES.values()) == 8
    assert [(drone["device_class"], drone["unit"]) for drone in state["drones"]] == [
        ("aircraft", 1),
        ("aircraft", 2),
        ("aircraft", 3),
        ("aircraft", 4),
        ("ground_vehicle", 1),
        ("ground_vehicle", 2),
        ("ground_vehicle", 3),
        ("ground_vehicle", 4),
    ]
    projection = _material_state_projection(state)
    assert '"device_class":"ground_vehicle"' in projection and '"unit":4' in projection
    state["drones"].append(dict(state["drones"][0]))
    with pytest.raises(AuditLogError, match="bounded device list"):
        _material_state_projection(state)


def test_session_and_runtime_thread_the_configured_devices_into_the_registry(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: GROUND},
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(settings, clock=clock, event_ids=event_ids)
    session = runtime.session(SESSION)
    ground = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
    aircraft = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)

    events = session.process_frame(
        ground_membership_payload(action="join", event_id="join-ground", timestamp=clock()),
        ground,
    )
    assert [event["type"] for event in events] == ["membership", "state"]
    session.process_frame(
        membership_payload(action="join", event_id="join-aircraft", timestamp=clock()), aircraft
    )
    session.process_frame(
        ground_telemetry_payload(event_id="telemetry-ground", timestamp=clock(), state="moving"),
        ground,
    )
    drones = {drone["drone_id"]: drone for drone in session.current_state()["drones"]}

    assert (drones[1]["device_class"], drones[1]["unit"]) == ("aircraft", 1)
    assert (drones[GROUND_ID]["device_class"], drones[GROUND_ID]["unit"]) == ("ground_vehicle", 1)
    assert drones[GROUND_ID]["flight_state"] == "moving"
    assert drones[GROUND_ID]["telemetry"]["z"] == 0.0
    assert drones[GROUND_ID]["sensor"] == {"kind": "lidar_scan", "last_scan_at": None}

    refused = session.process_frame(
        membership_payload(
            action="join",
            event_id="join-wrong",
            timestamp=clock(),
            drone_id=GROUND_ID,
            key=GROUND_KEY,
            capabilities=["flight"],
        ),
        Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY),
    )
    assert refused[0]["type"] == "refusal"
    assert refused[0]["reason"] == "device_class_mismatch"


def test_a_mixed_session_projects_both_classes_into_one_planner_snapshot(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    """One snapshot carries the whole session: the aircraft with its flight state, the
    ground vehicle with its drive state, a null ``flight_state``, and its unit."""
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: GROUND},
        log_dir=tmp_path,
    )
    session = RelayRuntime(settings, clock=clock, event_ids=event_ids).session(SESSION)
    ground = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
    aircraft = Principal(source="adapter", drone_id=1, signing_key=ADAPTER_KEY)
    session.process_frame(
        ground_membership_payload(action="join", event_id="join-ground", timestamp=clock()), ground
    )
    session.process_frame(
        membership_payload(action="join", event_id="join-aircraft", timestamp=clock()), aircraft
    )
    session.process_frame(
        ground_telemetry_payload(event_id="telemetry-ground", timestamp=clock(), state="moving"),
        ground,
    )
    session.process_frame(
        telemetry_payload(event_id="telemetry-aircraft", timestamp=clock(), state="landed"),
        aircraft,
    )

    state = session.current_state()
    assert {drone["drone_id"] for drone in state["drones"]} == {1, GROUND_ID}
    snapshot = relay_snapshot(state, operator_last_seen_ms=None)

    assert set(snapshot.aircraft) == {1, GROUND_ID}
    robot = snapshot.aircraft[GROUND_ID]
    assert robot.device_class is DeviceClass.GROUND_VEHICLE
    assert robot.drive_state is DriveState.MOVING
    assert robot.flight_state is None
    assert robot.unit == 1
    assert robot.pose.z == 0.0
    assert robot.mobile is True and robot.airborne is False
    assert robot.armed is True, "wheels are enabled in every state but docked and fault"
    drone = snapshot.aircraft[1]
    assert drone.device_class is DeviceClass.AIRCRAFT
    assert drone.flight_state is FlightState.LANDED
    assert drone.drive_state is None
    assert drone.armed is False and drone.mobile is False

    session.process_frame(
        ground_telemetry_payload(
            event_id="telemetry-docked", timestamp=clock() + 1, state="docked"
        ),
        ground,
    )
    docked = relay_snapshot(session.current_state(), operator_last_seen_ms=None)
    assert docked.aircraft[GROUND_ID].armed is False
    assert docked.aircraft[GROUND_ID].mobile is False

    facts = build_grounding_facts(state, capability_version="device-class-v1")
    assert {int(drone["drone_id"]): drone["flight_state"] for drone in facts.drones} == {
        1: "landed",
        GROUND_ID: "moving",
    }, "the same projection grounds the language compiler, which refuses an unknown state"


def test_a_device_state_outside_its_class_vocabulary_is_excluded(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    """Each class's telemetry state is read in its own vocabulary and nothing else."""
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={1: ADAPTER_KEY, GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: GROUND},
        log_dir=tmp_path,
    )
    session = RelayRuntime(settings, clock=clock, event_ids=event_ids).session(SESSION)
    ground = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
    session.process_frame(
        ground_membership_payload(action="join", event_id="join-ground", timestamp=clock()), ground
    )
    session.process_frame(
        ground_telemetry_payload(event_id="telemetry-ground", timestamp=clock(), state="idle"),
        ground,
    )
    state = session.current_state()
    assert set(relay_snapshot(state, operator_last_seen_ms=None).aircraft) == {GROUND_ID}

    flight_state = {
        **state,
        "drones": [{**state["drones"][0], "telemetry": {**state["drones"][0]["telemetry"]}}],
    }
    flight_state["drones"][0]["telemetry"]["state"] = "hovering"
    assert relay_snapshot(flight_state, operator_last_seen_ms=None).aircraft == {}

    unknown_class = {
        **state,
        "drones": [{**state["drones"][0], "device_class": "submarine"}],
    }
    assert relay_snapshot(unknown_class, operator_last_seen_ms=None).aircraft == {}


def test_unconfigured_session_treats_every_id_as_an_aircraft_numbered_by_id(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(
            intent_max_age_ms=5_000,
            transport_event_max_age_ms=5_000,
            future_clock_skew_ms=1_000,
            telemetry_freshness_ms=1_000,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink=profiled_sink(lambda _intent, _state: None),
    )
    principal = Principal(source="adapter", drone_id=3, signing_key=ADAPTER_KEY)
    events = session.process_frame(
        membership_payload(action="join", event_id="join-3", timestamp=clock(), drone_id=3),
        principal,
    )

    drone = events[1]["drones"][0]
    assert (drone["device_class"], drone["unit"]) == ("aircraft", 3)
