import pytest
from dataclasses import replace

from arbiter.safety import SafetyArbiter
from planner.controller import PreparedExecutionRouter
from planner.models import FlightState, Plan
from planner.planner import DeterministicPlanner
from relay.audit import SessionAuditLog
from relay.capabilities import (
    C1_CAPABILITY_PROFILE,
    C1_BASIC_CONTROL_INTENT_NAMES,
    CapabilityProfile,
    IntentName,
)
from relay.intent_v1 import AcceptedIntent, RejectedIntent, validate_intent
from relay.session import RelayLimits, RelaySession
from relay.tests.conftest import intent_payload
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    make_stack,
    planning_config,
    safety_config,
)


def test_c1_profile_enables_only_earned_intents() -> None:
    assert C1_CAPABILITY_PROFILE.enabled_intent_names == C1_BASIC_CONTROL_INTENT_NAMES
    assert {name.value for name in C1_CAPABILITY_PROFILE.enabled_intent_names} == {
        "arm",
        "capture_room",
        "come_home",
        "estop",
        "hold",
        "land",
        "land_all",
        "select",
        "takeoff",
        "translate",
    }
    assert not C1_CAPABILITY_PROFILE.supports(IntentName.ALTITUDE)
    assert not C1_CAPABILITY_PROFILE.supports(IntentName.DISARM)


def test_altitude_profile_tracks_disabled_relative_and_absolute_configurations(tmp_path) -> None:
    disabled = DeterministicPlanner(planning_config())
    relative = DeterministicPlanner(
        replace(
            planning_config(),
            altitude_step_m=0.25,
            altitude_configuration_id="altitude-relative-v1",
        )
    )
    absolute = DeterministicPlanner(
        replace(
            planning_config(),
            altitude_step_m=0.25,
            altitude_floor_z_m=0.0,
            altitude_configuration_id="altitude-absolute-v1",
        )
    )
    limits = RelayLimits(5_000, 5_000, 1_000, 1_000)

    disabled_session = RelaySession(
        session_id="disabled-altitude",
        audit_log=SessionAuditLog(tmp_path, "disabled-altitude"),
        limits=limits,
        capability_profile=disabled.capability_profile,
    )
    relative_session = RelaySession(
        session_id="relative-altitude",
        audit_log=SessionAuditLog(tmp_path, "relative-altitude"),
        limits=limits,
        capability_profile=relative.capability_profile,
    )
    absolute_session = RelaySession(
        session_id="absolute-altitude",
        audit_log=SessionAuditLog(tmp_path, "absolute-altitude"),
        limits=limits,
        capability_profile=absolute.capability_profile,
    )
    relative_raw = intent_payload()
    relative_raw.update(name="altitude", args={"delta": 1})
    absolute_raw = intent_payload()
    absolute_raw.update(name="altitude", args={"height_m": 1})

    assert "altitude" not in disabled_session.current_state()["enabled_intent_names"]
    assert "altitude" in relative_session.current_state()["enabled_intent_names"]
    assert "altitude" in absolute_session.current_state()["enabled_intent_names"]
    assert isinstance(validate_intent(relative_raw, capability_profile=relative.capability_profile), AcceptedIntent)
    assert isinstance(validate_intent(absolute_raw, capability_profile=relative.capability_profile), RejectedIntent)
    assert isinstance(validate_intent(absolute_raw, capability_profile=absolute.capability_profile), AcceptedIntent)


def test_profile_rejects_unimplemented_intents() -> None:
    with pytest.raises(ValueError, match="unimplemented intents: sweep"):
        CapabilityProfile("unsafe", frozenset({IntentName.SWEEP}))


def test_relay_and_router_must_use_the_same_profile(tmp_path) -> None:
    profile = CapabilityProfile("land-only", frozenset({IntentName.LAND}))
    snapshot = make_snapshot(1, selection=(1,))
    router = PreparedExecutionRouter(
        make_stack(snapshot, config=planning_config())[0], current_snapshot=lambda: snapshot
    )
    router.controller.planner = DeterministicPlanner(planning_config(), profile)
    limits = RelayLimits(5_000, 5_000, 1_000, 1_000)

    with pytest.raises(ValueError, match="different capability profiles"):
        RelaySession(
            session_id="profile-test",
            audit_log=SessionAuditLog(tmp_path, "profile-test"),
            limits=limits,
            intent_sink=router,
        )

    session = RelaySession(
        session_id="profile-test",
        audit_log=SessionAuditLog(tmp_path, "profile-test"),
        limits=limits,
        intent_sink=router,
        capability_profile=profile,
    )

    assert session.current_state()["capability_profile"] == "land-only"


def test_every_advertised_intent_has_a_safe_planner_and_arbiter_path() -> None:
    planner = DeterministicPlanner(planning_config(), C1_CAPABILITY_PROFILE)
    arbiter = SafetyArbiter(safety_config())
    cases = (
        (
            make_intent(IntentName.ARM, selection=()),
            make_snapshot(1, selection=(), flight_state=FlightState.DISARMED, armed=False),
        ),
        (
            make_intent(IntentName.SELECT, selection=(), args={"ids": (1,)}),
            make_snapshot(1, selection=()),
        ),
        (
            make_intent(IntentName.TAKEOFF, selection=(1,), confirm=True),
            make_snapshot(1, selection=(1,), flight_state=FlightState.LANDED),
        ),
        (
            make_intent(IntentName.TRANSLATE, selection=(1,), args={"dx": 1, "dy": 0}),
            make_snapshot(1, selection=(1,)),
        ),
        (make_intent(IntentName.HOLD, selection=(1,)), make_snapshot(1, selection=(1,))),
        (make_intent(IntentName.COME_HOME, selection=(1,)), make_snapshot(1, selection=(1,))),
        (
            make_intent(IntentName.LAND, selection=(1,), confirm=True),
            make_snapshot(1, selection=(1,)),
        ),
        (
            make_intent(IntentName.LAND_ALL, selection=(), confirm=True),
            make_snapshot(1, selection=(1,)),
        ),
        (make_intent(IntentName.ESTOP, selection=()), make_snapshot(1, selection=(1,))),
        (
            make_intent(
                IntentName.CAPTURE_ROOM,
                selection=(1,),
                confirm=True,
                args={
                    "room_id": "room-1",
                    "capture_id": "capture-1",
                    "pattern": "pano_360",
                },
            ),
            make_snapshot(1, selection=(1,)),
        ),
    )

    assert {intent.name for intent, _ in cases} == C1_CAPABILITY_PROFILE.enabled_intent_names
    for intent, snapshot in cases:
        assert arbiter.check_intent(intent, snapshot) is None
        plan = planner.plan(intent, snapshot)
        assert isinstance(plan, Plan)
        assert arbiter.check_plan(plan, snapshot) is None


def test_validation_and_planning_share_the_injected_profile() -> None:
    profile = CapabilityProfile("land-only", frozenset({IntentName.LAND}))
    raw = intent_payload()
    raw.update(name="land", confirm=True)

    accepted = validate_intent(raw, capability_profile=profile)
    refused = validate_intent(intent_payload(), capability_profile=profile)
    planned = DeterministicPlanner(planning_config(), profile).plan(
        make_intent(IntentName.LAND, selection=(1,), confirm=True), make_snapshot(1)
    )
    unsupported = DeterministicPlanner(planning_config(), profile).plan(
        make_intent(IntentName.HOLD), make_snapshot(1)
    )

    assert isinstance(accepted, AcceptedIntent)
    assert isinstance(refused, RejectedIntent)
    assert isinstance(planned, Plan)
    assert unsupported.reason.value == "unsupported"
