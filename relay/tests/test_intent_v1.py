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
def webcam_select_payload() -> dict[str, object]:
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "intent",
        "intent_id": "01J7FQ9M6A7Z3T2R8C4N5K1P0B",
        "retry_of": None,
        "source": "webcam",
        "session": "2026-09-02T09-00-00Z",
        "name": "select",
        "args": {"ids": [1, 2]},
        "selection": [],
        "mode": "indoor",
        "confirm": False,
    }


def test_webcam_select_payload_is_validated(
    webcam_select_payload: dict[str, object],
) -> None:
    result = validate_intent(webcam_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name is IntentName.SELECT
    assert result.intent.args == {"ids": (1, 2)}
    assert result.intent.selection == ()
    assert result.intent.mode is Mode.INDOOR


def test_validated_intent_is_detached_from_input(
    webcam_select_payload: dict[str, object],
) -> None:
    result = validate_intent(webcam_select_payload)
    args = webcam_select_payload["args"]
    assert isinstance(args, dict)
    ids = args["ids"]
    assert isinstance(ids, list)

    ids.append(3)
    webcam_select_payload["selection"] = [3]

    assert isinstance(result, AcceptedIntent)
    assert result.intent.args == {"ids": (1, 2)}
    assert result.intent.selection == ()


@pytest.mark.parametrize("source", ["webcam", "language", "keyboard"])
def test_registered_sources_share_the_validator(
    webcam_select_payload: dict[str, object], source: str
) -> None:
    webcam_select_payload["source"] = source

    result = validate_intent(webcam_select_payload)

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
def test_m20_webcam_intents_match_the_planner_contract(
    webcam_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
    confirm: bool,
) -> None:
    webcam_select_payload.update(name=name, args=args, selection=[1, 2], confirm=confirm)

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, AcceptedIntent)
    assert result.intent.name.value == name
    assert result.intent.args == args
    assert result.intent.selection == (1, 2)


def test_intent_id_and_retry_of_pass_through(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload["retry_of"] = "01J7FQ9M6A7Z3T2R8C4N5K1P0A"

    result = validate_intent(webcam_select_payload)

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
    webcam_select_payload: dict[str, object], field: str, value: object
) -> None:
    webcam_select_payload[field] = value

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_unregistered_webcam_source_is_rejected(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload["source"] = "glasses"

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNKNOWN_SOURCE


def test_invalid_motion_args_are_rejected(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload.update(name="translate", args={"dx": 1})

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_schema_version_requires_integer_one(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload["v"] = 1.0

    result = validate_intent(webcam_select_payload)

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
    webcam_select_payload: dict[str, object], field: str, value: object
) -> None:
    webcam_select_payload[field] = value

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_extra_top_level_field_is_rejected(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload["sequence"] = 1

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_translate_rejects_non_numeric_or_non_finite_steps(
    webcam_select_payload: dict[str, object], value: object
) -> None:
    webcam_select_payload.update(name="translate", args={"dx": value, "dy": 0})

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_unknown_intent_name_is_rejected(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload.update(name="flip", args={})

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNKNOWN_INTENT


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("disarm", {}),
        ("land", {}),
        ("altitude", {"delta": 1}),
        ("formation_next", {}),
        ("formation_set", {"name": "line"}),
        ("spacing", {"delta": -1}),
        ("sweep", {"box": {"x": 0, "y": 0, "width": 4, "height": 3}}),
        ("survey_area", {"area_id": "floor-1"}),
        ("map_area", {"area_id": "floor-1"}),
        (
            "capture_room",
            {"room_id": "room-1", "capture_id": "capture-1", "pattern": "pano_360"},
        ),
    ],
)
def test_valid_intents_outside_m20_are_unsupported(
    webcam_select_payload: dict[str, object],
    name: str,
    args: dict[str, object],
) -> None:
    webcam_select_payload.update(name=name, args=args)

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.UNSUPPORTED


def test_malformed_gated_intent_is_invalid_before_capability_check(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload.update(name="altitude", args={"metres": 1})

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD


def test_capture_room_rejects_unknown_pattern(
    webcam_select_payload: dict[str, object],
) -> None:
    webcam_select_payload.update(
        name="capture_room",
        args={"room_id": "room-1", "capture_id": "capture-1", "pattern": "wide"},
    )

    result = validate_intent(webcam_select_payload)

    assert isinstance(result, RejectedIntent)
    assert result.reason is RejectionReason.INVALID_PAYLOAD
