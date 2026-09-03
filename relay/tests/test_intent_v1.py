import pytest

from relay.intent_v1 import (
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


@pytest.mark.parametrize("source", ["console", "keyboard"])
def test_registered_sources_share_the_validator(
    console_select_payload: dict[str, object], source: str
) -> None:
    console_select_payload["source"] = source

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.source == source


@pytest.mark.parametrize(
    ("name", "args", "confirm"),
    [
        ("arm", {}, False),
        ("takeoff", {}, True),
        ("translate", {"dx": 2, "dy": -1}, False),
        ("hold", {}, False),
        ("come_home", {}, False),
        ("land_all", {}, True),
        ("estop", {}, False),
    ],
)
def test_m20_console_intents_match_the_planner_contract(
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
    ],
)
def test_m15_sim_intents_are_accepted_on_the_indoor_contract(
    console_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    confirm: bool,
) -> None:
    console_select_payload.update(name=name, args=args, selection=[1, 2], confirm=confirm)

    result = validate_intent(console_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name.value == name


def test_sweep_requires_confirmation_before_planning() -> None:
    result = validate_intent(
        {
            "v": 1,
            "t": 1_756_700_000_000,
            "type": "intent",
            "intent_id": "sweep-unconfirmed",
            "source": "console",
            "session": "2026-09-02T09-00-00Z",
            "name": "sweep",
            "args": {},
            "selection": [1, 2],
            "mode": "indoor",
            "confirm": False,
        }
    )

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


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


@pytest.mark.parametrize("source", ["webcam", "language", "glasses"])
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
        ("land", {}),
        ("survey_area", {"area_id": "floor-1"}),
        ("map_area", {"area_id": "floor-1"}),
    ],
)
def test_valid_intents_outside_m15_are_unsupported(
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
