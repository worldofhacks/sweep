import pytest

from relay.capabilities import (
    C1_IMPLEMENTED_INTENT_NAMES,
    C2_CAPABILITY_PROFILE,
    IMPLEMENTED_INTENT_NAMES,
    CapabilityProfile,
)
from relay.intent_v1 import (
    MAX_INTENT_DRONE_ID,
    MAX_INTENT_DRONE_IDS,
    MAX_INTENT_IDENTIFIER_CHARS,
    MAX_INTENT_NAME_CHARS,
    MAX_INTENT_SESSION_CHARS,
    MAX_INTENT_SOURCE_CHARS,
    MAX_INTENT_TIMESTAMP,
    REGISTERED_SOURCES,
    SOURCE_ALLOWED_NAMES,
    AcceptedIntent,
    IntentName,
    Mode,
    RejectedIntent,
    RejectionReason,
    validate_intent,
)


@pytest.fixture
def console_select_payload() -> dict[str, object]:
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "intent",
        "intent_id": "01J7FQ9M6A7Z3T2R8C4N5K1P0B",
        "source": "console",
        "session": "2026-09-02T09-00-00Z",
        "name": "select",
        "args": {"ids": [1, 2]},
        "selection": [],
        "mode": "indoor",
        "confirm": False,
    }


def test_console_select_payload_is_validated(
    console_select_payload: dict[str, object],
) -> None:
    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name is IntentName.SELECT
    assert result.intent.args == {"ids": (1, 2)}
    assert result.intent.selection == ()
    assert result.intent.mode is Mode.INDOOR


def test_validated_intent_is_detached_from_input(
    console_select_payload: dict[str, object],
) -> None:
    result = validate_intent(console_select_payload)
    args = console_select_payload["args"]
    assert isinstance(args, dict)
    ids = args["ids"]
    assert isinstance(ids, list)

    ids.append(3)
    console_select_payload["selection"] = [3]

    assert isinstance(result, AcceptedIntent)
    assert result.intent.args == {"ids": (1, 2)}
    assert result.intent.selection == ()


@pytest.mark.parametrize(
    ("source", "name", "args", "selection"),
    [
        ("console", "select", {"ids": [1, 2]}, []),
        ("keyboard", "estop", {}, []),
        ("webcam", "hold", {}, [1]),
    ],
)
def test_registered_sources_share_the_validator(
    console_select_payload: dict[str, object],
    source: str,
    name: str,
    args: dict[str, object],
    selection: list[int],
) -> None:
    console_select_payload.update(source=source, name=name, args=args, selection=selection)

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.source == source
    assert result.intent.name.value == name


@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        ("arm", {}, False),
        ("takeoff", {}, True),
        ("translate", {"dx": 2, "dy": -1}, False),
        ("hold", {}, False),
        ("come_home", {}, False),
        ("land", {}, True),
        ("land_all", {}, True),
        ("estop", {}, False),
    ],
)
def test_basic_c1_console_intents_match_the_planner_contract(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    confirm: bool,
) -> None:
    console_select_payload.update(name=name, args=args, selection=[1, 2], confirm=confirm)

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name.value == name
    assert result.intent.args == args
    assert result.intent.selection == (1, 2)


@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        ("altitude", {"delta": 1}, False),
        ("formation_next", {}, False),
        ("formation_set", {"name": "circle"}, False),
        ("spacing", {"delta": 1}, False),
        ("sweep", {}, True),
        (
            "sweep",
            {"box": {"min_x": -2, "max_x": 2, "min_y": -3, "max_y": 3}},
            True,
        ),
    ],
)
def test_m15_sim_intents_are_accepted_on_the_indoor_contract(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    confirm: bool,
) -> None:
    console_select_payload.update(name=name, args=args, selection=[1, 2], confirm=confirm)

    result = validate_intent(console_select_payload, capability_profile=C2_CAPABILITY_PROFILE)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name.value == name


