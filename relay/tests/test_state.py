from __future__ import annotations

import pytest

from relay.contracts import Membership, parse_membership_request, parse_telemetry
from relay.state import FleetRegistry, RegistryError
from relay.tests.conftest import SESSION, membership_payload, telemetry_payload


def _join(registry: FleetRegistry, drone_id: int, event_id: str) -> None:
    request = parse_membership_request(
        membership_payload(action="join", event_id=event_id, drone_id=drone_id)
    )
    registry.apply_join(request)


def _ready(registry: FleetRegistry, event_id: str, timestamp: int) -> None:
    request = parse_membership_request(
        membership_payload(action="readiness", event_id=event_id, timestamp=timestamp)
    )
    registry.apply_readiness(request)


def test_registry_accepts_four_stable_ids_and_rejects_a_fifth() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)

    for drone_id in range(1, 5):
        _join(registry, drone_id, f"join-{drone_id}")

    assert registry.roster_version == 4
    with pytest.raises(RegistryError) as error:
        _join(registry, 5, "join-5")
    assert error.value.code == "fleet_capacity"


def test_four_aircraft_can_disconnect_and_rejoin_in_one_session() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    for drone_id in range(1, 5):
        _join(registry, drone_id, f"join-{drone_id}")
    for drone_id in range(1, 5):
        registry.disconnect(
            drone_id=drone_id,
            connection_epoch=1,
            t=1_756_700_000_001,
            event_id=f"loss-{drone_id}",
        )
    for drone_id in range(1, 5):
        _join(registry, drone_id, f"rejoin-{drone_id}")

    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_002,
        event_id="state-rejoined",
    )

    assert registry.roster_version == 12
    assert [drone["connection_epoch"] for drone in state["drones"]] == [2, 2, 2, 2]
    assert all(drone["membership"] == "registered" for drone in state["drones"])


def test_active_connection_identity_is_atomic_and_excludes_inactive_memberships() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    assert registry.active_connection_identity(1) is None

    _join(registry, 1, "join-1")
    assert registry.active_connection_identity(1) == (1, 1)

    registry.disconnect(
        drone_id=1,
        connection_epoch=1,
        t=1_756_700_000_001,
        event_id="loss-1",
    )
    assert registry.active_connection_identity(1) is None

    _join(registry, 1, "join-2")
    assert registry.active_connection_identity(1) == (2, 3)


def test_all_readiness_gates_must_pass_before_aircraft_is_selectable() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")

    transition = registry.apply_readiness(
        parse_membership_request(membership_payload(action="readiness", event_id="ready-early"))
    )

    assert transition.membership is Membership.DEGRADED
    assert "telemetry_missing" in transition.readiness_reasons
    assert "home_pose_missing" in transition.readiness_reasons

    telemetry = parse_telemetry(telemetry_payload(event_id="telemetry-1"))
    registry.apply_telemetry(telemetry, transition_event_id="recovered-1")
    transition = registry.apply_readiness(
        parse_membership_request(membership_payload(action="readiness", event_id="ready-1"))
    )

    state = registry.state_event(session=SESSION, t=telemetry.t, event_id="state-1")
    assert transition.membership is Membership.READY
    assert state["drones"][0]["selectable"] is True
    assert state["drones"][0]["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.5}
    assert state["capability_profile"] == "c1_basic_control"
    assert state["enabled_intent_names"] == [
        "altitude",
        "arm",
        "capture_room",
        "come_home",
        "estop",
        "formation_next",
        "formation_set",
        "hold",
        "land",
        "land_all",
        "select",
        "spacing",
        "sweep",
        "takeoff",
        "translate",
    ]


def test_readiness_reports_each_failed_declared_gate() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-1")),
        transition_event_id="unused",
    )
    raw = membership_payload(action="readiness", event_id="ready-1")
    raw["home_pose_confirmed"] = False
    raw["control_authority"] = False
    raw["rc_safety_operator_present"] = False
    from relay.auth import sign_event
    from relay.tests.conftest import ADAPTER_KEY

    raw["signature"] = sign_event(
        {key: value for key, value in raw.items() if key != "signature"}, ADAPTER_KEY
    )

    transition = registry.apply_readiness(parse_membership_request(raw))

    assert transition.readiness_reasons == (
        "home_pose_missing",
        "control_authority_missing",
        "rc_safety_operator_missing",
    )


