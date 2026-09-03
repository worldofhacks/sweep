import json
from dataclasses import replace

import pytest

from adapters.protocols import (
    CameraResultStatus,
    CaptureCoverage,
    CapturePattern,
)
from adapters.sim.camera import CameraFailureMode
from planner.models import CommandOperation, LifecycleStatus, Position, RefusalReason
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


def capture_intent(pattern: str, *, capture_id: str = "capture-a"):  # type: ignore[no-untyped-def]
    return make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room-a", "capture_id": capture_id, "pattern": pattern},
        confirm=True,
    )


def test_full_equirectangular_fixture_is_typed_and_deterministic() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    bundle = result.capture_bundle
    assert bundle is not None
    assert bundle.pattern is CapturePattern.PANO_360
    assert bundle.coverage is CaptureCoverage.FULL_EQUIRECTANGULAR
    assert bundle.room_id == "room-a"
    assert len(bundle.media) == 1
    media = bundle.media[0]
    assert media.intrinsics.projection == "equirectangular"
    assert media.intrinsics.width_px == media.intrinsics.height_px * 2
    assert media.intrinsics.horizontal_fov_deg == 360.0
    assert len(media.checksum_sha256) == 64
    assert json.loads(json.dumps(bundle.to_dict()))["status"] == "completed"
    assert flight.calls == []


def test_reconstruct_eight_fixture_preserves_headings_and_coverage_label() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, _ = make_stack(snapshot)

    result = controller.execute(
        capture_intent("reconstruct_8", capture_id="capture-eight"), snapshot
    )

    assert result.status is LifecycleStatus.COMPLETED
    bundle = result.capture_bundle
    assert bundle is not None
    assert bundle.pattern is CapturePattern.RECONSTRUCT_8
    assert bundle.coverage is CaptureCoverage.INCOMPLETE_VERTICAL
    assert len(bundle.media) == 8
    assert [media.actual_yaw_deg for media in bundle.media] == [
        float(value) for value in range(0, 360, 45)
    ]
    assert sum(call.operation is CommandOperation.ROTATE_TO for call in flight.calls) == 8


@pytest.mark.parametrize("pattern", ["pano_360", "reconstruct_8"])
def test_unsupported_camera_is_typed_before_capture(pattern: str) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)
    camera.inject_failure(1, CameraFailureMode.UNSUPPORTED)

    result = controller.execute(capture_intent(pattern), snapshot)

    assert result.status is LifecycleStatus.REFUSED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_UNSUPPORTED
    assert [call[0] for call in camera.calls] == ["capabilities"]
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]


def test_injected_camera_failure_holds_and_preserves_failed_bundle() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)
    camera.inject_failure(1, CameraFailureMode.CAMERA)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE
    assert result.capture_bundle is not None
    assert result.capture_bundle.status is CameraResultStatus.FAILED
    assert result.capture_bundle.capture_id == "capture-a"
    assert flight.calls[-1].operation is CommandOperation.HOVER


def test_injected_download_failure_holds_and_returns_typed_reason() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)
    camera.inject_failure(1, CameraFailureMode.DOWNLOAD)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.DOWNLOAD_FAILURE
    assert result.capture_bundle is not None
    assert result.capture_bundle.status is CameraResultStatus.FAILED
    assert flight.calls[-1].operation is CommandOperation.HOVER


def test_wrong_camera_aircraft_identity_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    capabilities = camera.capabilities

    def wrong_identity(drone_id: int):  # type: ignore[no-untyped-def]
        return replace(capabilities(drone_id), drone_id=99)

    camera.calls.clear()
    monkeypatch.setattr(camera, "capabilities", wrong_identity)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE


