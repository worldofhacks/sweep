from dataclasses import replace

import pytest

from adapters.protocols import CaptureCoverage, CapturePattern
from adapters.sim.camera import CameraFailureMode
from planner.models import CommandOperation, LifecycleStatus, Plan, RefusalReason
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


def test_single_still_runs_one_shutter_and_retrieval_without_rotation() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        confirm=True,
        args={"room_id": "viewpoint-a", "capture_id": "photo-a", "pattern": "single_still"},
    )

    result = controller.execute(intent, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    bundle = result.capture_bundle
    assert bundle is not None
    assert bundle.pattern is CapturePattern.SINGLE_STILL
    assert bundle.coverage is CaptureCoverage.SINGLE_VIEW
    assert len(bundle.media) == 1
    assert bundle.media[0].pose == snapshot.aircraft[1].pose
    assert bundle.media[0].intrinsics.projection == "rectilinear"
    assert [call.operation for call in flight.calls] == [CommandOperation.HOVER]
    assert len([call for call in camera.calls if call[0] == "capture_photo"]) == 1
    assert len([call for call in camera.calls if call[0] == "retrieve"]) == 1


@pytest.mark.parametrize(
    "mutation", ["extra_shutter", "rotation", "wrong_retrieval", "missing_photo"]
)
def test_single_still_rejects_changed_command_sequence_before_io(mutation: str) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    _, planner, _, dispatcher, flight, camera = make_stack(snapshot)
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            confirm=True,
            args={"room_id": "viewpoint-a", "capture_id": "photo-a", "pattern": "single_still"},
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    commands = list(plan.commands)
    if mutation == "extra_shutter":
        commands.append(replace(commands[4], command_id="extra-shutter"))
    elif mutation == "rotation":
        commands[3] = replace(
            commands[3],
            operation=CommandOperation.ROTATE_TO,
            parameters={"yaw": 90, "speed": 10, "tolerance": 1, "min_overlap": 10},
        )
    elif mutation == "wrong_retrieval":
        commands[5] = replace(commands[5], parameters={"source_command_id": "other-photo"})
    else:
        del commands[4]
    result = dispatcher.dispatch(replace(plan, commands=tuple(commands)), snapshot)
    assert result.refusal is not None
    assert result.refusal.reason is RefusalReason.INVALID_PLAN
    assert not flight.calls and not camera.calls


@pytest.mark.parametrize("failure", [CameraFailureMode.CAMERA, CameraFailureMode.DOWNLOAD])
def test_single_still_camera_failure_keeps_failed_bundle_and_holds(
    failure: CameraFailureMode,
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    controller, _, _, _, flight, camera = make_stack(snapshot)
    camera.inject_failure(1, failure)
    result = controller.execute(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            confirm=True,
            args={"room_id": "viewpoint-a", "capture_id": "photo-a", "pattern": "single_still"},
        ),
        snapshot,
    )
    assert result.refusal is not None
    assert result.capture_bundle is not None
    assert result.capture_bundle.status.value == "failed"
    assert result.capture_bundle.coverage is CaptureCoverage.SINGLE_VIEW
    assert not result.capture_bundle.media
    assert flight.calls[-1].operation is CommandOperation.HOVER
