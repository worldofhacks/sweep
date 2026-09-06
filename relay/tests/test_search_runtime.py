from __future__ import annotations

from dataclasses import replace

import pytest

from adapters.sim.camera import CameraFailureMode
from perception.object_detection import FrameIdentity, ProcessedFrameEvent
from perception.search_events import CameraPolicy, FramePoseEvidence
from planner.models import LifecycleStatus, Position, PreparedExecution, Refusal
from planner.navigation import NavigationPermission
from planner.search import SearchArea
from planner.test_navigation_runtime import stack
from relay.intent_v1 import IntentName
from relay.search_runtime import SearchMissionPreview, SearchRuntime, SearchRuntimeConfig
from tests.autonomy_fixtures import make_intent, replace_aircraft


def _prepared(intent_id: str = "search-runtime", *, mission_cache_limit: int = 32):
    controller, dispatcher, flight, snapshot, current, maps, _ = stack(1)
    runtime = SearchRuntime(
        SearchRuntimeConfig(
            {"atrium": SearchArea("atrium", "level_1", ((0, 0), (8, 0), (8, 4), (0, 4)))},
            maps[0].map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            "camera-calibration-v1",
            {1: "camera-1"},
            NavigationPermission(frozenset({"atrium"})),
            mission_cache_limit=mission_cache_limit,
            floor_z_m=0,
            camera_offset_z_m=0,
        ),
        dispatcher.navigation,
    )
    intent = make_intent(
        IntentName.NAVIGATE,
        selection=(1,),
        args={"zone_id": "atrium", "target_class": "backpack"},
        confirm=True,
        intent_id=intent_id,
    )
    preview = runtime.prepare(intent, snapshot)
    assert isinstance(preview, SearchMissionPreview)
    return controller, dispatcher, flight, snapshot, current, intent, runtime, preview


def _at_first_coverage(snapshot, preview: SearchMissionPreview):
    task = preview.search.assignments[0].task
    cell = task.cells[0]
    return replace_aircraft(
        replace(snapshot, now_ms=snapshot.now_ms + 1),
        1,
        pose=Position(cell.pose.x_m, cell.pose.y_m, cell.pose.z_m),
        position_last_seen_ms=snapshot.now_ms + 1,
    )


def _frame(task, cell, index: int) -> tuple[ProcessedFrameEvent, FramePoseEvidence]:
    timestamp = 10 + index / 10
    identity = FrameIdentity(task.source_id, task.task_id.split(":v", 1)[0], "test-run", index + 1)
    event = ProcessedFrameEvent(
        identity,
        timestamp,
        timestamp,
        timestamp + 0.01,
        "empty",
        0,
        ("backpack",),
        "a" * 64,
    )
    return event, FramePoseEvidence(
        identity, task.connection_epoch, cell.pose, timestamp, timestamp + 0.02
    )


def test_prepare_refuses_without_camera_height_references_or_at_wrong_height() -> None:
    _, _, _, snapshot, _, intent, runtime, _ = _prepared("height")

    unverified = SearchRuntime(replace(runtime.config, floor_z_m=None), runtime.navigation)
    unverified_result = unverified.prepare(intent, snapshot)
    assert isinstance(unverified_result, Refusal)
    assert unverified_result.detail == "camera_height_unverified"

    mismatched = SearchRuntime(replace(runtime.config, camera_offset_z_m=0.2), runtime.navigation)
    mismatched_result = mismatched.prepare(intent, snapshot)
    assert isinstance(mismatched_result, Refusal)
    assert mismatched_result.detail == "camera_height_mismatch"