@pytest.mark.parametrize(
    "boundary",
    ["capabilities", "state", "capture", "media_result", "media_file"],
)
def test_camera_boundaries_reject_bool_identity_smuggling(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)

    if boundary == "capabilities":
        original = camera.capabilities

        def malformed_capabilities(drone_id: int):  # type: ignore[no-untyped-def]
            return replace(original(drone_id), drone_id=True)

        monkeypatch.setattr(camera, "capabilities", malformed_capabilities)
    elif boundary == "state":
        original = camera.ready

        def malformed_state(drone_id: int):  # type: ignore[no-untyped-def]
            return replace(original(drone_id), connection_epoch=True)

        monkeypatch.setattr(camera, "ready", malformed_state)
    elif boundary == "capture":
        original = camera.capture_panorama

        def malformed_capture(drone_id: int, capture_id: str):  # type: ignore[no-untyped-def]
            return replace(original(drone_id, capture_id), drone_id=True)

        monkeypatch.setattr(camera, "capture_panorama", malformed_capture)
    elif boundary == "media_result":
        original = camera.retrieve

        def malformed_result(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
            return replace(original(drone_id, file_id), connection_epoch=True)

        monkeypatch.setattr(camera, "retrieve", malformed_result)
    else:
        original = camera.retrieve

        def malformed_file(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
            result = original(drone_id, file_id)
            assert result.media_file is not None
            return replace(result, media_file=replace(result.media_file, drone_id=True))

        monkeypatch.setattr(camera, "retrieve", malformed_file)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE
    assert flight.calls[-1].operation is CommandOperation.HOVER


def test_cross_linked_capture_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    capture = camera.capture_panorama

    def wrong_capture(drone_id: int, capture_id: str):  # type: ignore[no-untyped-def]
        return replace(capture(drone_id, capture_id), capture_id="different-capture")

    monkeypatch.setattr(camera, "capture_panorama", wrong_capture)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE


def test_cross_linked_media_result_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def wrong_file(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        return replace(retrieve(drone_id, file_id), file_id="other-file")

    monkeypatch.setattr(camera, "retrieve", wrong_file)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE


def test_cross_linked_media_file_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def wrong_media(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        return replace(result, media_file=replace(result.media_file, drone_id=99))

    monkeypatch.setattr(camera, "retrieve", wrong_media)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE


def test_media_file_with_nonterminal_retrieval_status_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def incomplete_media(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        return replace(
            result,
            media_file=replace(
                result.media_file,
                retrieval_status=CameraResultStatus.FAILED,
            ),
        )

    monkeypatch.setattr(camera, "retrieve", incomplete_media)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE


def test_reconstruct_bundle_rejects_duplicate_media_file_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    capture_photo = camera.capture_photo
    first_media = None

    def duplicate_media(drone_id: int, capture_id: str):  # type: ignore[no-untyped-def]
        nonlocal first_media
        result = capture_photo(drone_id, capture_id)
        if first_media is None:
            first_media = result.media
        return replace(result, media=first_media)

    monkeypatch.setattr(camera, "capture_photo", duplicate_media)

    result = controller.execute(capture_intent("reconstruct_8"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE


def test_reconstruct_bundle_rejects_media_outside_requested_yaw_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve
    returned = 0

    def swapped_yaw(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        nonlocal returned
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        swapped = (45.0, 0.0)[returned] if returned < 2 else result.media_file.actual_yaw_deg
        returned += 1
        return replace(result, media_file=replace(result.media_file, actual_yaw_deg=swapped))

    monkeypatch.setattr(camera, "retrieve", swapped_yaw)

    result = controller.execute(capture_intent("reconstruct_8"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE


@pytest.mark.parametrize(
    "changes",
    [
        {"pose": Position(9.0, 9.0, 1.0)},
        {"checksum_sha256": ""},
        {"checksum_sha256": "z" * 64},
        {"storage_ref": ""},
        {"timestamp_ms": 0},
        {"gimbal_pitch_deg": 2.0},
    ],
)
def test_capture_bundle_rejects_untrusted_media_evidence(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def malformed_media(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        return replace(result, media_file=replace(result.media_file, **changes))

    monkeypatch.setattr(camera, "retrieve", malformed_media)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE


def test_reconstruct_bundle_requires_measured_fov_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def insufficient_overlap(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        intrinsics = replace(result.media_file.intrinsics, horizontal_fov_deg=40.0)
        return replace(
            result,
            media_file=replace(result.media_file, intrinsics=intrinsics),
        )

    monkeypatch.setattr(camera, "retrieve", insufficient_overlap)

    result = controller.execute(capture_intent("reconstruct_8"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE


def test_panorama_bundle_requires_declared_full_horizontal_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, _, camera = make_stack(snapshot)
    retrieve = camera.retrieve

    def partial_panorama(drone_id: int, file_id: str):  # type: ignore[no-untyped-def]
        result = retrieve(drone_id, file_id)
        assert result.media_file is not None
        intrinsics = replace(result.media_file.intrinsics, horizontal_fov_deg=60.0)
        return replace(
            result,
            media_file=replace(result.media_file, intrinsics=intrinsics),
        )

    monkeypatch.setattr(camera, "retrieve", partial_panorama)

    result = controller.execute(capture_intent("pano_360"), snapshot)

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.CAMERA_FAILURE
