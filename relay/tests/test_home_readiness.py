"""Home declarations wait for measured ground evidence without granting other readiness."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from relay.audit import AuditLogError
from relay.auth import Principal
from relay.autonomy import create_autonomy_app
from relay.contracts import parse_membership_request, parse_telemetry
from relay.state import FleetRegistry
from relay.tests.conftest import (
    ADAPTER_KEY,
    SESSION,
    membership_payload,
    telemetry_payload,
)
from relay.tests.test_autonomy import _config, _settings

NOW = 1_756_700_000_000


def registry():
    fleet = FleetRegistry(telemetry_freshness_ms=1_000, min_home_position_quality=0.7)
    fleet.apply_join(parse_membership_request(membership_payload(action="join", event_id="join")))
    return fleet


def readiness(fleet, *, now=NOW, **changes):
    return fleet.apply_readiness(
        parse_membership_request(
            membership_payload(action="readiness", event_id=f"ready-{now}", **changes)
        ),
        now_ms=now,
    )


def telemetry(fleet, *, now=NOW, quality=0.7, **changes):
    raw = telemetry_payload(event_id=f"telemetry-{now}", state="landed")
    raw.update(pos_quality=quality, **changes)
    return fleet.apply_telemetry(
        parse_telemetry(raw), transition_event_id=f"transition-{now}", now_ms=now
    )


def drone(fleet, now=NOW):
    return fleet.state_event(session=SESSION, t=now, event_id="state")["drones"][0]


def test_early_home_declaration_recovers_without_another_readiness_message():
    fleet = registry()
    readiness(fleet)
    assert drone(fleet)["home_pose"] is None
    recovered = telemetry(fleet)
    assert recovered is not None and recovered.membership.value == "ready"
    assert drone(fleet)["home_pose"] == {"x": 1.0, "y": 2.0, "z": 0.5}
    telemetry(fleet, now=NOW + 1, t=NOW + 1, x=8.0)
    assert drone(fleet)["home_pose"]["x"] == 1.0


@pytest.mark.parametrize(
    "changes,now",
    [
        ({"quality": 0.0}, NOW),
        ({"quality": 0.69}, NOW),
        ({"state": "hovering"}, NOW),
        ({"state": "taking_off"}, NOW),
        ({"state": "landing"}, NOW),
        ({}, NOW + 1_001),
        ({"t": NOW + 1}, NOW),
    ],
)
def test_home_waits_for_eligible_evidence_and_uses_relay_receipt_time(changes, now):
    fleet = registry()
    readiness(fleet)
    telemetry(fleet, now=now, **changes)
    assert drone(fleet, now)["home_pose"] is None
    assert "home_pose_missing" in drone(fleet, now)["readiness_reasons"]
    readiness(fleet, now=now)  # re-sending a declaration cannot approve bad evidence
    assert drone(fleet, now)["home_pose"] is None
    telemetry(fleet, now=NOW + 2_000, t=NOW + 2_000)
    assert drone(fleet, NOW + 2_000)["home_pose"] is not None


def test_stale_ground_telemetry_cannot_become_home_through_a_delayed_declaration():
    fleet = registry()
    telemetry(fleet)
    readiness(fleet, now=NOW + 1_001)  # signed request and telemetry timestamps match
    assert drone(fleet, NOW + 1_001)["home_pose"] is None


@pytest.mark.parametrize("offset,captured", [(17, True), (1_000, True), (1_001, False)])
def test_home_respects_the_existing_configured_future_clock_skew(offset, captured):
    fleet = FleetRegistry(
        telemetry_freshness_ms=1_000,
        min_home_position_quality=0.7,
        future_clock_skew_ms=1_000,
    )
    fleet.apply_join(parse_membership_request(membership_payload(action="join", event_id="join")))
    readiness(fleet)
    telemetry(fleet, t=NOW + offset)
    assert (drone(fleet)["home_pose"] is not None) is captured


def test_cancelled_or_absent_declaration_cannot_capture_home():
    fleet = registry()
    telemetry(fleet)
    assert drone(fleet)["home_pose"] is None
    readiness(fleet, home_pose_confirmed=False)
    telemetry(fleet, now=NOW + 1, t=NOW + 1)
    assert drone(fleet)["home_pose"] is None


def test_home_recovery_never_grants_authority_or_asserts_an_rc_operator():
    fleet = registry()
    readiness(fleet, control_authority=False, rc_safety_operator_present=False)
    telemetry(fleet)
    state = drone(fleet)
    assert state["home_pose"] is not None
    assert state["control_authority"] is False
    assert state["rc_safety_operator_present"] is False
    assert state["selectable"] is False


def test_pending_home_and_other_declarations_do_not_survive_rejoin():
    fleet = registry()
    readiness(fleet)
    fleet.disconnect(drone_id=1, connection_epoch=1, t=NOW, event_id="disconnect")
    fleet.apply_join(parse_membership_request(membership_payload(action="join", event_id="rejoin")))
    telemetry(fleet, connection_epoch=2)
    state = drone(fleet)
    assert state["home_pose"] is None
    assert state["control_authority"] is False
    assert state["rc_safety_operator_present"] is False


def test_stale_ground_evidence_cannot_clear_a_previously_confirmed_home():
    fleet = registry()
    telemetry(fleet)
    readiness(fleet)
    readiness(fleet, now=NOW + 1_001, home_pose_confirmed=False)
    assert drone(fleet, NOW + 1_001)["home_pose"] is not None


@pytest.mark.parametrize("prior_state", ["landed", "hovering"])
def test_pending_explicit_clear_waits_for_fresh_ground_before_recapturing_home(prior_state):
    fleet = registry()
    telemetry(fleet)
    readiness(fleet)
    original_home = drone(fleet)["home_pose"]
    telemetry(fleet, state=prior_state)
    readiness(fleet, now=NOW + 1_001, home_pose_confirmed=False)
    assert drone(fleet, NOW + 1_001)["home_pose"] == original_home
    telemetry(fleet, now=NOW + 1_002, t=NOW + 1_002, state="hovering", x=8.0)
    assert drone(fleet, NOW + 1_002)["home_pose"] == original_home

    telemetry(fleet, now=NOW + 1_003, t=NOW + 1_003, x=8.0)
    assert drone(fleet, NOW + 1_003)["home_pose"] is None
    readiness(fleet, now=NOW + 1_003)
    assert drone(fleet, NOW + 1_003)["home_pose"] == {"x": 8.0, "y": 2.0, "z": 0.5}


def test_rejoin_reset_alone_does_not_clear_existing_home_on_fresh_ground_telemetry():
    fleet = registry()
    telemetry(fleet)
    readiness(fleet)
    original_home = drone(fleet)["home_pose"]
    fleet.disconnect(drone_id=1, connection_epoch=1, t=NOW, event_id="disconnect")
    fleet.apply_join(parse_membership_request(membership_payload(action="join", event_id="rejoin")))
    telemetry(fleet, connection_epoch=2, x=8.0)
    assert drone(fleet)["home_pose"] == original_home
    assert "home_pose_missing" in drone(fleet)["readiness_reasons"]


def test_composed_relay_uses_deployed_position_quality_age_and_clock_limits(
    tmp_path, clock, event_ids
):
    config = _config()
    config = replace(
        config,
        safety=replace(config.safety, min_position_quality=0.7, max_position_age_ms=500),
    )
    app, composition = create_autonomy_app(
        _settings(tmp_path), config, clock=clock, event_ids=event_ids
    )
    try:
        with TestClient(app):
            session = app.state.relay_runtime.session(SESSION)
            adapter = Principal("adapter", 1, ADAPTER_KEY)
            session.process_membership(membership_payload(action="join", event_id="join"), adapter)
            session.process_membership(
                membership_payload(action="readiness", event_id="readiness"), adapter
            )
            clock.advance(501)
            raw = telemetry_payload(event_id="old", state="landed")
            raw.update(pos_quality=0.7, t=clock() - 501)
            session.process_telemetry(raw, adapter)
            state = session.current_state()["drones"][0]
            assert state["telemetry"]["t"] == clock() - 501
            assert state["home_pose"] is None
            raw.update(event_id="weak", pos_quality=0.69, t=clock())
            session.process_telemetry(raw, adapter)
            assert session.current_state()["drones"][0]["home_pose"] is None
            raw.update(event_id="valid", pos_quality=0.7, t=clock() + 17)
            session.process_telemetry(raw, adapter)
            assert session.current_state()["drones"][0]["home_pose"] is not None
    finally:
        composition.close()


def test_home_capture_from_telemetry_rolls_back_if_its_audit_write_fails(
    relay_session, adapter_principal, monkeypatch
):
    relay_session.process_membership(
        membership_payload(action="join", event_id="join"), adapter_principal
    )
    relay_session.process_membership(
        membership_payload(action="readiness", event_id="ready"), adapter_principal
    )
    before_roster = relay_session.registry.roster_version

    def fail_audit(*args, **kwargs):
        raise AuditLogError("injected home audit failure")

    monkeypatch.setattr(relay_session.audit_log, "append_batch", fail_audit)
    with pytest.raises(AuditLogError, match="injected home audit failure"):
        relay_session.process_telemetry(
            telemetry_payload(event_id="grounded", state="landed"), adapter_principal
        )
    assert drone(relay_session.registry)["home_pose"] is None
    assert relay_session.registry.roster_version == before_roster
