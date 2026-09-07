import copy
import json
from dataclasses import replace

import pytest

from spatial.contracts import FrameDeclaration, FrameKind, NodeType, ObservationError
from spatial.observations import (
    MAX_OBSERVATION_BYTES,
    LidarScanPayload,
    Observation,
    ObservationSource,
    ObservationSubmission,
    bounded_json,
    source_registry,
)
from spatial.schema import SCHEMA_PATH, schema
from spatial.vectors import FIXTURE_PATH, vectors


def pose():
    return copy.deepcopy(vectors()["cases"][0]["submission"])


def test_generated_vectors_match_committed_json_and_roundtrip():
    assert json.loads(FIXTURE_PATH.read_text()) == vectors()
    assert json.loads(SCHEMA_PATH.read_text()) == schema()
    for case in vectors()["cases"]:
        event = case["accepted"]
        assert Observation.parse(event).to_dict() == event
        assert len(bounded_json(event)) < MAX_OBSERVATION_BYTES
        assert event["authority"] == "diagnostic"
        if case["submission"] is not None:
            assert ObservationSubmission.parse(case["submission"]).t_capture == 1756700000000
            assert event["t_ingest"] - event["t_capture"] == 75
        else:
            assert event["t_capture"] is None
            assert event["payload"]["capture_time_available"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"authority": "diagnostic"},
        {"t_ingest": 1},
        {"frame_provenance": {}},
        {"v": True},
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": float("inf")},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": 1 << 10000},
        {"t_capture": None},
        {"t_capture": True},
        {"t_capture": 1756700000051},
        {"t": -1},
        {"t": 2**53},
        {"node_type": "robot"},
        {"event_id": "\ud800"},
        {"frame": " world"},
        {"source_id": "x" * 129},
        {"session": "x" * 513},
        {"drone_id": True},
        {"connection_epoch": 0},
    ],
)
def test_invalid_submission_fails_with_typed_error(changes):
    with pytest.raises(ObservationError):
        ObservationSubmission.parse(pose() | changes)


def test_pose_frame_cannot_be_changed_by_envelope_or_constructor():
    with pytest.raises(ObservationError, match="frames differ"):
        ObservationSubmission.parse(pose() | {"frame": "map_enu"})
    with pytest.raises(ObservationError, match="frames differ"):
        replace(ObservationSubmission.parse(pose()), frame="building")


def test_local_frame_origin_changes_on_rejoin_even_when_frame_id_is_reused():
    first = Observation.parse(vectors()["cases"][1]["accepted"])
    second = Observation(
        replace(first.submission, connection_epoch=3), first.t_ingest, first.declaration
    )
    assert first.submission.payload.position.frame == second.submission.payload.position.frame
    assert first.to_dict()["frame_provenance"] != second.to_dict()["frame_provenance"]
    # Position alone omits the enclosing session/epoch. It deliberately exposes
    # no arithmetic helper that could mistake those different origins as equal.
    with pytest.raises(AttributeError):
        first.submission.payload.position.distance_to(second.submission.payload.position)


@pytest.mark.parametrize(
    "change",
    [
        {"ranges_mm": []},
        {"ranges_mm": [None] * 721},
        {"ranges_mm": [True]},
        {"ranges_mm": [0]},
        {"ranges_mm": [12001]},
        {"ranges_mm": [float("nan")]},
        {"angle_increment_mdeg": 0},
        {"angle_increment_mdeg": 360000},
        {"range_min_mm": 12001},
        {"range_max_mm": 655351},
    ],
)
def test_lidar_bounds_and_unknown_returns_are_explicit(change):
    scan = copy.deepcopy(vectors()["cases"][2]["submission"])
    scan["payload"].update(change)
    with pytest.raises(ObservationError):
        ObservationSubmission.parse(scan)


def test_direct_payload_constructor_obeys_decoder_bounds():
    with pytest.raises(ObservationError):
        LidarScanPayload(0, 1, 1, 10, (11,))


def test_legacy_payload_cannot_be_submitted_or_promoted():
    legacy = vectors()["cases"][-1]["accepted"]
    submission = {
        key: value
        for key, value in legacy.items()
        if key not in {"authority", "t_ingest", "frame_provenance"}
    }
    with pytest.raises(ObservationError, match="not supported"):
        ObservationSubmission.parse(submission)
    for changes in (
        {"t_capture": legacy["t"]},
        {"confidence": 0.9},
        {"node_type": "ground_vehicle"},
    ):
        with pytest.raises(ObservationError):
            ObservationSubmission.parse(submission | changes, allow_legacy=True)
    with pytest.raises(ObservationError):
        Observation.parse(legacy | {"authority": "control"})


def test_frame_provenance_is_exact_and_does_not_declare_transforms():
    event = vectors()["cases"][2]["accepted"]
    for changes in (
        {"origin_connection_epoch": 1},
        {"origin_drone_id": 12},
        {"transform_id": "claimed-approved-transform"},
        {"axes": "raw_sdk_axes"},
    ):
        with pytest.raises(ObservationError):
            Observation.parse(event | {"frame_provenance": event["frame_provenance"] | changes})
    for frame, kind in (
        ("world", FrameKind.MAP),
        ("building", FrameKind.WORLD),
        ("map_enu", FrameKind.DEVICE_BODY),
    ):
        with pytest.raises(ObservationError):
            FrameDeclaration(frame, kind)


def test_source_registry_binds_local_frames_to_one_device():
    source = ObservationSource(
        "lidar-1",
        "adapter",
        1,
        NodeType.AIRCRAFT,
        (FrameDeclaration("device:1:body", FrameKind.DEVICE_BODY),),
        ("lidar_scan",),
    )
    with pytest.raises(ObservationError, match="multiple owners"):
        source_registry(
            {"lidar-1": source, "lidar-2": replace(source, source_id="lidar-2", drone_id=2)}
        )
    with pytest.raises(TypeError):
        source_registry({"lidar-1": source})["new"] = source


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "legacy.aircraft.1.telemetry"},
        {"principal_source": "console"},
        {"principal_source": []},
        {"payload_types": (["pose"],)},
        {"frames": ()},
        {"payload_types": ("pose", "pose")},
    ],
)
def test_source_configuration_is_strict(changes):
    source = ObservationSource(
        "pose-1",
        "localization",
        1,
        NodeType.AIRCRAFT,
        (FrameDeclaration("world", FrameKind.WORLD),),
        ("pose",),
    )
    with pytest.raises(ObservationError):
        replace(source, **changes)
