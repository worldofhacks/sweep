from __future__ import annotations

import pytest

from relay.contracts import parse_membership_request, parse_telemetry
from relay.state import FleetRegistry
from relay.tests.conftest import membership_payload, telemetry_payload
from relay.tests.test_navigation_service import Case, FlightExecution, live_state


def test_real_registry_telemetry_can_qualify_an_aircraft_navigation_review(tmp_path):
    case = Case(tmp_path, flight_execution=FlightExecution())
    registry = FleetRegistry(telemetry_freshness_ms=1000)
    scope = {"session": "test-session", "timestamp": 1000}
    registry.apply_join(
        parse_membership_request(
            membership_payload(
                action="join", event_id="join", capabilities=["flight", "navigate"], **scope
            )
        )
    )
    registry.apply_telemetry(
        parse_telemetry(telemetry_payload(event_id="telemetry", **scope)),
        transition_event_id="telemetry-ready",
    )
    registry.apply_readiness(
        parse_membership_request(membership_payload(action="readiness", event_id="ready", **scope))
    )
    registry.set_selection((1,))
    case.state = registry.state_event(session="test-session", t=1000, event_id="state")
    try:
        assert case.state["drones"][0]["selectable"] is True
        assert "connection_epoch" not in case.state["drones"][0]["telemetry"]

        envelope = case.preview()

        assert envelope["preview"]["dispatchEligible"] is True
        assert (
            case.service.confirm("test-session", case.confirmation(envelope))["status"]
            == "accepted"
        )
    finally:
        case.service.close()


@pytest.mark.parametrize("device_index", [0, 1])
def test_normal_aircraft_telemetry_ticks_do_not_retire_a_navigation_review(tmp_path, device_index):
    case = Case(tmp_path, flight_execution=FlightExecution())
    case.state = live_state(("aircraft", "aircraft"))
    case.state["selection"] = [1]
    try:
        envelope = case.preview()
        for index in range(1, 4):
            case.now += 100
            case.state["t"] = case.now
            case.state["drones"][device_index]["telemetry"].update(
                t=case.now, x=index * 0.01, vx=0.1, event_id=f"telemetry-{index}"
            )
            case.service.observe_state("test-session", case.state)

        result = case.service.confirm("test-session", case.confirmation(envelope))

        assert result["status"] == "accepted"
    finally:
        case.service.close()


@pytest.mark.parametrize(
    "change", ["armed", "flight_state", "battery", "link", "pos_quality", "authority", "stale"]
)
def test_safety_authority_changes_still_retire_navigation_reviews(tmp_path, change):
    case = Case(tmp_path, flight_execution=FlightExecution())
    case.state = live_state(("aircraft",))
    node = case.state["drones"][0]
    node["telemetry"].update(battery=0.9, link=0.9, pos_quality=0.9)
    try:
        envelope = case.preview()
        if change == "armed":
            case.state["armed"] = False
        elif change == "flight_state":
            node["flight_state"] = "landed"
            node["telemetry"]["state"] = "landed"
        elif change == "authority":
            node["control_authority"] = False
        elif change == "stale":
            node["readiness_reasons"] = ["telemetry_stale"]
            node["selectable"] = False
        else:
            node["telemetry"][change] = 0.01
        case.service.observe_state("test-session", case.state)

        result = case.service.confirm("test-session", case.confirmation(envelope))

        assert result["status"] == "invalidated"
        assert result["code"] == "frozen_inputs_changed"
    finally:
        case.service.close()