def test_controller_dispatches_guarded_coverage_route_and_processed_frames_complete_it() -> None:
    controller, _, flight, snapshot, current, intent, runtime, preview = _prepared()
    task_route = preview.task_routes[0]
    task = preview.search.assignments[0].task

    assert len(preview.plan.navigation.route.routes[0].swept_segments) > len(
        preview.search.assignments[0].transit.swept_segments
    )
    assert runtime.start(intent.intent_id, snapshot).state == "running"
    at_coverage = _at_first_coverage(snapshot, preview)
    assert (
        runtime.on_command(intent.intent_id, task_route.gimbal_command_id, snapshot).state
        == "running"
    )
    assert (
        runtime.on_command(intent.intent_id, task_route.camera_ready_command_id, snapshot).state
        == "running"
    )
    assert (
        runtime.on_command(intent.intent_id, task_route.first_coverage_command_id, at_coverage)
        .tasks[0]
        .state
        == "active"
    )

    for index, cell in enumerate(task.cells):
        event, evidence = _frame(task, cell, index)
        assert runtime.observe_processed_frame(intent.intent_id, event, evidence).accepted

    result = controller.dispatch_prepared(
        PreparedExecution(intent, preview.plan, snapshot), current_snapshot=current
    )

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert (
        runtime.on_command(intent.intent_id, task_route.terminal_command_id, current()).state
        == "covered"
    )
    assert [call.operation for call in flight.calls].count("goto") == len(
        preview.plan.navigation.route.routes[0].swept_segments
    )


def test_route_completion_without_processed_frames_marks_task_incomplete() -> None:
    controller, _, _, snapshot, current, intent, runtime, preview = _prepared("no-frames")
    task_route = preview.task_routes[0]

    runtime.start(intent.intent_id, snapshot)
    runtime.on_command(intent.intent_id, task_route.gimbal_command_id, snapshot)
    runtime.on_command(intent.intent_id, task_route.camera_ready_command_id, snapshot)
    runtime.on_command(
        intent.intent_id,
        task_route.first_coverage_command_id,
        _at_first_coverage(snapshot, preview),
    )
    result = controller.dispatch_prepared(
        PreparedExecution(intent, preview.plan, snapshot), current_snapshot=current
    )
    status = runtime.on_command(intent.intent_id, task_route.terminal_command_id, current())

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert status.state == "incomplete"
    assert status.events[0].requires_fresh_confirmation


def test_hold_and_cancel_reject_replayed_frames_and_never_reassign_after_reconnect() -> None:
    _, _, _, snapshot, _, intent, runtime, preview = _prepared("hold-cancel")
    task_route = preview.task_routes[0]
    task = preview.search.assignments[0].task
    event, evidence = _frame(task, task.cells[0], 1)

    runtime.start(intent.intent_id, snapshot)
    runtime.on_command(intent.intent_id, task_route.gimbal_command_id, snapshot)
    runtime.on_command(intent.intent_id, task_route.camera_ready_command_id, snapshot)
    runtime.on_command(
        intent.intent_id,
        task_route.first_coverage_command_id,
        _at_first_coverage(snapshot, preview),
    )
    assert runtime.hold(intent.intent_id, "operator_hold").state == "hold"
    held_observation = runtime.observe_processed_frame(intent.intent_id, event, evidence)
    assert held_observation.reason == "task_not_active"
    assert runtime.cancel(intent.intent_id, "operator_cancelled").state == "cancelled"
    cancelled_observation = runtime.observe_processed_frame(intent.intent_id, event, evidence)
    assert cancelled_observation.reason == "task_not_active"

    _, _, _, reconnect_snapshot, _, reconnect_intent, reconnect_runtime, reconnect_preview = (
        _prepared("reconnect")
    )
    reconnect_runtime.start(reconnect_intent.intent_id, reconnect_snapshot)
    changed_epoch = replace_aircraft(
        _at_first_coverage(reconnect_snapshot, reconnect_preview), 1, connection_epoch=2
    )
    status = reconnect_runtime.on_command(
        reconnect_intent.intent_id,
        reconnect_preview.task_routes[0].first_coverage_command_id,
        changed_epoch,
    )

    assert status.tasks[0].state == "pending"
    assert status.tasks[0].task_id == reconnect_preview.search.assignments[0].task.task_id


