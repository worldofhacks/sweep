from dataclasses import replace

import pytest

from arbiter.safety import SafetyArbiter
from planner.controller import PreparedExecutionRouter
from planner.models import FlightState, Plan
from planner.planner import DeterministicPlanner
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.capabilities import (
    C1_CAPABILITY_PROFILE,
    C1_IMPLEMENTED_INTENT_NAMES,
    C2_ADDITIONAL_INTENT_NAMES,
    C2_CAPABILITY_PROFILE,
    IMPLEMENTED_INTENT_NAMES,
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
    assert C1_CAPABILITY_PROFILE.enabled_intent_names == C1_IMPLEMENTED_INTENT_NAMES
    assert {name.value for name in C1_CAPABILITY_PROFILE.enabled_intent_names} == {
        "arm",
        "altitude",
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


def test_c2_profile_is_a_strict_c1_superset() -> None:
    assert C2_CAPABILITY_PROFILE.enabled_intent_names == IMPLEMENTED_INTENT_NAMES
    assert C2_CAPABILITY_PROFILE.enabled_intent_names == (
        C1_CAPABILITY_PROFILE.enabled_intent_names | C2_ADDITIONAL_INTENT_NAMES
    )
    assert C1_CAPABILITY_PROFILE.enabled_intent_names < C2_CAPABILITY_PROFILE.enabled_intent_names


def test_profile_rejects_unimplemented_intents() -> None:
    with pytest.raises(ValueError, match="unimplemented intents: survey_area"):
        CapabilityProfile("unsafe", frozenset({IntentName.SURVEY_AREA}))

    with pytest.raises(ValueError, match="must not be empty"):
        CapabilityProfile("empty", frozenset())


def test_deployment_grounding_derives_altitude_capability_without_widening() -> None:
    disabled = replace(
        planning_config(),
        altitude_step_m=None,
        altitude_floor_z_m=None,
        altitude_configuration_id=None,
        altitude_completion_tolerance_m=None,
    )
    without_altitude = CapabilityProfile(
        "c1_without_altitude",
        C1_CAPABILITY_PROFILE.enabled_intent_names - {IntentName.ALTITUDE},
    )

    disabled_planner = DeterministicPlanner(disabled, C1_CAPABILITY_PROFILE)
    grounded = replace(
        planning_config(),
        altitude_step_m=0.5,
        altitude_floor_z_m=0.0,
        altitude_configuration_id="capability-test-floor-v1",
        altitude_completion_tolerance_m=0.05,
    )
    grounded_planner = DeterministicPlanner(grounded, C1_CAPABILITY_PROFILE)
    narrowed_planner = DeterministicPlanner(grounded, without_altitude)

    altitude = make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1})
    assert disabled_planner.capability_profile.name == "c1_basic_control.no_altitude"
    assert disabled_planner.capability_profile.enabled_intent_names == (
        C1_CAPABILITY_PROFILE.enabled_intent_names - {IntentName.ALTITUDE}
    )
    assert not disabled_planner.supports(altitude)
    assert grounded_planner.capability_profile is C1_CAPABILITY_PROFILE
    assert narrowed_planner.capability_profile is without_altitude
    assert not narrowed_planner.supports(altitude)


def test_profile_normalizes_caller_owned_sets_and_string_members() -> None:
    caller_owned = {IntentName.LAND}
    profile = CapabilityProfile("land-only", caller_owned)  # type: ignore[arg-type]
    caller_owned.add(IntentName.SURVEY_AREA)

    assert profile.enabled_intent_names == frozenset({IntentName.LAND})
    assert isinstance(profile.enabled_intent_names, frozenset)
    assert profile.state_value()["enabled_intent_names"] == ["land"]

    from_string = CapabilityProfile("land-string", frozenset({"land"}))  # type: ignore[arg-type]
    assert from_string.enabled_intent_names == frozenset({IntentName.LAND})
    assert from_string.state_value()["enabled_intent_names"] == ["land"]