def test_sweep_requires_confirmation_before_planning(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(name="sweep", args={}, selection=[1, 2], confirm=False)

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_search_requires_confirmed_selected_aircraft_and_an_enabled_profile(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(
        name="search",
        args={"zone_id": "atrium", "target_class": "backpack"},
        selection=[1],
        confirm=True,
    )

    disabled = validate_intent(console_select_payload)
    enabled = validate_intent(
        console_select_payload,
        capability_profile=CapabilityProfile("search-enabled", frozenset({IntentName.SEARCH})),
    )

    assert isinstance(disabled, RejectedIntent)
    assert disabled.reason is RejectionReason.UNSUPPORTED
    assert isinstance(enabled, AcceptedIntent)
    assert enabled.intent.args == {"zone_id": "atrium", "target_class": "backpack"}


@pytest.mark.parametrize(
    "box",
    [
        {"x": 0, "y": 0, "width": 4, "height": 3},
        {"min_x": 0, "max_x": 0, "min_y": -1, "max_y": 1},
        {"min_x": 1, "max_x": 0, "min_y": -1, "max_y": 1},
        {"min_x": 0, "max_x": 1, "min_y": -1, "max_y": float("inf")},
        {"min_x": False, "max_x": 1, "min_y": -1, "max_y": 1},
    ],
)
def test_sweep_rejects_ambiguous_or_invalid_boxes(
    console_select_payload: dict[str, object], box: dict[str, object]
) -> None:
    console_select_payload.update(name="sweep", args={"box": box}, selection=[1], confirm=True)

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_translate_rejects_source_owned_frame_and_step(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(
        name="translate",
        args={"dx": 1, "dy": 0, "frame": "world", "step_m": 9.0},
        selection=[1],
    )

    assert isinstance(validate_intent(console_select_payload), RejectedIntent)


@pytest.mark.parametrize("mode", ["outdoorC", "outdoorF"])
@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        ("takeoff", {}, True),
        ("translate", {"dx": 2, "dy": -1}, False),
        (
            "capture_room",
            {"room_id": "room-1", "capture_id": "capture-1", "pattern": "pano_360"},
            True,
        ),
    ],
)
def test_outdoor_modes_are_reserved_until_future_capability(
    console_select_payload: dict[str, object],
    mode: str,
    name: str,
    args: dict[str, object],
    confirm: bool,
) -> None:
    console_select_payload.update(
        name=name,
        args=args,
        selection=[1],
        mode=mode,
        confirm=confirm,
    )

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNSUPPORTED


def test_intent_id_and_retry_of_pass_through(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["retry_of"] = "01J7FQ9M6A7Z3T2R8C4N5K1P0A"

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.intent_id == "01J7FQ9M6A7Z3T2R8C4N5K1P0B"
    assert result.intent.retry_of == "01J7FQ9M6A7Z3T2R8C4N5K1P0A"


def test_intent_envelope_accepts_exact_text_integer_and_fleet_boundaries(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(
        t=MAX_INTENT_TIMESTAMP,
        intent_id="🚁" * MAX_INTENT_IDENTIFIER_CHARS,
        retry_of="r" * MAX_INTENT_IDENTIFIER_CHARS,
        session="🚁" * MAX_INTENT_SESSION_CHARS,
        args={"ids": list(range(1, MAX_INTENT_DRONE_IDS + 1))},
        selection=list(range(1, MAX_INTENT_DRONE_IDS + 1)),
    )

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.t == MAX_INTENT_TIMESTAMP
    assert len(result.intent.selection) == MAX_INTENT_DRONE_IDS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("t", MAX_INTENT_TIMESTAMP + 1),
        ("intent_id", "i" * (MAX_INTENT_IDENTIFIER_CHARS + 1)),
        ("retry_of", "r" * (MAX_INTENT_IDENTIFIER_CHARS + 1)),
        ("session", "s" * (MAX_INTENT_SESSION_CHARS + 1)),
        ("source", "s" * (MAX_INTENT_SOURCE_CHARS + 1)),
        ("name", "n" * (MAX_INTENT_NAME_CHARS + 1)),
        ("selection", list(range(1, MAX_INTENT_DRONE_IDS + 2))),
        ("selection", [MAX_INTENT_DRONE_ID + 1]),
    ],
)
def test_intent_envelope_rejects_values_above_each_public_bound(
    console_select_payload: dict[str, object], field: str, value: object
) -> None:
    console_select_payload[field] = value

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_source_and_name_exact_text_ceilings_reach_semantic_validation(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["source"] = "s" * MAX_INTENT_SOURCE_CHARS
    source_result = validate_intent(console_select_payload)
    assert isinstance(source_result, RejectedIntent)
    assert source_result.reason is RejectionReason.UNKNOWN_SOURCE

    console_select_payload["source"] = "console"
    console_select_payload["name"] = "n" * MAX_INTENT_NAME_CHARS
    name_result = validate_intent(console_select_payload)
    assert isinstance(name_result, RejectedIntent)
    assert name_result.reason is RejectionReason.UNKNOWN_INTENT


@pytest.mark.parametrize("value", [" padded", "padded ", "zero\u200bwidth", "line\nbreak"])
def test_intent_text_fields_require_canonical_printable_values(
    console_select_payload: dict[str, object], value: str
) -> None:
    console_select_payload["intent_id"] = value

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_select_ids_apply_the_same_fleet_and_int32_boundaries(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["args"] = {"ids": list(range(1, MAX_INTENT_DRONE_IDS + 2))}
    assert isinstance(validate_intent(console_select_payload), RejectedIntent)

    console_select_payload["args"] = {"ids": [MAX_INTENT_DRONE_ID + 1]}
    assert isinstance(validate_intent(console_select_payload), RejectedIntent)


@pytest.mark.parametrize(
    ("name", "args", "selection", "confirm", "exact_reason"),
    [
        (
            "formation_set",
            {"name": "f" * MAX_INTENT_IDENTIFIER_CHARS},
            [1],
            False,
            None,
        ),
        (
            "capture_room",
            {
                "room_id": "r" * MAX_INTENT_IDENTIFIER_CHARS,
                "capture_id": "c" * MAX_INTENT_IDENTIFIER_CHARS,
                "pattern": "pano_360",
            },
            [1],
            True,
            None,
        ),
        (
            "survey_area",
            {"area_id": "a" * MAX_INTENT_IDENTIFIER_CHARS},
            [1],
            True,
            RejectionReason.UNSUPPORTED,
        ),
        (
            "map_area",
            {"area_id": "a" * MAX_INTENT_IDENTIFIER_CHARS},
            [1],
            True,
            RejectionReason.UNSUPPORTED,
        ),
    ],
)
def test_intent_argument_identifiers_accept_the_exact_shared_ceiling(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    selection: list[int],
    confirm: bool,
    exact_reason: RejectionReason | None,
) -> None:
    console_select_payload.update(name=name, args=args, selection=selection, confirm=confirm)

    result = validate_intent(console_select_payload, capability_profile=C2_CAPABILITY_PROFILE)

    if exact_reason is None:
        assert isinstance(result, AcceptedIntent)
    else:
        assert isinstance(result, RejectedIntent)
        assert result.reason is exact_reason


@pytest.mark.parametrize(
    ("name", "args", "selection", "confirm"),
    [
        ("formation_set", {"name": "f" * (MAX_INTENT_IDENTIFIER_CHARS + 1)}, [1], False),
        (
            "capture_room",
            {
                "room_id": "r" * (MAX_INTENT_IDENTIFIER_CHARS + 1),
                "capture_id": "capture",
                "pattern": "pano_360",
            },
            [1],
            True,
        ),
        (
            "capture_room",
            {
                "room_id": "room",
                "capture_id": "c" * (MAX_INTENT_IDENTIFIER_CHARS + 1),
                "pattern": "pano_360",
            },
            [1],
            True,
        ),
        ("survey_area", {"area_id": "a" * (MAX_INTENT_IDENTIFIER_CHARS + 1)}, [1], True),
        ("map_area", {"area_id": "a" * (MAX_INTENT_IDENTIFIER_CHARS + 1)}, [1], True),
    ],
)
def test_intent_argument_identifiers_reject_values_above_the_shared_ceiling(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    selection: list[int],
    confirm: bool,
) -> None:
    console_select_payload.update(name=name, args=args, selection=selection, confirm=confirm)

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent_id", ""),
        ("intent_id", 1),
        ("retry_of", ""),
        ("retry_of", 1),
    ],
)
def test_invalid_intent_id_or_retry_of_is_rejected(
    console_select_payload: dict[str, object], field: str, value: object
) -> None:
    console_select_payload[field] = value

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize("source", ["band", "Webcam"])
def test_unregistered_source_is_rejected(
    console_select_payload: dict[str, object], source: str
) -> None:
    console_select_payload["source"] = source

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNKNOWN_SOURCE


def test_invalid_motion_args_are_rejected(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(name="translate", args={"dx": 1})

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_schema_version_requires_integer_one(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["v"] = 1.0

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("t", True),
        ("selection", [1, 1]),
        ("mode", "outdoor"),
    ],
)
def test_invalid_envelope_values_are_rejected(
    console_select_payload: dict[str, object], field: str, value: object
) -> None:
    console_select_payload[field] = value

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_extra_top_level_field_is_rejected(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["sequence"] = 1

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_translate_rejects_non_numeric_or_non_finite_steps(
    console_select_payload: dict[str, object], value: object
) -> None:
    console_select_payload.update(name="translate", args={"dx": value, "dy": 0})

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_unknown_intent_name_is_rejected(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(name="flip", args={})

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNKNOWN_INTENT


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("disarm", {}),
        ("survey_area", {"area_id": "floor-1"}),
        ("map_area", {"area_id": "floor-1"}),
    ],
)
def test_valid_intents_outside_c1_are_unsupported(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
) -> None:
    console_select_payload.update(name=name, args=args, selection=[1], confirm=True)

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNSUPPORTED


def test_malformed_gated_intent_is_invalid_before_capability_check(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(name="altitude", args={"metres": 1})

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_deeply_nested_sweep_box_is_a_typed_rejection(
    console_select_payload: dict[str, object],
) -> None:
    box: dict[str, object] = {}
    cursor = box
    for _ in range(1_100):
        nested: dict[str, object] = {}
        cursor["nested"] = nested
        cursor = nested
    console_select_payload.update(name="sweep", args={"box": box})

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_capture_room_rejects_unknown_pattern(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload.update(
        name="capture_room",
        args={"room_id": "room-1", "capture_id": "capture-1", "pattern": "wide"},
        selection=[1],
        confirm=True,
    )

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_capture_room_is_supported_at_one_confirmed_hover() -> None:
    result = validate_intent(
        {
            "v": 1,
            "t": 1_756_700_000_000,
            "type": "intent",
            "intent_id": "01J7FQ9M6A7Z3T2R8C4N5K1P0C",
            "retry_of": None,
            "source": "console",
            "session": "2026-09-02T09-00-00Z",
            "name": "capture_room",
            "args": {
                "room_id": "room-1",
                "capture_id": "capture-1",
                "pattern": "reconstruct_8",
            },
            "selection": [1],
            "mode": "indoor",
            "confirm": True,
        }
    )

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name is IntentName.CAPTURE_ROOM
    assert result.intent.selection == (1,)


@pytest.mark.parametrize(
    ("selection", "confirm"),
    [([], True), ([1, 2], True), ([1], False)],
)
def test_capture_room_requires_one_selected_drone_and_confirmation(
    console_select_payload: dict[str, object],
    selection: list[int],
    confirm: bool,
) -> None:
    console_select_payload.update(
        name="capture_room",
        args={"room_id": "room-1", "capture_id": "capture-1", "pattern": "pano_360"},
        selection=selection,
        confirm=confirm,
    )

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize(
    ("name", "selection", "confirm"),
    [
        ("survey_area", [1], False),
        ("map_area", [], True),
        ("map_area", [1], False),
    ],
)
def test_area_workflows_enforce_confirmation_and_map_selection(
    console_select_payload: dict[str, object],
    name: str,
    selection: list[int],
    confirm: bool,
) -> None:
    console_select_payload.update(
        name=name,
        args={"area_id": "floor-1"},
        selection=selection,
        confirm=confirm,
    )

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_initial_request_may_explicitly_set_null_retry(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["retry_of"] = None

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.retry_of is None


def test_retry_cannot_reference_its_own_intent_id(
    console_select_payload: dict[str, object],
) -> None:
    console_select_payload["retry_of"] = console_select_payload["intent_id"]

    result = validate_intent(console_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


_C1_ARGS: dict[IntentName, dict[str, object]] = {
    IntentName.SELECT: {"ids": [1]},
    IntentName.TRANSLATE: {"dx": 1, "dy": 0},
    IntentName.ALTITUDE: {"delta": 1},
    IntentName.FORMATION_SET: {"name": "line"},
    IntentName.SPACING: {"delta": 1},
    IntentName.CAPTURE_ROOM: {
        "room_id": "room-1",
        "capture_id": "capture-1",
        "pattern": "pano_360",
    },
    IntentName.SURVEY_AREA: {"area_id": "floor-1"},
}
_CONFIRMED_NAMES = frozenset(
    {
        IntentName.TAKEOFF,
        IntentName.LAND,
        IntentName.LAND_ALL,
        IntentName.CAPTURE_ROOM,
        IntentName.SWEEP,
        IntentName.SURVEY_AREA,
    }
)


def _c1_payload(source: str, name: IntentName) -> dict[str, object]:
    """Return a well-formed indoor request for one registered intent name."""
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "intent",
        "intent_id": f"intent-{source}-{name.value}",
        "retry_of": None,
        "source": source,
        "session": "session-test",
        "name": name.value,
        "args": _C1_ARGS.get(name, {}),
        "selection": [1],
        "mode": "indoor",
        "confirm": name in _CONFIRMED_NAMES,
    }


def test_source_allowlist_covers_every_registered_source() -> None:
    assert set(SOURCE_ALLOWED_NAMES) == REGISTERED_SOURCES
    assert all(names <= IMPLEMENTED_INTENT_NAMES for names in SOURCE_ALLOWED_NAMES.values())
    assert SOURCE_ALLOWED_NAMES["console"] is IMPLEMENTED_INTENT_NAMES
    assert SOURCE_ALLOWED_NAMES["keyboard"] == {IntentName.ESTOP}
    assert SOURCE_ALLOWED_NAMES["webcam"] == {IntentName.CAPTURE_ROOM, IntentName.HOLD}


@pytest.mark.parametrize("name", sorted(C1_IMPLEMENTED_INTENT_NAMES))
def test_console_may_emit_every_c1_name(name: IntentName) -> None:
    result = validate_intent(_c1_payload("console", name))

    assert isinstance(result, AcceptedIntent)
    assert result.intent.source == "console"
    assert result.intent.name is name


def test_keyboard_network_stop_passes_validation() -> None:
    result = validate_intent(_c1_payload("keyboard", IntentName.ESTOP))

    assert isinstance(result, AcceptedIntent)
    assert result.intent.source == "keyboard"
    assert result.intent.name is IntentName.ESTOP


@pytest.mark.parametrize("name", sorted(C1_IMPLEMENTED_INTENT_NAMES - {IntentName.ESTOP}))
def test_keyboard_may_only_emit_the_network_stop(name: IntentName) -> None:
    result = validate_intent(_c1_payload("keyboard", name))

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.SOURCE_NOT_ALLOWED
    assert result.detail == f"{name.value} is not allowed from source keyboard"


@pytest.mark.parametrize("name", [IntentName.HOLD, IntentName.CAPTURE_ROOM])
def test_webcam_gesture_names_pass_validation(name: IntentName) -> None:
    result = validate_intent(_c1_payload("webcam", name))

    assert isinstance(result, AcceptedIntent)
    assert result.intent.source == "webcam"
    assert result.intent.name is name


@pytest.mark.parametrize(
    "name",
    sorted(C1_IMPLEMENTED_INTENT_NAMES - {IntentName.HOLD, IntentName.CAPTURE_ROOM}),
)
def test_webcam_never_gesture_emittable_names_are_refused(name: IntentName) -> None:
    result = validate_intent(_c1_payload("webcam", name))

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.SOURCE_NOT_ALLOWED
    assert result.detail == f"{name.value} is not allowed from source webcam"


def test_source_allowlist_is_checked_after_shape_and_capability() -> None:
    outside_profile = _c1_payload("webcam", IntentName.SURVEY_AREA)
    outdoor = _c1_payload("webcam", IntentName.TAKEOFF)
    outdoor["mode"] = "outdoorC"
    malformed = _c1_payload("webcam", IntentName.TAKEOFF)
    malformed["args"] = {"z": 1}

    for payload, reason in (
        (outside_profile, RejectionReason.UNSUPPORTED),
        (outdoor, RejectionReason.UNSUPPORTED),
        (malformed, RejectionReason.INVALID_PAYLOAD),
    ):
        result = validate_intent(payload)

        assert isinstance(result, RejectedIntent)
        assert result.reason is reason
