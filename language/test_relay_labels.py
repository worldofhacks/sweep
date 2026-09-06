"""Relay compiler labels and nouns follow each device's class and unit."""

from __future__ import annotations

from collections.abc import Mapping

from language.contracts import CompilerReason, ProposedIntent
from language.relay_compiler import _clarify_options, _label, _labels, _step_notes
from relay.intent_v1 import IntentName, Mode


def _drone(drone_id: int, device_class: str, unit: int, state: str) -> dict[str, object]:
    return {
        "drone_id": drone_id,
        "device_class": device_class,
        "unit": unit,
        "membership": "ready",
        "selectable": True,
        "flight_state": state,
    }


STATE: Mapping[str, object] = {
    "armed": True,
    "selection": [3, 11],
    "drones": [
        _drone(3, "aircraft", 1, "landed"),
        _drone(7, "aircraft", 2, "hovering"),
        _drone(11, "ground_vehicle", 1, "idle"),
        _drone(13, "ground_vehicle", 2, "moving"),
    ],
}
DRONES = {drone["drone_id"]: drone for drone in STATE["drones"]}  # type: ignore[index, union-attr]


def _intent(name: IntentName, selection: tuple[int, ...], **args: object) -> ProposedIntent:
    return ProposedIntent(name=name, args=args, selection=selection, mode=Mode.INDOOR)


def test_labels_number_devices_by_unit_within_their_class() -> None:
    assert _label(3, DRONES) == "D-01"
    assert _label(7, DRONES) == "D-02"
    assert _label(11, DRONES) == "G-01"
    assert _label(13, DRONES) == "G-02"
    # A device the state does not list is an aircraft numbered by its id, as the relay does.
    assert _label(4, DRONES) == "D-04"
    assert _label(4) == "D-04"
    assert _labels((3, 11, 13), DRONES) == "D-01, G-01, G-02"


def test_step_notes_use_class_aware_nouns() -> None:
    hold_ground = _step_notes(0, _intent(IntentName.HOLD, (11, 13)), STATE)
    assert hold_ground[0] == "Targets G-01, G-02 (an earlier step's selection)."
    assert hold_ground[1] == "Drive state when compiled: G-01 idle, G-02 moving."
    assert "Each robot stops and holds its current pose." in hold_ground

    hold_aircraft = _step_notes(0, _intent(IntentName.HOLD, (3, 7)), STATE)
    assert hold_aircraft[1] == "Flight state when compiled: D-01 landed, D-02 hovering."
    assert "Each aircraft hovers at its current pose." in hold_aircraft

    hold_mixed = _step_notes(0, _intent(IntentName.HOLD, (3, 11)), STATE)
    assert hold_mixed[0] == "Targets D-01, G-01 (the current selection)."
    assert hold_mixed[1] == "State when compiled: D-01 landed, G-01 idle."
    assert "Each aircraft hovers and each robot stops at its current pose." in hold_mixed

    come_home = _step_notes(0, _intent(IntentName.COME_HOME, (11,)), STATE)
    assert "Returns to each robot's captured home pose." in come_home
    estop = _step_notes(0, _intent(IntentName.ESTOP, ()), STATE)
    assert estop[0] == "Fleet-wide: targets D-01, D-02, G-01, G-02 from the roster."
    assert "Lands every ready airborne aircraft in place; robots are not affected." in _step_notes(
        0, _intent(IntentName.LAND_ALL, ()), STATE
    )


def test_clarify_options_offer_class_labels_and_a_class_aware_all() -> None:
    assert _clarify_options(CompilerReason.AMBIGUOUS_SELECTION, STATE, ()) == (
        "D-01",
        "D-02",
        "G-01",
        "G-02",
        "all devices",
    )
    ground_only = {
        "drones": [_drone(11, "ground_vehicle", 1, "idle"), _drone(13, "ground_vehicle", 2, "idle")]
    }
    assert _clarify_options(CompilerReason.AMBIGUOUS_SELECTION, ground_only, ())[-1] == "all robots"
    aircraft_only = {
        "drones": [_drone(1, "aircraft", 1, "landed"), _drone(2, "aircraft", 2, "landed")]
    }
    assert _clarify_options(CompilerReason.AMBIGUOUS_SELECTION, aircraft_only, ())[-1] == (
        "all aircraft"
    )