@pytest.mark.parametrize("name", ["survey_area", "not_registered"])
def test_profile_rejects_unsupported_string_members_as_value_errors(name: str) -> None:
    with pytest.raises(ValueError):
        CapabilityProfile("unsafe", frozenset({name}))  # type: ignore[arg-type]


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

    with pytest.raises(ValueError, match="different capability profiles"):
        RelaySession(
            session_id="profile-test",
            audit_log=SessionAuditLog(tmp_path, "profile-test"),
            limits=limits,
            intent_sink=router.__call__,
        )

    session = RelaySession(
        session_id="profile-test",
        audit_log=SessionAuditLog(tmp_path, "profile-test"),
        limits=limits,
        intent_sink=router,
        capability_profile=profile,
    )

    assert session.current_state()["capability_profile"] == "land-only"

    router.controller.planner = DeterministicPlanner(planning_config(), C1_CAPABILITY_PROFILE)
    refusal = session.process_intent(
        {**intent_payload(), "name": "land", "confirm": True},
        Principal(source="console", drone_id=None, signing_key=b"x" * 32),
    )
    assert refusal[0]["reason"] == "capability_profile_mismatch"


def test_opaque_sink_requires_an_explicit_capability_contract(tmp_path) -> None:
    with pytest.raises(ValueError, match="must declare an immutable capability profile"):
        RelaySession(
            session_id="profile-test",
            audit_log=SessionAuditLog(tmp_path, "profile-test"),
            limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
            intent_sink=lambda _intent, _state: None,
        )


def _safe_profile_cases() -> tuple[tuple[object, object], ...]:
    return (
        (
            make_intent(IntentName.ARM, selection=()),
            make_snapshot(1, selection=(), flight_state=FlightState.DISARMED, armed=False),
        ),
        (
            make_intent(IntentName.DISARM, selection=()),
            make_snapshot(1, selection=(), flight_state=FlightState.LANDED, armed=True),
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
        (
            make_intent(IntentName.ALTITUDE, selection=(1,), args={"delta": 1}),
            make_snapshot(1, selection=(1,)),
        ),
        (
            make_intent(IntentName.FORMATION_NEXT, selection=(1, 2)),
            make_snapshot(2, selection=(1, 2), spacing=1.0),
        ),
        (
            make_intent(IntentName.FORMATION_SET, selection=(1, 2), args={"name": "line"}),
            make_snapshot(2, selection=(1, 2), spacing=1.0),
        ),
        (
            make_intent(IntentName.SPACING, selection=(1, 2), args={"delta": 1}),
            make_snapshot(2, selection=(1, 2), spacing=1.0, formation="line"),
        ),
        (
            make_intent(IntentName.SWEEP, selection=(1, 2), args={}, confirm=True),
            make_snapshot(2, selection=(1, 2)),
        ),
    )


def _grounded_planner(profile: CapabilityProfile) -> DeterministicPlanner:
    return DeterministicPlanner(
        replace(
            planning_config(),
            altitude_step_m=0.5,
            altitude_floor_z_m=0.0,
            altitude_configuration_id="capability-test-floor-v1",
            altitude_completion_tolerance_m=0.05,
        ),
        profile,
    )


def test_every_c2_intent_has_a_safe_planner_and_arbiter_path() -> None:
    planner = _grounded_planner(C2_CAPABILITY_PROFILE)
    arbiter = SafetyArbiter(safety_config())
    cases = _safe_profile_cases()

    assert {intent.name for intent, _ in cases} == C2_CAPABILITY_PROFILE.enabled_intent_names
    for intent, snapshot in cases:
        assert arbiter.check_intent(intent, snapshot) is None
        plan = planner.plan(intent, snapshot)
        assert isinstance(plan, Plan)
        assert arbiter.check_plan(plan, snapshot) is None


def test_c2_preserves_c1_plan_shapes_and_safety_checks() -> None:
    c1 = _grounded_planner(C1_CAPABILITY_PROFILE)
    c2 = _grounded_planner(C2_CAPABILITY_PROFILE)
    arbiter = SafetyArbiter(safety_config())

    for intent, snapshot in _safe_profile_cases():
        if intent.name not in C1_CAPABILITY_PROFILE.enabled_intent_names:
            continue
        c1_plan = c1.plan(intent, snapshot)
        c2_plan = c2.plan(intent, snapshot)
        assert isinstance(c1_plan, Plan)
        assert c2_plan == c1_plan
        assert arbiter.check_intent(intent, snapshot) is None
        assert arbiter.check_plan(c1_plan, snapshot) is None


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
