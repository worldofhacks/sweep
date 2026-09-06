"""Grounding facts for a mixed session: each class's telemetry state read in its own vocabulary."""

from __future__ import annotations

import pytest

from language.compiler import InMemoryAuditSink, TranscriptCompiler, _snapshot_matches_facts
from language.contracts import CompilerReason, OutcomeKind, build_grounding_facts
from language.transport import ModelResponse, TransportError
from tests.autonomy_fixtures import NOW_MS, make_mixed_snapshot

AIRCRAFT_ID = 1
GROUND_ID = 11
SELECTION = (AIRCRAFT_ID, GROUND_ID)


class _OfflineTransport:
    """Reaching this transport proves grounding accepted the state it was given."""

    def __init__(self) -> None:
        self.requests = 0

    def complete(self, request: object) -> ModelResponse:
        self.requests += 1
        raise TransportError("offline")


def _drone(
    drone_id: int,
    *,
    device_class: str,
    state: str,
    x: float,
    y: float,
    z: float,
    capabilities: list[str],
) -> dict[str, object]:
    """One device shaped like the relay's state projection, which names the class for both."""
    return {
        "drone_id": drone_id,
        "device_class": device_class,
        "unit": 1,
        "membership": "ready",
        "selectable": True,
        "flight_state": state,
        "camera_patterns": [],
        "adapter_capabilities": capabilities,
        "telemetry": {"x": x, "y": y, "z": z, "t": NOW_MS},
        "home_pose": {"x": x, "y": y, "z": 0.0},
    }


def _mixed_relay_state(**changes: object) -> dict[str, object]:
    """An aircraft hovering and a ground vehicle idling in one authoritative state event."""
    return {
        "v": 1,
        "type": "state",
        "mode": "indoor",
        "session": "mixed-session",
        "event_id": "state-mixed",
        "t": NOW_MS,
        "roster_version": 7,
        "armed": True,
        "estop": False,
        "selection": list(SELECTION),
        "drones": [
            _drone(
                AIRCRAFT_ID,
                device_class="aircraft",
                state="hovering",
                x=0.0,
                y=0.0,
                z=1.0,
                capabilities=["flight", "pano_360"],
            ),
            _drone(
                GROUND_ID,
                device_class="ground_vehicle",
                state="idle",
                x=0.0,
                y=6.0,
                z=0.0,
                capabilities=["ground_drive", "lidar"],
            ),
        ],
        **changes,
    }


def _facts(state: dict[str, object]):
    return build_grounding_facts(state, capability_version="mixed-v1")


def test_grounding_facts_carry_a_ground_vehicles_drive_state() -> None:
    facts = _facts(_mixed_relay_state())

    states = {int(drone["drone_id"]): drone["flight_state"] for drone in facts.drones}
    assert states == {AIRCRAFT_ID: "hovering", GROUND_ID: "idle"}
    ground = next(drone for drone in facts.drones if drone["drone_id"] == GROUND_ID)
    assert ground["flight_available"] is False, "a robot has no flight capability to offer"


@pytest.mark.parametrize(
    ("index", "state"),
    [(0, "idle"), (0, "docked"), (1, "hovering"), (1, "landed")],
)
def test_a_telemetry_state_outside_its_class_vocabulary_is_refused(index: int, state: str) -> None:
    """The two vocabularies do not cross: neither class may report the other's state."""
    relay_state = _mixed_relay_state()
    drones = [dict(drone) for drone in relay_state["drones"]]
    drones[index]["flight_state"] = state
    relay_state["drones"] = drones

    with pytest.raises(ValueError):
        _facts(relay_state)


def test_an_unnamed_device_class_is_refused() -> None:
    """A class this build does not know fails closed rather than defaulting to aircraft."""
    relay_state = _mixed_relay_state()
    drones = [dict(drone) for drone in relay_state["drones"]]
    drones[1]["device_class"] = "submarine"
    relay_state["drones"] = drones

    with pytest.raises(ValueError):
        _facts(relay_state)


def test_a_device_without_a_class_is_grounded_as_an_aircraft() -> None:
    """An older relay names no class, and its devices are aircraft with flight states."""
    relay_state = _mixed_relay_state()
    aircraft = dict(relay_state["drones"][0])
    del aircraft["device_class"]
    relay_state["drones"] = [aircraft]
    relay_state["selection"] = [AIRCRAFT_ID]

    facts = _facts(relay_state)

    assert facts.drones[0]["flight_state"] == "hovering"


def test_a_mixed_session_reaches_the_model_instead_of_refusing_as_stale_state() -> None:
    """A ground vehicle in the roster must not refuse every transcript in the session.

    Grounding runs before the provider call, so a state the facts reject is reported as
    ``stale_state`` for aircraft-only work too. Reaching the transport is the evidence
    that the whole session is no longer refused by one robot's drive state.
    """
    transport = _OfflineTransport()

    outcome, plan = TranscriptCompiler(transport, audit=InMemoryAuditSink()).compile(
        "Hold.",
        _mixed_relay_state(),
        capability_version="mixed-v1",
        now_ms=NOW_MS,
    )

    assert transport.requests == 1
    assert outcome.kind is OutcomeKind.REFUSE
    assert outcome.reason is CompilerReason.MODEL_UNAVAILABLE
    assert outcome.reason is not CompilerReason.STALE_STATE
    assert plan is None


def test_the_snapshot_consistency_check_reads_a_ground_vehicles_drive_state() -> None:
    """The compiler compares each device's telemetry state to the facts in its own vocabulary."""
    snapshot = make_mixed_snapshot(
        aircraft_ids=(AIRCRAFT_ID,), ground_ids=(GROUND_ID,), selection=SELECTION
    )
    facts = _facts(_mixed_relay_state())

    assert snapshot.aircraft[GROUND_ID].telemetry_state == "idle"
    assert _snapshot_matches_facts(snapshot, facts, SELECTION)


def test_a_ground_vehicle_that_has_started_moving_does_not_match_its_facts() -> None:
    """A stale drive state is caught the same way a stale flight state is."""
    snapshot = make_mixed_snapshot(
        aircraft_ids=(AIRCRAFT_ID,), ground_ids=(GROUND_ID,), selection=SELECTION
    )
    relay_state = _mixed_relay_state()
    drones = [dict(drone) for drone in relay_state["drones"]]
    drones[1]["flight_state"] = "moving"
    relay_state["drones"] = drones

    assert not _snapshot_matches_facts(snapshot, _facts(relay_state), SELECTION)
