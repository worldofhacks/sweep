"""Isolated contract fixtures; no runtime fleet, map or motion data is synthesized."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from planner.navigation_runtime import TagDestinationBinding
from relay.navigation_service import NavigationError, NavigationService, state_projection


def approved_map():
    reference = {"bundleId": "building-map", "revision": "revision-1", "contentHash": "a" * 64}
    return {
        "reference": reference,
        "approval": {"reference": reference, "auditId": "approval-1"},
        "bundle": {
            "manifest": {"mapVersion": "map-v1", "floorId": "floor-1", "frame": "world"},
            "zones": [
                {"id": "room-a", "name": "Room A", "aliases": ["Workshop", "Shared"]},
                {"id": "room-b", "name": "Room B", "aliases": ["Office", "Shared"]},
            ],
            "corridors": [],
            "obstacles": [],
            "geofence": {"id": "boundary"},
        },
    }


def live_state(classes=("aircraft", "ground_vehicle")):
    return {
        "session": "test-session",
        "t": 1000,
        "roster_version": 1,
        "selection": list(range(1, len(classes) + 1)),
        "enabled_intent_names": ["navigate"],
        "estop": False,
        "mode": "indoor",
        "armed": True,
        "pending": None,
        "accepted_plan": None,
        "drones": [
            {
                "drone_id": index,
                "device_class": device_class,
                "connection_epoch": 1,
                "membership": "ready",
                "readiness_reasons": [],
                "selectable": True,
                "control_authority": True,
                "adapter_capabilities": ["navigate"],
                "flight_state": "hovering" if device_class == "aircraft" else "idle",
                "telemetry": {"t": 1000, "connection_epoch": 1, "state": "hovering"},
            }
            for index, device_class in enumerate(classes, 1)
        ],
    }


class Case:
    def __init__(self, tmp_path, **kwargs):
        self.now = 1000
        self.approved = approved_map()
        self.state = live_state()
        self.config = {"planning": {"measured_setting": 1}, "safety": {"measured_setting": 2}}
        self.path = tmp_path / "navigation.sqlite"
        self.service = NavigationService(
            self.path,
            clock_ms=lambda: self.now,
            approved_bundle=lambda _: self.approved,
            state=lambda _: self.state,
            motion_config=lambda _: self.config,
            **kwargs,
        )

    def request(self, intent="review-1"):
        catalog = self.service.catalog("test-session")["catalog"]
        return {
            "session": "test-session",
            "intentId": intent,
            "zoneId": "room-a",
            "rosterVersion": self.state["roster_version"],
            "selected": state_projection(self.state)["selected"],
            **{
                key: catalog[key]
                for key in ("catalogVersion", "map", "configVersion", "motionConfig")
            },
        }

    def preview(self, intent="review-1"):
        return self.service.preview("test-session", self.request(intent))

    def confirmation(self, envelope):
        return {
            "previewId": envelope["preview"]["previewId"],
            "intentId": envelope["preview"]["intentId"],
            "previewHash": envelope["previewHash"],
        }


@pytest.fixture
def case(tmp_path):
    case = Case(tmp_path)
    yield case
    case.service.close()


@pytest.mark.parametrize(
    "classes", [("aircraft",), ("ground_vehicle",), ("aircraft", "ground_vehicle")]
)
def test_button_reviews_bind_every_selected_class_and_never_create_routes(case, classes):
    case.state = live_state(classes)
    envelope = case.preview()
    preview = envelope["preview"]
    assert preview["selected"] == state_projection(case.state)["selected"]
    assert [outcome["code"] for outcome in preview["outcomes"]] == [
        "class_planner_unavailable"
    ] * len(classes)
    assert preview["routes"] == []
    assert preview["dispatchEligible"] is False
    assert preview["destination"]["zoneId"] == "room-a"
    assert preview["motionConfig"] == case.config
    assert (
        envelope["previewHash"]
        == hashlib.sha256(
            json.dumps(preview, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("query", ["Room A", "room-a", "Workshop", "Ｗｏｒｋｓｈｏｐ", "ROOM  A"])
def test_compiler_resolves_accepted_names_and_aliases_without_coordinates(case, query):
    result = case.service.compile("test-session", {"intentId": "named-1", "query": query})
    assert result["kind"] == "review"
    assert result["intent"] == {
        "name": "navigate",
        "args": {"zone_id": "room-a"},
        "selection": [1, 2],
        "mode": "indoor",
    }
    assert result["preview"]["dispatchEligible"] is False
    assert result["preview"]["routes"] == []


def test_compiler_resolves_a_pinned_tag_destination_through_the_review_workflow(case):
    case.service.close()
    case.service = NavigationService(
        case.path,
        clock_ms=lambda: case.now,
        approved_bundle=lambda _: case.approved,
        state=lambda _: case.state,
        motion_config=lambda _: case.config,
        tag_destinations=lambda _: (
            TagDestinationBinding(42, "room-a", "room-a-slot", 0.2, 0.5, 1.5),
        ),
    )
    result = case.service.compile("test-session", {"intentId": "tag-42", "query": "tag 42"})
    assert result["kind"] == "review"
    assert result["intent"]["args"] == {"zone_id": "room-a"}
    assert result["preview"]["destination"]["aliases"] == ["Workshop", "Shared", "tag 42"]


def test_compiler_ambiguity_clarifies_and_never_chooses_first_match(case):
    result = case.service.compile("test-session", {"intentId": "ambiguous-1", "query": "shared"})
    assert result["kind"] == "clarify"
    assert result["code"] == "destination_ambiguous"
    assert len(result["candidates"]) == 2
    assert "preview" not in result


def test_resolver_normalization_matches_console_lowercase_instead_of_broader_casefold(case):
    case.approved["bundle"]["zones"][0]["aliases"] = ["Straße"]
    assert (
        case.service.resolve("test-session", {"query": "straße", "selected": []})["kind"]
        == "resolved"
    )
    assert (
        case.service.resolve("test-session", {"query": "Strasse", "selected": []})["kind"]
        == "refused"
    )


@pytest.mark.parametrize(
    "configuration",
    [
        {"nested": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}},
        {"values": [0] * 2049},
    ],
)
def test_configuration_structural_bounds_match_the_console_parser(case, configuration):
    case.config = configuration
    with pytest.raises(NavigationError):
        case.service.catalog("test-session")


@pytest.mark.parametrize(
    "query",
    [
        "unknown",
        "do not go to Workshop",
        "never navigate to Room A",
        "Workshop then capture",
        "take off and navigate to Workshop",
        "Room A; survey the room",
    ],
)
def test_compiler_does_not_turn_unmatched_transcripts_into_destination_permission(case, query):
    result = case.service.compile("test-session", {"intentId": "query-1", "query": query})
    assert result == {
        "kind": "refused",
        "code": "destination_unknown",
        "reason": "No accepted destination matches that name or alias.",
    }


def test_compiler_never_selects_devices_when_selection_is_empty(case):
    case.state["selection"] = []
    assert (
        case.service.compile("test-session", {"intentId": "q", "query": "Workshop"})["code"]
        == "no_selection"
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("enabled_intent_names", [], "capability_disabled"),
        ("estop", True, "estop_active"),
        ("mode", "outdoorF", "mode_unsupported"),
    ],
)
def test_session_admission_produces_per_node_refusals(case, field, value, code):
    case.state[field] = value
    assert {item["code"] for item in case.preview()["preview"]["outcomes"]} == {code}


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("adapter_capabilities", [], "capability_disabled"),
        ("control_authority", False, "node_not_ready"),
        ("membership", "disconnected", "node_not_ready"),
        ("readiness_reasons", ["telemetry_stale"], "node_not_ready"),
        ("telemetry", None, "node_not_ready"),
        ("telemetry", {"connection_epoch": 2, "t": 1000}, "node_not_ready"),
        ("telemetry", {"connection_epoch": 1, "t": 1001}, "node_not_ready"),
        ("flight_state", "landed", "aircraft_grounded"),
        ("flight_state", "taking_off", "aircraft_grounded"),
    ],
)
def test_mixed_selection_refuses_specific_node_without_implicit_takeoff(case, field, value, code):
    case.state["drones"][0][field] = value
    outcomes = case.preview()["preview"]["outcomes"]
    assert outcomes[0]["code"] == code
    assert outcomes[1]["code"] == "class_planner_unavailable"


@pytest.mark.parametrize("field", ["map", "catalogVersion", "configVersion", "motionConfig"])
def test_review_refuses_changed_frozen_map_and_configuration(case, field):
    request = case.request()
    request[field] = {}
    with pytest.raises(NavigationError, match="differs from the request") as error:
        case.service.preview("test-session", request)
    assert error.value.code == "frozen_inputs_changed"


@pytest.mark.parametrize("change", ["selection", "epoch", "class", "roster"])
def test_review_refuses_stale_selection_type_epoch_or_roster(case, change):
    request = case.request()
    if change == "selection":
        case.state["selection"] = [1]
    elif change == "epoch":
        case.state["drones"][0]["connection_epoch"] += 1
    elif change == "class":
        case.state["drones"][0]["device_class"] = "ground_vehicle"
    else:
        case.state["roster_version"] += 1
    with pytest.raises(NavigationError) as error:
        case.service.preview("test-session", request)
    assert error.value.code in {"selection_changed", "roster_changed"}


@pytest.mark.parametrize(
    "configuration", [None, {}, {"value": float("nan")}, {"value": "x" * 17000}]
)
def test_catalog_never_invents_missing_or_invalid_motion_configuration(case, configuration):
    case.config = configuration
    with pytest.raises(NavigationError):
        case.service.catalog("test-session")


@pytest.mark.parametrize("change", ["none", "frame", "approval", "ambiguous"])
def test_catalog_requires_exact_current_approved_world_map(case, change):
    if change == "none":
        case.approved = None
    elif change == "frame":
        case.approved["bundle"]["manifest"]["frame"] = "building"
    elif change == "ambiguous":

        def ambiguous(_):
            raise ValueError("Explicitly select an approved map")

        case.service.approved_bundle = ambiguous
    else:
        case.approved["approval"]["reference"] = {"bundleId": "other-map"}
    with pytest.raises(NavigationError):
        case.service.catalog("test-session")


def test_catalog_pins_static_documents_without_claiming_generated_geometry(case):
    catalog = case.service.catalog("test-session")["catalog"]
    assert catalog["map"]["geometryPin"]["version"].startswith("static-")
    assert catalog["map"]["navigationPin"]["version"].startswith("catalog-")
    assert all(zone["reachability"] == "unknown" for zone in catalog["destinations"])
    original = copy.deepcopy(catalog)
    catalog["map"]["approvalId"] = "forged"
    case.approved["bundle"]["zones"][0]["name"] = "Changed Name"
    current = case.service.catalog("test-session")["catalog"]
    assert current["catalogVersion"] != original["catalogVersion"]
    assert current["map"]["approvalId"] == "approval-1"


def test_named_excluded_areas_are_refused_in_resolver_and_button_review(case):
    case.approved["bundle"]["obstacles"] = [
        {"id": "restricted", "name": "Restricted Room", "aliases": ["Back Room"]}
    ]
    assert (
        case.service.resolve("test-session", {"query": "Back Room", "selected": []})["code"]
        == "destination_excluded"
    )
    request = case.request()
    request["zoneId"] = "restricted"
    preview = case.service.preview("test-session", request)["preview"]
    assert {outcome["code"] for outcome in preview["outcomes"]} == {"destination_excluded"}
    assert preview["routes"] == []


def test_confirmation_revalidates_and_cannot_grant_a_motion_capability(case):
    envelope = case.preview()
    result = case.service.confirm("test-session", case.confirmation(envelope))
    assert result["status"] == "refused"
    assert result["code"] == "navigation_execution_unavailable"
    assert result["dispatchEligible"] is False
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "confirmation_consumed"
    )


class FlightExecution:
    def __init__(self):
        self.preview_calls = []
        self.confirm_calls = []
        self.reserve_calls = []
        self.dispatch_reserved_calls = []

    def preview(self, session, preview):
        self.preview_calls.append((session, copy.deepcopy(preview)))
        target = preview["selected"][0]
        arrival = {"xM": 2.0, "yM": 3.0, "zM": 1.5, "floorId": "floor-1", "frame": "world"}
        return {
            "routes": [
                {
                    "target": target,
                    "waypoints": [
                        {
                            "xM": 0.0,
                            "yM": 0.0,
                            "zM": 1.5,
                            "floorId": "floor-1",
                            "frame": "world",
                        },
                        arrival,
                    ],
                    "arrivalSlot": {
                        "slotId": "room-a-slot-1",
                        "zoneId": "room-a",
                        "position": arrival,
                    },
                    "holdBehavior": "hover",
                }
            ],
            "outcomes": [
                {
                    "target": target,
                    "status": "planned",
                    "code": "route_qualified",
                    "detail": "Signed deployment route.",
                }
            ],
            "execution": {
                "planHash": "b" * 64,
                "mapPin": preview["map"]["mapPin"],
                "geometryPin": preview["map"]["geometryPin"],
                "navigationPin": preview["map"]["navigationPin"],
                "approvalId": preview["map"]["approvalId"],
                "configurationSha256": "c" * 64,
                "permissionZoneIds": ["room-a"],
            },
        }

    def confirm(self, session, preview):
        self.confirm_calls.append((session, copy.deepcopy(preview)))
        return {
            "status": "accepted",
            "code": "navigation_dispatched",
            "detail": "The qualified route was accepted.",
        }

    def reserve(self, session, preview):
        self.reserve_calls.append((session, copy.deepcopy(preview)))
        return {
            "status": "accepted",
            "code": "navigation_reserved",
            "detail": "The qualified route was reserved.",
        }

    def dispatch_reserved(self, session, preview_id):
        self.dispatch_reserved_calls.append((session, preview_id))
        return {
            "status": "accepted",
            "code": "navigation_dispatched",
            "detail": "The reserved route was accepted.",
        }


def test_qualified_flight_preview_dispatches_the_exact_retained_route(tmp_path):
    flight = FlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft",))
        envelope = case.preview()
        preview = envelope["preview"]
        assert preview["dispatchEligible"] is True
        assert preview["destination"]["reachability"] == "reachable"
        assert preview["execution"]["mapPin"] == preview["map"]["mapPin"]

        result = case.service.confirm("test-session", case.confirmation(envelope))

        assert result == {
            "status": "accepted",
            "code": "navigation_dispatched",
            "detail": "The qualified route was accepted.",
            "previewId": preview["previewId"],
            "intentId": preview["intentId"],
            "dispatchEligible": True,
        }
        assert flight.confirm_calls == [("test-session", preview)]
        repeat = case.service.confirm("test-session", case.confirmation(envelope))
        assert repeat["code"] == "confirmation_consumed"
    finally:
        case.service.close()


@pytest.mark.parametrize("binding", ["valid", "missing", "wrong"])
def test_distinct_flight_map_requires_explicit_authoring_binding(tmp_path, binding):
    class BoundFlightExecution(FlightExecution):
        def preview(self, session, preview):
            result = super().preview(session, preview)
            execution = result["execution"]
            if binding != "missing":
                execution["authoringMapPin"] = copy.deepcopy(execution["mapPin"])
                if binding == "wrong":
                    execution["authoringMapPin"]["contentSha256"] = "f" * 64
            execution["mapPin"] = {"version": "flight-map-v1", "contentSha256": "d" * 64}
            return result

    flight = BoundFlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft",))
        if binding != "valid":
            with pytest.raises(NavigationError, match="pins do not bind the approved map"):
                case.preview()
            assert flight.confirm_calls == []
            return
        envelope = case.preview()
        preview = envelope["preview"]
        assert preview["dispatchEligible"] is (binding == "valid")
        if binding == "valid":
            assert preview["execution"]["authoringMapPin"] == preview["map"]["mapPin"]
            assert preview["execution"]["mapPin"] != preview["map"]["mapPin"]
            result = case.service.confirm("test-session", case.confirmation(envelope))
            assert result["status"] == "accepted"
            assert flight.confirm_calls == [("test-session", preview)]
        else:
            assert flight.confirm_calls == []
    finally:
        case.service.close()


def test_qualified_flight_review_can_be_reserved_then_dispatched(tmp_path):
    flight = FlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft",))
        envelope = case.preview("reserved-review")
        confirmation = case.confirmation(envelope)

        reserved = case.service.reserve("test-session", confirmation)

        assert reserved["status"] == "accepted"
        assert flight.reserve_calls == [("test-session", envelope["preview"])]
        dispatched = case.service.dispatch_reserved(
            "test-session", envelope["preview"]["previewId"]
        )
        assert dispatched == {
            "status": "accepted",
            "code": "navigation_dispatched",
            "detail": "The reserved route was accepted.",
        }
        assert flight.dispatch_reserved_calls == [
            ("test-session", envelope["preview"]["previewId"])
        ]
    finally:
        case.service.close()


def test_qualified_fleet_preview_requires_and_dispatches_every_selected_route(tmp_path):
    class FleetExecution(FlightExecution):
        def preview(self, session, preview):
            result = super().preview(session, preview)
            second = copy.deepcopy(result["routes"][0])
            second["target"] = preview["selected"][1]
            second["arrivalSlot"]["slotId"] = "room-a-slot-2"
            second["waypoints"][-1]["xM"] = 4.0
            second["arrivalSlot"]["position"]["xM"] = 4.0
            result["routes"].append(second)
            result["outcomes"].append({**result["outcomes"][0], "target": second["target"]})
            return result

    flight = FleetExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft", "aircraft"))
        envelope = case.preview()
        assert envelope["preview"]["dispatchEligible"] is True
        assert [route["target"]["id"] for route in envelope["preview"]["routes"]] == [1, 2]
        result = case.service.confirm("test-session", case.confirmation(envelope))
        assert result["status"] == "accepted"
        assert flight.confirm_calls == [("test-session", envelope["preview"])]
    finally:
        case.service.close()


def test_changed_state_invalidates_qualified_preview_without_dispatch(tmp_path):
    flight = FlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft",))
        envelope = case.preview()
        case.state["estop"] = True

        result = case.service.confirm("test-session", case.confirmation(envelope))

        assert result["status"] == "invalidated"
        assert result["code"] == "frozen_inputs_changed"
        assert flight.confirm_calls == []
    finally:
        case.service.close()


def test_fleet_executor_cannot_override_a_target_readiness_refusal(tmp_path):
    flight = FlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(("aircraft",))
        case.state["drones"][0]["control_authority"] = False
        preview = case.preview()["preview"]
        assert preview["dispatchEligible"] is False
        assert preview["outcomes"][0]["code"] == "node_not_ready"
        assert flight.preview_calls == []
    finally:
        case.service.close()


@pytest.mark.parametrize("classes", [("ground_vehicle",), ("aircraft", "ground_vehicle")])
def test_flight_executor_preserves_ground_and_mixed_review_only_previews(tmp_path, classes):
    flight = FlightExecution()
    case = Case(tmp_path, flight_execution=flight)
    try:
        case.state = live_state(classes)

        preview = case.preview()["preview"]

        assert preview["dispatchEligible"] is False
        assert "execution" not in preview
        assert preview["routes"] == []
        assert flight.preview_calls == []
        assert flight.confirm_calls == []
    finally:
        case.service.close()


@pytest.mark.parametrize(
    "change",
    ["config", "map", "approval", "selection", "capability", "epoch", "flight_state", "estop"],
)
def test_confirmation_invalidates_on_any_frozen_input_change(case, change):
    envelope = case.preview()
    if change == "config":
        case.config["planning"]["measured_setting"] += 1
    elif change == "map":
        case.approved["bundle"]["zones"][0]["name"] = "new-name"
    elif change == "approval":
        case.approved["approval"]["auditId"] = "approval-2"
    elif change == "selection":
        case.state["selection"] = [1]
    elif change == "capability":
        case.state["enabled_intent_names"] = []
    elif change == "epoch":
        case.state["drones"][0]["connection_epoch"] = 2
    elif change == "flight_state":
        case.state["drones"][0]["flight_state"] = "landed"
    else:
        case.state["estop"] = True
    result = case.service.confirm("test-session", case.confirmation(envelope))
    assert result["status"] == "invalidated"
    assert result["code"] == "frozen_inputs_changed"


def test_transient_state_drift_cannot_restore_a_review(case):
    envelope = case.preview()
    case.state["estop"] = True
    case.service.observe_state("test-session", case.state)
    case.state["estop"] = False
    case.service.observe_state("test-session", case.state)
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "frozen_inputs_changed"
    )


def test_external_config_generation_invalidates_even_if_values_return_to_original(case):
    envelope = case.preview()
    case.service.invalidate("test-session")
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "frozen_inputs_changed"
    )


@pytest.mark.parametrize("now", [999, 16000, 16001])
def test_expiry_and_clock_rollback_retire_confirmation(case, now):
    envelope = case.preview()
    case.now = now
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "preview_expired"
    )


@pytest.mark.parametrize("change", ["hash", "intent", "session", "unknown"])
def test_confirmation_identity_mismatch_cannot_consume_another_review(case, change):
    envelope = case.preview()
    request = case.confirmation(envelope)
    session = "test-session"
    if change == "hash":
        request["previewHash"] = "0" * 64
    elif change == "intent":
        request["intentId"] = "other-intent"
    elif change == "unknown":
        request["previewId"] = "missing-preview"
    else:
        session = "other-session"
    with pytest.raises(NavigationError):
        case.service.confirm(session, request)
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "navigation_execution_unavailable"
    )


def test_preview_bytes_are_immutable_and_confirmation_ignores_browser_timestamp_rebasing(case):
    envelope = case.preview()
    request = case.confirmation(envelope)
    envelope["preview"]["receivedAt"] = 1
    envelope["preview"]["selected"].clear()
    assert (
        case.service.confirm("test-session", request)["code"] == "navigation_execution_unavailable"
    )


def test_process_restart_preserves_record_but_retires_old_authority(case):
    envelope = case.preview()
    case.service.close()
    case.service = NavigationService(
        case.path,
        clock_ms=lambda: case.now,
        approved_bundle=lambda _: case.approved,
        state=lambda _: case.state,
        motion_config=lambda _: case.config,
    )
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "frozen_inputs_changed"
    )


def test_confirmation_is_one_shot_under_concurrent_requests(case):
    envelope = case.preview()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: case.service.confirm("test-session", case.confirmation(envelope)),
                range(4),
            )
        )
    assert sorted(result["code"] for result in results) == ["confirmation_consumed"] * 3 + [
        "navigation_execution_unavailable"
    ]


def test_retention_is_bounded_and_intent_ids_cannot_replace_a_captured_review(tmp_path):
    case = Case(tmp_path, max_previews=2)
    case.preview("one")
    with pytest.raises(NavigationError) as error:
        case.preview("one")
    assert error.value.code == "intent_reused"
    case.preview("two")
    with pytest.raises(NavigationError) as error:
        case.preview("three")
    assert error.value.code == "review_capacity"
    case.now = 16000
    assert case.preview("three")["preview"]["intentId"] == "three"
    case.service.close()


def routes_for(request, catalog, state):
    routes, outcomes = [], []
    for target in request["selected"]:
        start = {
            "xM": target["id"],
            "yM": 1,
            "zM": 1 if target["deviceClass"] == "aircraft" else 0,
            "floorId": "floor-1",
            "frame": "world",
        }
        end = {**start, "yM": 2}
        routes.append(
            {
                "target": target,
                "waypoints": [start, end],
                "arrivalSlot": {
                    "slotId": f"slot-{target['id']}",
                    "zoneId": "room-a",
                    "position": end,
                },
                "holdBehavior": "hover" if target["deviceClass"] == "aircraft" else "stop",
            }
        )
        outcomes.append(
            {
                "target": target,
                "status": "planned",
                "code": "route_reviewed",
                "detail": "Isolated test route provider evidence.",
            }
        )
    return {"routes": routes, "outcomes": outcomes}


def test_route_provider_freezes_class_specific_routes_slots_and_holds_without_dispatch(case):
    case.service.route_preview = routes_for
    envelope = case.preview()
    assert [route["holdBehavior"] for route in envelope["preview"]["routes"]] == ["hover", "stop"]
    assert envelope["preview"]["dispatchEligible"] is False
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "navigation_execution_unavailable"
    )


@pytest.mark.parametrize(
    "change", ["epoch", "hold", "floor", "duplicate_slot", "endpoint", "partial", "unsolicited"]
)
def test_provider_cannot_supply_unbound_routes_or_arrival_slots(case, change):
    def provider(*args):
        result = copy.deepcopy(routes_for(*args))
        if change == "epoch":
            result["routes"][0]["target"]["epoch"] = 2
        elif change == "hold":
            result["routes"][1]["holdBehavior"] = "hover"
        elif change == "floor":
            result["routes"][0]["waypoints"][0]["floorId"] = "other-floor"
        elif change == "duplicate_slot":
            result["routes"][1]["arrivalSlot"]["slotId"] = "slot-1"
        elif change == "endpoint":
            result["routes"][0]["arrivalSlot"]["position"] = result["routes"][0]["waypoints"][0]
        elif change == "partial":
            result["outcomes"].pop()
        else:
            result["outcomes"][0]["status"] = "refused"
        return result

    case.service.route_preview = provider
    with pytest.raises(NavigationError):
        case.preview()


def test_late_provider_output_cannot_bind_a_new_roster(case):
    def provider(*args):
        result = routes_for(*args)
        case.state["roster_version"] += 1
        return result

    case.service.route_preview = provider
    with pytest.raises(NavigationError) as error:
        case.preview()
    assert error.value.code == "frozen_inputs_changed"


def test_new_state_event_without_material_change_does_not_retire_review(case):
    envelope = case.preview()
    case.state["event_id"] = "new-event"
    case.state["state_sequence"] = 5
    case.state["t"] += 1
    case.service.observe_state("test-session", case.state)
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "navigation_execution_unavailable"
    )


@pytest.mark.parametrize(
    "change", ["extra", "coordinates", "empty", "class", "duplicate", "nonfinite"]
)
def test_untrusted_requests_are_bounded_and_exact(case, change):
    request = case.request()
    if change == "extra":
        request["dispatchEligible"] = True
    elif change == "coordinates":
        request["zoneId"] = {"x": 1, "y": 2}
    elif change == "empty":
        request["selected"] = []
    elif change == "class":
        request["selected"][0]["deviceClass"] = {}
    elif change == "duplicate":
        request["selected"].append(request["selected"][0])
    else:
        request["motionConfig"] = {"speed": float("inf")}
    with pytest.raises(NavigationError):
        case.service.preview("test-session", request)


@pytest.mark.parametrize("operation", ["preview", "confirm", "observe_state", "invalidate"])
def test_storage_failures_are_typed_and_never_report_a_confirmation(case, operation):
    envelope = case.preview()
    request = case.request("another-review")
    case.service.close()
    args = {
        "preview": ("test-session", request),
        "confirm": ("test-session", case.confirmation(envelope)),
        "observe_state": ("test-session", case.state),
        "invalidate": ("test-session",),
    }
    with pytest.raises(NavigationError) as error:
        getattr(case.service, operation)(*args[operation])
    assert error.value.code == "storage_unavailable"
    assert error.value.status_code == 503


def test_persisted_preview_fields_cannot_be_replaced(case):
    envelope = case.preview()
    with sqlite3.connect(case.path) as database:
        with pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"):
            database.execute("UPDATE navigation_previews SET preview_json='{}'")
    assert (
        case.service.confirm("test-session", case.confirmation(envelope))["code"]
        == "navigation_execution_unavailable"
    )


def test_corrupt_preview_cannot_be_confirmed(case):
    envelope = case.preview()
    with sqlite3.connect(case.path) as database:
        database.execute("DROP TRIGGER navigation_preview_immutable")
        database.execute("UPDATE navigation_previews SET preview_json='{}'")
    with pytest.raises(NavigationError) as error:
        case.service.confirm("test-session", case.confirmation(envelope))
    assert error.value.code == "storage_unavailable"


def test_provider_cannot_add_a_dispatch_permission_flag(case):
    def provider(*args):
        return {**routes_for(*args), "dispatchEligible": True}

    case.service.route_preview = provider
    with pytest.raises(NavigationError) as error:
        case.preview()
    assert error.value.code == "invalid_payload"


def test_real_map_store_approval_wires_to_named_navigation_and_retires_on_edit(tmp_path):
    from relay.map_authoring import MapAuthoringStore
    from tests.world_bundle_fixtures import fixture_world_draft

    maps = MapAuthoringStore(tmp_path / "maps.sqlite", clock_ms=lambda: 1000)
    draft = fixture_world_draft()
    reference = maps.save("test-session", draft, None, "test-console")
    validation = maps.validate("test-session", reference, "test-console")
    assert validation["valid"]
    maps.approve("test-session", reference, validation["validationId"], "test-console")
    navigation = NavigationService(
        tmp_path / "navigation.sqlite",
        clock_ms=lambda: 1000,
        approved_bundle=maps.approved_bundle,
        state=lambda _: live_state(),
        motion_config=lambda _: {"planning": {"measured_setting": 1}},
    )
    envelope = navigation.compile("test-session", {"intentId": "compiled", "query": "entry"})
    assert envelope["kind"] == "review"
    assert envelope["intent"]["args"] == {"zone_id": "lobby"}
    assert envelope["preview"]["map"]["mapPin"]["contentSha256"] == reference["contentHash"]
    assert (
        navigation.resolve("test-session", {"query": "glass", "selected": []})["code"]
        == "destination_excluded"
    )
    draft["features"][1]["name"] = "changed-lobby"
    maps.save("test-session", draft, reference, "test-console")
    result = navigation.confirm(
        "test-session",
        {
            "previewId": envelope["preview"]["previewId"],
            "intentId": "compiled",
            "previewHash": envelope["previewHash"],
        },
    )
    assert result["code"] == "frozen_inputs_changed"
    navigation.close()


@pytest.fixture
def approved_maps(tmp_path):
    from relay.map_authoring import MapAuthoringStore
    from tests.world_bundle_fixtures import fixture_world_draft

    store = MapAuthoringStore(tmp_path / "maps.sqlite", clock_ms=lambda: 1000)
    references, drafts = [], []
    for index in (1, 2):
        draft = fixture_world_draft()
        draft["metadata"]["mapVersion"] = f"map-{index}"
        draft["metadata"]["floorId"] = f"floor-{index}"
        reference = store.save("test-session", draft, None, "console")
        validation = store.validate("test-session", reference, "console")
        store.approve("test-session", reference, validation["validationId"], "console")
        references.append(reference)
        drafts.append(draft)
    navigation = NavigationService(
        tmp_path / "navigation.sqlite",
        clock_ms=lambda: 1000,
        approved_bundle=store.approved_bundle,
        state=lambda _: live_state(),
        motion_config=lambda _: {"planning": {"measured_setting": 1}},
    )
    yield store, navigation, references, drafts
    navigation.close()


def test_multiple_approved_maps_require_explicit_durable_session_selection(approved_maps):
    _, navigation, references, _ = approved_maps
    with pytest.raises(NavigationError) as error:
        navigation.catalog("test-session")
    assert error.value.code == "map_unavailable"
    receipt = navigation.select_map("test-session", {"reference": references[1]}, "console")
    assert receipt["reference"] == references[1]
    assert receipt["selectedBy"] == "console"
    assert receipt["selectedAt"] == 1000
    assert receipt["selectionId"]
    assert navigation.catalog("test-session")["catalog"]["map"]["floorId"] == "floor-2"
    receipt["reference"]["revision"] = "999"
    assert (
        navigation.catalog("test-session")["catalog"]["map"]["mapId"] == references[1]["bundleId"]
    )


def test_active_map_a_b_a_cannot_restore_a_frozen_review(approved_maps):
    _, navigation, references, _ = approved_maps
    navigation.select_map("test-session", {"reference": references[0]})
    envelope = navigation.compile("test-session", {"intentId": "map-review", "query": "entry"})
    navigation.select_map("test-session", {"reference": references[1]})
    navigation.select_map("test-session", {"reference": references[0]})
    result = navigation.confirm(
        "test-session",
        {
            "previewId": envelope["preview"]["previewId"],
            "intentId": "map-review",
            "previewHash": envelope["previewHash"],
        },
    )
    assert result["code"] == "frozen_inputs_changed"


def test_active_map_never_silently_follows_an_edited_or_newly_approved_head(approved_maps):
    store, navigation, references, drafts = approved_maps
    navigation.select_map("test-session", {"reference": references[0]})
    drafts[0]["features"][1]["name"] = "updated lobby"
    drafts[0]["metadata"]["mapVersion"] = "map-1-revised"
    revised = store.save("test-session", drafts[0], references[0], "console")
    validation = store.validate("test-session", revised, "console")
    store.approve("test-session", revised, validation["validationId"], "console")
    with pytest.raises(NavigationError):
        navigation.catalog("test-session")
    with pytest.raises(NavigationError):
        navigation.select_map("test-session", {"reference": references[0]})
    navigation.select_map("test-session", {"reference": revised})
    catalog = navigation.catalog("test-session")["catalog"]
    assert catalog["map"]["mapPin"]["contentSha256"] == revised["contentHash"]


@pytest.mark.parametrize("change", ["session", "hash", "actor", "revision"])
def test_map_selection_cannot_bind_an_unapproved_or_spoofed_reference(approved_maps, change):
    _, navigation, references, _ = approved_maps
    request = {"reference": copy.deepcopy(references[0])}
    session = "test-session"
    if change == "session":
        session = "other-session"
    elif change == "hash":
        request["reference"]["contentHash"] = "0" * 64
    elif change == "actor":
        request["selectedBy"] = "forged-owner"
    else:
        request["reference"]["revision"] = "999"
    with pytest.raises(NavigationError):
        navigation.select_map(session, request)
    with pytest.raises(NavigationError):
        navigation.catalog("test-session")


def test_failed_map_selection_commit_does_not_change_the_active_map(approved_maps):
    _, navigation, references, _ = approved_maps
    navigation.select_map("test-session", {"reference": references[0]})
    navigation._db.execute("PRAGMA query_only=ON")
    with pytest.raises(NavigationError) as error:
        navigation.select_map("test-session", {"reference": references[1]})
    assert error.value.code == "storage_unavailable"
    navigation._db.execute("PRAGMA query_only=OFF")
    assert (
        navigation.catalog("test-session")["catalog"]["map"]["mapId"] == references[0]["bundleId"]
    )


def test_map_selection_audit_is_immutable(approved_maps):
    _, navigation, references, _ = approved_maps
    navigation.select_map("test-session", {"reference": references[0]})
    with pytest.raises(sqlite3.IntegrityError, match="selection audit is immutable"):
        navigation._db.execute("UPDATE navigation_map_selections SET selected_by='forged'")
    navigation._db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="selection audit is immutable"):
        navigation._db.execute("DELETE FROM navigation_map_selections")
    navigation._db.rollback()


def test_explicit_active_map_persists_across_process_restart(approved_maps, tmp_path):
    store, navigation, references, _ = approved_maps
    navigation.select_map("test-session", {"reference": references[1]})
    second = NavigationService(
        tmp_path / "navigation.sqlite",
        clock_ms=lambda: 1000,
        approved_bundle=store.approved_bundle,
        state=lambda _: live_state(),
        motion_config=lambda _: {"planning": {"measured_setting": 1}},
    )
    assert second.catalog("test-session")["catalog"]["map"]["mapId"] == references[1]["bundleId"]
    second.close()
