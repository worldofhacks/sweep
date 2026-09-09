from dataclasses import replace
from math import dist

import pytest

from perception.object_detection import FrameIdentity, ProcessedFrameEvent
from perception.search_events import FramePoseEvidence
from planner.models import Position
from planner.navigation import Pose
from planner.test_navigation_runtime import setup_runtime
from relay.search_runtime import SearchMissionPreview, SearchRuntime
from relay.tests.test_search_runtime import _intent, _search_runtime


@pytest.mark.parametrize("use_aircraft_enu", [True, False])
def test_survey_activates_coverage_only_at_the_world_arrival(use_aircraft_enu: bool) -> None:
    navigation, snapshot, _, _ = setup_runtime(
        transform=(
            (0.0, -1.0, 0.0, 10.0),
            (1.0, 0.0, 0.0, 20.0),
            (0.0, 0.0, 1.0, 30.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    frame = navigation.config.frame(1)
    snapshot = replace(
        snapshot,
        aircraft={
            1: replace(
                snapshot.aircraft[1], pose=Position(*frame.enu(Pose(0.5, 1.5, 1, "level_1")))
            )
        },
    )
    runtime = SearchRuntime(_search_runtime().config, navigation)
    preview = runtime.prepare(_intent("survey-frame", survey=True), snapshot)
    assert isinstance(preview, SearchMissionPreview)
    assignment = preview.search.assignments[0]
    arrival = assignment.transit.arrival_slot.pose
    enu = frame.enu(arrival)
    assert dist(enu, arrival.xyz) > 1
    arrived = replace(
        snapshot,
        aircraft={
            1: replace(
                snapshot.aircraft[1],
                pose=Position(*(enu if use_aircraft_enu else arrival.xyz)),
                position_last_seen_ms=snapshot.now_ms,
            )
        },
    )
    runtime.start("survey-frame")
    runtime._activate_arrived_tasks(runtime._mission("survey-frame"), arrived)
    task = assignment.task
    identity = FrameIdentity(task.source_id, preview.search.mission.frame_mission_id, "survey", 1)
    observation = runtime.observe_processed_frame(
        "survey-frame",
        ProcessedFrameEvent(identity, 1.0, 1.0, 1.0, "empty", 0, ("backpack",), "a" * 64),
        FramePoseEvidence(identity, task.connection_epoch, task.cells[0].pose, 1.0, 1.0),
        now_s=1.0,
    )

    assert observation.accepted is use_aircraft_enu
    covered_cells = runtime.status_payload("survey-frame")["tasks"][0]["covered_cells"]
    assert (covered_cells > 0) is use_aircraft_enu
