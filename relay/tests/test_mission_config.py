import json

import pytest

from perception.search_events import SearchMissionIdentity
from planner.mapped_formation_runtime import MappedFormationRuntime
from planner.models import Plan, Position
from planner.navigation import (
    ArtifactPin,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationPermission,
    Pose,
    Zone,
    preview_evidence,
)
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime
from planner.search import SearchDrone, SearchPlanner, SearchRefusal, SearchRequest
from relay.intent_v1 import IntentName
from relay.mission_config import load_detection_camera_ids, load_mission_config
from relay.settings import SettingsError
from tests.autonomy_fixtures import make_intent, make_snapshot, replace_aircraft

MOTION = MotionConfig(0.15, 0.2, 0.02, 0.02, 0.05, 0.05)
PIN = ArtifactPin("map-v3", "a" * 64)


def artifact() -> NavigationArtifact:
    return NavigationArtifact(
        PIN,
        ArtifactPin("geometry-v3", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        0.5,
        ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)),
        0.0,
        4.0,
        (GridLevel("level_1", 1.5, (0, 0), 1, 20, 20, frozenset()),),
        (
            Zone(
                "search-zone",
                "level_1",
                True,
                ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0)),
                0.0,
                4.0,
                (),
            ),
        ),
    )


def navigation() -> NavigationRuntime:
    return NavigationRuntime(
        artifact,
        NavigationExecutionConfig("level_1", MOTION, 0.5, 0.05, 500, 0.5, 5_000),
        NavigationPermission(frozenset({"search-zone"})),
    )


def config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mapped_formations": {
            "permission_zone_ids": ["lobby"],
            "formations": {
                "line": {
                    "shape": "line",
                    "zone": {
                        "zone_id": "lobby",
                        "floor_id": "level_1",
                        "polygon_xy": [[1, 1], [19, 1], [19, 19], [1, 19], [1, 1]],
                        "z_min_m": 0.5,
                        "z_max_m": 3.5,
                        "owner_approved": True,
                        "formation_enabled": True,
                    },
                    "layout": {
                        "center": {"x_m": 10, "y_m": 10, "z_m": 1.5, "floor_id": "level_1"},
                        "heading_rad": 0,
                        "spacing_m": 2,
                        "altitude_offsets_m": [0, 0],
                    },
                }
            },
        },
        "search": {
            "areas": [
                {
                    "zone_id": "search-zone",
                    "floor_id": "level_1",
                    "polygon_xy_m": [[1, 1], [10, 1], [10, 5], [1, 5]],
                }
            ],
            "map_pin": {"version": "map-v3", "content_sha256": "a" * 64},
            "camera": {
                "horizontal_fov_deg": 90,
                "vertical_fov_deg": 90,
                "height_agl_m": 1,
                "gimbal_pitch_deg": -90,
                "gimbal_min_pitch_deg": -90,
                "gimbal_max_pitch_deg": 0,
                "overlap_fraction": 0.25,
            },
            "calibration_id": "search-camera-v1",
            "source_by_drone": {"1": "physical-camera-1"},
            "permission_zone_ids": ["search-zone"],
            "mission_version": 1,
            "maximum_drones": 1,
            "floor_z_m": 0,
            "height_tolerance_m": 0.05,
            "camera_offset_z_m": 0,
        },
    }


def load(tmp_path, raw: dict[str, object] | None = None):
    path = tmp_path / "mission.json"
    path.write_text(json.dumps(raw or config()))
    return load_mission_config(navigation(), {"SWEEP_MISSION_CONFIG": str(path)})


def test_explicit_config_builds_runtimes_and_drives_real_planners(tmp_path):
    configured = load(tmp_path)
    assert configured is not None
    assert isinstance(configured.mapped_formations, MappedFormationRuntime)
    assert configured.search is not None
    assert configured.search.floor_z_m == 0
    assert configured.search.camera_offset_z_m == 0
    assert configured.search.height_tolerance_m == 0.05

    snapshot = make_snapshot(2)
    snapshot = replace_aircraft(snapshot, 1, pose=Position(8, 8, 1.5))
    snapshot = replace_aircraft(snapshot, 2, pose=Position(12, 8, 1.5))
    formation = configured.mapped_formations.prepare(
        make_intent(
            IntentName.FORMATION_SET,
            selection=(1, 2),
            args={"name": "line"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(formation, Plan)

    search = configured.search
    drone = DronePose(1, 1, Pose(1.5, 1.5, 1.5, "level_1"))
    preview = SearchPlanner().plan(
        SearchRequest(
            SearchMissionIdentity("mission", search.mission_version, 7),
            search.areas["search-zone"],
            "backpack",
            7,
            7,
            (SearchDrone(drone, search.source_by_drone[1]),),
            (drone,),
            search.map_pin,
            search.calibration_id,
            search.camera,
            MOTION,
            search.permission,
            "confirmed",
        ),
        artifact(),
    )
    assert not isinstance(preview, SearchRefusal)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw["mapped_formations"].update({"unknown": True}),
        lambda raw: raw["mapped_formations"].update({"permission_zone_ids": ["wrong"]}),
        lambda raw: raw["search"].update(
            {"map_pin": {"version": "old", "content_sha256": "c" * 64}}
        ),
        lambda raw: raw["search"]["camera"].update({"height_agl_m": float("nan")}),
        lambda raw: raw["search"].pop("floor_z_m"),
        lambda raw: raw["search"].update({"height_tolerance_m": -0.01}),
        lambda raw: raw["mapped_formations"]["formations"]["line"].update({"shape": []}),
    ],
)
def test_config_rejects_unknown_mismatched_and_nonfinite_values(tmp_path, mutate):
    raw = config()
    mutate(raw)
    with pytest.raises(SettingsError):
        load(tmp_path, raw)


def test_absent_file_config_disables_both_optional_runtimes():
    assert load_mission_config(navigation(), {}) is None


def test_detection_camera_identity_loader_is_explicit_and_strict():
    assert load_detection_camera_ids({}) == {}
    assert load_detection_camera_ids(
        {"SWEEP_DETECTION_CAMERA_IDS_JSON": '{"1":"front-camera"}'}
    ) == {1: "front-camera"}
    for raw in ("[]", '{"01":"camera"}', '{"١":"camera"}', '{"1":""}', '{"x":"camera"}'):
        with pytest.raises(SettingsError):
            load_detection_camera_ids({"SWEEP_DETECTION_CAMERA_IDS_JSON": raw})