def test_check_revalidates_search_roster_and_delegates_navigation_cursor() -> None:
    _, _, _, snapshot, _, _, runtime, preview = _prepared("search-check")
    command = preview.plan.commands[0]

    assert runtime.active_mission(1) is None
    assert runtime.start(preview.plan.intent_id, snapshot).state == "running"
    active = runtime.active_mission(1)
    assert active == (preview.plan.intent_id, preview)
    assert runtime.current_task(preview.plan.intent_id, 1) == preview.search.assignments[0].task
    assert runtime.progress(preview.plan.intent_id, 1).total_cells > 0
    assert runtime.check(preview.plan, command, snapshot) is None


def test_search_freezes_camera_prelude_before_transit_and_requires_it_for_coverage() -> None:
    _, _, _, snapshot, _, intent, runtime, preview = _prepared("camera-prelude")
    route = preview.task_routes[0]

    assert [command.operation.value for command in preview.plan.commands[:2]] == [
        "set_gimbal_pitch",
        "camera_ready",
    ]
    runtime.start(intent.intent_id, snapshot)
    at_coverage = _at_first_coverage(snapshot, preview)
    assert (
        runtime.on_command(intent.intent_id, route.first_coverage_command_id, at_coverage)
        .tasks[0]
        .state
        == "pending"
    )
    runtime.on_command(intent.intent_id, route.gimbal_command_id, snapshot)
    runtime.on_command(intent.intent_id, route.camera_ready_command_id, snapshot)

    assert (
        runtime.on_command(intent.intent_id, route.first_coverage_command_id, at_coverage)
        .tasks[0]
        .state
        == "active"
    )


def test_preview_allows_unconfirmed_search_but_active_mission_requires_running_state() -> None:
    _, _, _, snapshot, _, intent, runtime, preview = _prepared("unconfirmed")
    unconfirmed = replace(intent, confirm=False, intent_id="unconfirmed-preview")

    assert isinstance(runtime.prepare(unconfirmed, snapshot), SearchMissionPreview)
    assert runtime.active_mission(1) is None

    runtime.start(intent.intent_id, snapshot)
    assert runtime.active_mission(1) == (intent.intent_id, preview)
    runtime.hold(intent.intent_id, "operator_hold")
    assert runtime.active_mission(1) is None


def test_search_cache_evicts_retired_missions_and_rejects_over_capacity_running_missions() -> None:
    _, _, _, snapshot, _, intent, runtime, _ = _prepared("first", mission_cache_limit=1)
    second = replace(intent, intent_id="second")

    assert isinstance(runtime.prepare(second, snapshot), SearchMissionPreview)
    with pytest.raises(ValueError, match="search mission is unknown"):
        runtime.preview(intent.intent_id)

    runtime.start(second.intent_id, snapshot)
    third = replace(intent, intent_id="third")
    assert not isinstance(runtime.prepare(third, snapshot), SearchMissionPreview)


def test_start_rejects_a_second_running_mission_for_the_same_drone() -> None:
    _, _, _, snapshot, _, intent, runtime, _ = _prepared("running-first", mission_cache_limit=2)
    second = replace(intent, intent_id="running-second")
    assert isinstance(runtime.prepare(second, snapshot), SearchMissionPreview)

    runtime.start(intent.intent_id, snapshot)
    with pytest.raises(ValueError, match="already has a running search mission"):
        runtime.start(second.intent_id, snapshot)


def test_camera_ready_failure_stops_search_before_any_coverage_transit() -> None:
    controller, dispatcher, flight, snapshot, current, intent, runtime, preview = _prepared(
        "camera-failure"
    )
    dispatcher.camera.inject_failure(1, CameraFailureMode.UNSUPPORTED)
    dispatcher.on_navigation_command_completed = lambda plan, command, state: runtime.on_command(
        plan.intent_id, command.command_id, state
    )
    runtime.start(intent.intent_id, snapshot)

    result = controller.dispatch_prepared(
        PreparedExecution(intent, preview.plan, snapshot), current_snapshot=current
    )

    assert result.status is LifecycleStatus.REFUSED
    assert [operation for operation, _, _ in dispatcher.camera.calls] == [
        "set_gimbal_pitch",
        "ready",
    ]
    assert not any(call.operation == "goto" for call in flight.calls)
    assert runtime.status(intent.intent_id).tasks[0].state == "pending"