def test_non_flight_capabilities_cannot_make_aircraft_selectable() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    join = membership_payload(action="join", event_id="join-1")
    join["capabilities"] = ["pano_360", "unknown"]
    from relay.auth import sign_event
    from relay.tests.conftest import ADAPTER_KEY

    join["signature"] = sign_event(
        {key: value for key, value in join.items() if key != "signature"}, ADAPTER_KEY
    )
    registry.apply_join(parse_membership_request(join))
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-1")),
        transition_event_id="unused",
    )

    transition = registry.apply_readiness(
        parse_membership_request(membership_payload(action="readiness", event_id="readiness-1"))
    )

    assert transition.membership is Membership.DEGRADED
    assert "flight_capability_missing" in transition.readiness_reasons


def test_stale_telemetry_degrades_and_new_current_frame_recovers() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-1")),
        transition_event_id="unused",
    )
    _ready(registry, "ready-1", 1_756_700_000_000)

    transitions = registry.expire_stale_telemetry(
        now_ms=1_756_700_001_001,
        event_ids=["stale-1"],
    )
    recovered = registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-2", timestamp=1_756_700_001_001)),
        transition_event_id="recovered-1",
    )

    assert transitions[0].membership is Membership.DEGRADED
    assert recovered is not None
    assert recovered.membership is Membership.READY


def test_regressive_telemetry_cannot_replace_canonical_state() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-new", timestamp=1_756_700_000_100)),
        transition_event_id="unused",
    )

    with pytest.raises(RegistryError) as error:
        registry.apply_telemetry(
            parse_telemetry(
                telemetry_payload(event_id="telemetry-old", timestamp=1_756_700_000_099)
            ),
            transition_event_id="unused-2",
        )

    assert error.value.code == "out_of_order_telemetry"


def test_disconnect_history_and_rejoin_epoch_are_preserved_without_mutating_work() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.set_selection((1,))
    registry.set_armed(True)
    registry.set_accepted_plan({"plan_id": "plan-1", "roster_version": 1})
    disconnected = registry.disconnect(
        drone_id=1,
        connection_epoch=1,
        t=1_756_700_000_100,
        event_id="loss-1",
    )
    _join(registry, 1, "join-2")

    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_101,
        event_id="state-1",
    )
    drone = state["drones"][0]
    assert disconnected is not None
    assert disconnected.membership is Membership.DISCONNECTED
    assert drone["connection_epoch"] == 2
    assert [entry["membership"] for entry in drone["membership_history"]] == [
        "registered",
        "disconnected",
        "registered",
    ]
    assert state["selection"] == [1]
    assert state["armed"] is True
    assert state["accepted_plan"] == {"plan_id": "plan-1", "roster_version": 1}


