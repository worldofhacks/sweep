"""Authenticated current-epoch evidence reaches only the matching safety profile."""

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from relay.auth import Principal
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    node_status_payload,
    telemetry_payload,
)
from tests.autonomy_fixtures import planning_config, safety_config


@pytest.fixture
def current_aircraft(tmp_path):
    clock = MutableClock()
    config = AutonomyConfig(planning=planning_config(), safety=safety_config())
    app, composition = create_autonomy_app(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            log_dir=tmp_path,
            adapter_backend=AdapterBackend.REMOTE,
        ),
        config,
        clock=clock,
        event_ids=EventIds(),
    )
    with TestClient(app):
        session = app.state.relay_runtime.session(SESSION)
        principal = Principal("adapter", 1, ADAPTER_KEY)
        for action in ("join", "readiness"):
            session.process_membership(
                membership_payload(action=action, event_id=action), principal
            )
        session.process_frame(telemetry_payload(event_id="telemetry"), principal)
        yield app, composition, session, principal, clock, config
    composition.close()


def observe(fixture, **changes):
    _, _, session, principal, clock, _ = fixture
    frame = node_status_payload(
        event_id="recovery-status",
        timestamp=changes.pop("timestamp", clock()),
        control_authority=False,
        virtual_stick_enabled=False,
        authority_change_reason="virtual_stick_dropped",
        watchdog_state="nominal",
    )
    frame.update(changes)
    return session.process_node_frame(frame, principal)


def evidence(fixture, state=None):
    _, composition, session, _, _, _ = fixture
    return (
        composition.session(SESSION)
        .snapshot(session.current_state() if state is None else state)
        .aircraft[1]
        .landing_recovery
    )


def test_recovery_uses_relay_receipt_not_adapter_timestamp(current_aircraft):
    *_, clock, _ = current_aircraft
    clock.advance(300)
    observe(current_aircraft, timestamp=clock() - 300)
    value = evidence(current_aircraft)
    assert value is not None
    assert value.observed_at_ms == clock()
    assert value.reason == "virtual_stick_dropped"


@pytest.mark.parametrize(
    "changes",
    [
        {"control_authority": True},
        {"virtual_stick_enabled": True},
        {"watchdog_state": "hold"},
        {"watchdog_state": "failsafe"},
        {"authority_change_reason": "not_granted"},
        {"authority_change_reason": "virtual_stick_authority_lost"},
        {"authority_change_reason": "rc_sticks_moved"},
        {"authority_change_reason": "rc_pause"},
        {"authority_change_reason": None},
        {"connection_epoch": 2},
    ],
)
def test_other_authority_facts_never_grant_recovery(current_aircraft, changes):
    observe(current_aircraft, **changes)
    assert evidence(current_aircraft) is None


@pytest.mark.parametrize("delta", [-1, 1001])
def test_future_or_stale_receipts_are_refused(current_aircraft, delta):
    observe(current_aircraft)
    _, _, session, _, clock, _ = current_aircraft
    state = session.current_state()
    state["t"] = clock() + delta
    assert evidence(current_aircraft, state) is None


def test_old_epoch_status_is_cleared_on_rejoin(current_aircraft):
    observe(current_aircraft)
    assert evidence(current_aircraft) is not None
    _, _, session, principal, clock, _ = current_aircraft
    session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    session.process_membership(
        membership_payload(action="join", event_id="rejoin", timestamp=clock()), principal
    )
    session.process_membership(
        membership_payload(
            action="readiness", event_id="ready-2", timestamp=clock(), connection_epoch=2
        ),
        principal,
    )
    session.process_frame(telemetry_payload(event_id="telemetry-2", connection_epoch=2), principal)
    assert evidence(current_aircraft) is None


def test_world_services_receive_actual_configuration(current_aircraft):
    app, _, _, _, _, config = current_aircraft
    platform = app.state.platform_services
    actual = platform.navigation.motion_config(SESSION)
    assert actual["safety"] == asdict(config.safety)


def test_ground_return_profile_requires_actual_configured_return():
    from relay.autonomy import AutonomyComposition, _ground_return_selected
    from relay.contracts import NodeType
    from relay.intent_v1 import IntentName
    from relay.tests.test_supervised_vertical import _vertical_config
    from tests.autonomy_fixtures import make_intent, make_snapshot

    config = AutonomyConfig(supervised_vertical=_vertical_config())
    disabled = AutonomyComposition(config, node_types={9: NodeType.GROUND})
    enabled = AutonomyComposition(
        config, node_types={9: NodeType.GROUND}, ground_return_id="approved"
    )
    assert not disabled.capability_profile.supports(IntentName.COME_HOME)
    assert enabled.capability_profile.supports(IntentName.COME_HOME)
    assert not enabled.capability_profile.requires_home_pose
    state = {
        "drones": [{"drone_id": 1, "node_type": "aircraft"}, {"drone_id": 9, "node_type": "ground"}]
    }
    assert _ground_return_selected(make_intent(IntentName.COME_HOME, selection=(9,)), state)
    assert not _ground_return_selected(make_intent(IntentName.COME_HOME, selection=(1,)), state)
    assert not _ground_return_selected(make_intent(IntentName.COME_HOME, selection=(1, 9)), state)
    refusal = enabled.session(SESSION).arbiter.check_intent(
        make_intent(IntentName.COME_HOME, selection=(1,), confirm=True), make_snapshot()
    )
    assert refusal is not None and refusal.reason.value == "unsupported"
    enabled.close()
