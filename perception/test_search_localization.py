from perception.detection_contracts import DetectionCandidate
from perception.search_localization import (
    FiveFrameLocalizer,
    SearchCameraModel,
    project_bottom_center,
)
from planner.navigation import Pose, Zone


def _zone():
    return Zone("atrium", "level-1", True, ((0, 0), (4, 0), (4, 4), (0, 4), (0, 0)), 0, 3, ())


def test_bottom_center_ray_projects_then_reports_the_five_frame_median_zone():
    model = SearchCameraModel(
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ((1, 0, 0, 1), (0, -1, 0, 3), (0, 0, -1, 2), (0, 0, 0, 1)),
    )
    candidate = DetectionCandidate("backpack", 24, 0.9, (0, 0, 2, 1))
    pose = project_bottom_center(candidate, 2, model, (_zone(),))
    assert pose is not None
    assert pose.x_m == 3 and pose.y_m == 1 and pose.z_m == 0
    localizer = FiveFrameLocalizer((_zone(),))
    assert [localizer.observe(pose) for _ in range(4)] == [None] * 4
    result = localizer.observe(pose)
    assert result is not None
    assert result.samples == 5 and result.pose.floor_id == "level-1"


def test_projection_and_zone_filter_refuse_unapproved_space():
    model = SearchCameraModel(
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ((1, 0, 0, 10), (0, -1, 0, 10), (0, 0, -1, 2), (0, 0, 0, 1)),
    )
    candidate = DetectionCandidate("backpack", 24, 0.9, (0, 0, 2, 1))
    assert project_bottom_center(candidate, 2, model, (_zone(),)) is None


def test_sighting_localization_requires_five_fresh_unique_matching_frames():
    from perception.detection_contracts import FrameIdentity, SightingEvent
    from perception.search_events import FramePoseEvidence

    model = SearchCameraModel(
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        ((1, 0, 0, 1), (0, -1, 0, 3), (0, 0, -1, 2), (0, 0, 0, 1)),
    )
    candidate = DetectionCandidate("backpack", 24, 0.9, (0, 0, 2, 1))
    localizer = FiveFrameLocalizer((_zone(),))
    pose = project_bottom_center(candidate, 2, model, (_zone(),))
    assert pose is not None
    for sequence in range(1, 6):
        identity = FrameIdentity("camera-1", "mission", "run", sequence)
        event = SightingEvent(
            "candidate-1", identity, 10, 10, 10, 10.01, candidate, sequence, "a" * 64
        )
        evidence = FramePoseEvidence(identity, 1, pose, 10, 10.01)
        result = localizer.observe_sighting(event, evidence, model, 2, 10.02, accepted_frame=True)
    assert result is not None
    assert localizer.observe_sighting(event, evidence, model, 2, 10.02, accepted_frame=True) is None


def test_localizer_refuses_a_median_outside_a_concave_approved_zone():
    zone = Zone(
        "u-shaped",
        "level-1",
        True,
        ((0, 0), (3, 0), (3, 3), (2, 3), (2, 1), (1, 1), (1, 3), (0, 3), (0, 0)),
        0,
        3,
        (),
    )
    localizer = FiveFrameLocalizer((zone,))
    samples = (
        Pose(0.5, 2, 0, "level-1"),
        Pose(0.5, 2.5, 0, "level-1"),
        Pose(2.5, 2, 0, "level-1"),
        Pose(2.5, 2.5, 0, "level-1"),
        Pose(1.5, 0.5, 0, "level-1"),
    )

    assert [localizer.observe(sample) for sample in samples] == [None] * 5