def test_airborne_readiness_changes_cannot_replace_the_confirmed_home_pose() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-home", state="landed")),
        transition_event_id="unused-home",
    )
    _ready(registry, "ready-1", 1_756_700_000_000)
    airborne = telemetry_payload(
        event_id="telemetry-airborne",
        timestamp=1_756_700_000_101,
        state="hovering",
    )
    airborne.update(x=9.0, y=8.0, z=1.5)
    registry.apply_telemetry(
        parse_telemetry(airborne),
        transition_event_id="unused-airborne",
    )
    unconfirmed = membership_payload(
        action="readiness",
        event_id="ready-unconfirmed",
        timestamp=1_756_700_000_101,
        home_pose_confirmed=False,
    )
    transition = registry.apply_readiness(parse_membership_request(unconfirmed))
    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_101,
        event_id="state-unconfirmed",
    )
    assert transition.membership is Membership.DEGRADED
    assert "home_pose_missing" in transition.readiness_reasons
    assert state["drones"][0]["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.5}

    registry.apply_readiness(
        parse_membership_request(
            membership_payload(
                action="readiness",
                event_id="ready-2",
                timestamp=1_756_700_000_101,
            )
        )
    )

    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_101,
        event_id="state-reconfirmed",
    )

    assert state["drones"][0]["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.5}


def test_airborne_rejoin_cannot_replace_the_confirmed_home_pose() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-home")),
        transition_event_id="unused-home",
    )
    _ready(registry, "ready-1", 1_756_700_000_000)
    registry.disconnect(
        drone_id=1,
        connection_epoch=1,
        t=1_756_700_000_100,
        event_id="loss-1",
    )
    _join(registry, 1, "join-2")
    airborne = telemetry_payload(
        event_id="telemetry-airborne",
        timestamp=1_756_700_000_101,
        connection_epoch=2,
        state="hovering",
    )
    airborne.update(x=9.0, y=8.0, z=1.5)
    registry.apply_telemetry(
        parse_telemetry(airborne),
        transition_event_id="unused-airborne",
    )
    registry.apply_readiness(
        parse_membership_request(
            membership_payload(
                action="readiness",
                event_id="ready-2",
                timestamp=1_756_700_000_101,
                connection_epoch=2,
            )
        )
    )

    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_101,
        event_id="state-2",
    )

    assert state["drones"][0]["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.5}


def test_graceful_leave_has_observable_leaving_then_disconnected_states() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    leaving = registry.apply_graceful_leave(
        parse_membership_request(membership_payload(action="graceful_leave", event_id="leave-1"))
    )
    disconnected = registry.disconnect(
        drone_id=1,
        connection_epoch=1,
        t=1_756_700_000_001,
        event_id="leave-complete-1",
    )

    assert leaving.membership is Membership.LEAVING
    assert disconnected is not None
    assert disconnected.action.value == "graceful_leave_completed"
    assert disconnected.membership is Membership.DISCONNECTED


def test_state_v1_console_projection_has_frozen_compatibility_keys() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry, 1, "join-1")
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry-1")),
        transition_event_id="unused",
    )
    _ready(registry, "ready-1", 1_756_700_000_000)

    state = registry.state_event(
        session=SESSION,
        t=1_756_700_000_000,
        event_id="state-compatibility",
    )
    drone = state["drones"][0]

    assert set(state) == {
        "v",
        "t",
        "type",
        "event_id",
        "session",
        "roster_version",
        "state_sequence",
        "armed",
        "estop",
        "selection",
        "formation",
        "spacing",
        "mode",
        "capability_profile",
        "enabled_intent_names",
        "pending",
        "accepted_plan",
        "drones",
    }
    assert set(drone) == {
        "drone_id",
        "connection_epoch",
        "membership",
        "readiness_reasons",
        "flight_state",
        "battery",
        "link",
        "pos_quality",
        "control_authority",
        "last_seen_at",
        "camera_patterns",
        "selectable",
        "adapter_id",
        "adapter_capabilities",
        "home_pose",
        "rc_safety_operator_present",
        "telemetry",
        "membership_history",
        "camera_capabilities",
        "node_status",
        "video",
    }
    assert drone["camera_capabilities"] is None
    assert drone["node_status"] is None
    # The console contract accepts exactly these two keys (contract.ts isVideoStreamState).
    assert drone["video"] == {"status": "unreported", "last_frame_at": None}
    assert drone["flight_state"] == drone["telemetry"]["state"]
    assert drone["battery"] == drone["telemetry"]["battery"]
    assert drone["camera_patterns"] == ["pano_360"]


def test_state_sequence_orders_equal_time_control_snapshots() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    first = registry.state_event(session=SESSION, t=100, event_id="older")
    registry.set_armed(True)
    second = registry.state_event(session=SESSION, t=100, event_id="newer")
    third = registry.state_event(session=SESSION, t=99, event_id="clock-backward")
    assert [event["state_sequence"] for event in (first, second, third)] == [1, 2, 3]
    assert first["armed"] is False
    assert second["armed"] is True
