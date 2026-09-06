from __future__ import annotations

from dataclasses import replace

import pytest

from planner.mapped_formations import (
    FormationLayout,
    FormationPermission,
    FormationRefusal,
    FormationZone,
    MappedFormationPlan,
    MappedFormationPlanner,
    MappedFormationRequest,
    _slots,
)
from planner.navigation import (
    ArtifactPin,
    DronePose,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    Pose,
    Zone,
    preview_evidence,
)

MOTION = MotionConfig(0.15, 0.2, 0.02, 0.02, 0.05, 0.05, 0.2)
BOUNDARY = ((1.0, 1.0), (19.0, 1.0), (19.0, 19.0), (1.0, 19.0), (1.0, 1.0))
GEOFENCE = ((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0), (0.0, 0.0))


def pose(x: float, y: float, z: float = 1.5) -> Pose:
    return Pose(x, y, z, "level_1")


def drone(identity: int, at: Pose) -> DronePose:
    return DronePose(identity, 3, at)


def artifact(blocked: frozenset[tuple[int, int]] = frozenset()) -> NavigationArtifact:
    return NavigationArtifact(
        ArtifactPin("map-v3", "a" * 64),
        ArtifactPin("geometry-v3", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        0.75,
        GEOFENCE,
        0.0,
        4.0,
        (GridLevel("level_1", 1.5, (0.0, 0.0), 1.0, 20, 20, blocked),),
        (Zone("lobby", "level_1", True, BOUNDARY, 0.5, 3.5, ()),),
    )


def zone(map_artifact: NavigationArtifact, **changes: object) -> FormationZone:
    value = FormationZone(
        "lobby",
        "level_1",
        BOUNDARY,
        0.5,
        3.5,
        0.4,
        True,
        True,
        map_artifact.map_pin,
        map_artifact.geometry_pin,
    )
    return replace(value, **changes)


def request(
    shape: str,
    layout: FormationLayout,
    selected: tuple[DronePose, ...],
    *,
    airborne: frozenset[int] | None = None,
    permission: FormationPermission | None = None,
) -> MappedFormationRequest:
    return MappedFormationRequest(
        shape,
        9,
        12,
        selected,
        selected,
        frozenset(item.drone_id for item in selected) if airborne is None else airborne,
        MOTION,
        permission or FormationPermission(frozenset({"lobby"})),
        layout,
    )


@pytest.mark.parametrize(
    ("shape", "count"),
    (("line", 2), ("column", 2), ("line", 4), ("column", 4), ("wedge", 4), ("diamond", 4)),
)
def test_committed_shapes_produce_frozen_non_dispatchable_previews(
    shape: str,
    count: int,
) -> None:
    map_artifact = artifact()
    offsets = (-0.1, 0.1) if count == 2 else (-0.15, -0.05, 0.05, 0.15)
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, offsets)
    targets = _slots(shape, layout, "lobby")
    selected = tuple(drone(index + 1, target.pose) for index, target in enumerate(targets))

    result = MappedFormationPlanner().plan(
        request(shape, layout, selected),
        map_artifact,
        zone(map_artifact),
    )

    assert isinstance(result, MappedFormationPlan)
    assert result.dispatch_eligible is False
    assert result.navigation_plan.dispatch_eligible is False
    assert result.formation_zone.max_speed_mps == 0.4
    assert result.navigation_plan.execution_order == tuple(range(1, count + 1))
    assert all(assignment.cost_m == 0 for assignment in result.assignments)
    assert result.navigation_plan.navigation_pin != map_artifact.navigation_pin


def test_two_aircraft_cannot_request_four_aircraft_shape() -> None:
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, (-0.1, 0.1))
    selected = (drone(1, pose(9.0, 10.0, 1.4)), drone(2, pose(11.0, 10.0, 1.6)))

    with pytest.raises(ValueError, match="line and column"):
        request("diamond", layout, selected)


def test_four_aircraft_requires_explicit_altitude_stagger() -> None:
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, (0.0, 0.0, 0.0, 0.0))
    selected = tuple(drone(index, pose(4.0 + index, 4.0)) for index in range(1, 5))

    with pytest.raises(ValueError, match="distinct altitude offset"):
        request("line", layout, selected)


def test_permission_approval_and_pins_are_independent_navigation_gates() -> None:
    map_artifact = artifact()
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, (-0.1, 0.1))
    targets = _slots("line", layout, "lobby")
    selected = tuple(drone(index + 1, target.pose) for index, target in enumerate(targets))
    planner = MappedFormationPlanner()

    not_permitted = planner.plan(
        request(
            "line",
            layout,
            selected,
            permission=FormationPermission(frozenset({"atrium-front"})),
        ),
        map_artifact,
        zone(map_artifact),
    )
    unapproved = planner.plan(
        request("line", layout, selected),
        map_artifact,
        zone(map_artifact, owner_approved=False),
    )
    stale = planner.plan(
        request("line", layout, selected),
        map_artifact,
        zone(map_artifact, geometry_pin=ArtifactPin("geometry-v4", "d" * 64)),
    )

    assert not_permitted.code == "formation_not_permitted"
    assert unapproved.code == "formation_zone_unapproved"
    assert stale.code == "formation_artifact_changed"


def test_grounded_aircraft_is_refused_without_implicit_takeoff() -> None:
    map_artifact = artifact()
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, (-0.1, 0.1))
    targets = _slots("column", layout, "lobby")
    selected = tuple(drone(index + 1, target.pose) for index, target in enumerate(targets))

    result = MappedFormationPlanner().plan(
        request("column", layout, selected, airborne=frozenset({1})),
        map_artifact,
        zone(map_artifact),
    )

    assert result == FormationRefusal(
        "grounded_aircraft",
        "formation does not take off grounded aircraft",
    )


def test_full_cylindrical_envelopes_reject_diagonally_misleading_slots() -> None:
    map_artifact = artifact()
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 0.5, (-0.2, 0.2))
    selected = (drone(1, pose(8.0, 8.0)), drone(2, pose(12.0, 8.0)))

    result = MappedFormationPlanner().plan(
        request("line", layout, selected),
        map_artifact,
        zone(map_artifact),
    )

    assert result.code == "slot_separation"


def test_slot_must_fit_approved_volume_and_accepted_grid() -> None:
    map_artifact = artifact(frozenset({(9, 10)}))
    layout = FormationLayout(pose(10.0, 10.0), 0.0, 2.0, (-0.1, 0.1))
    selected = (drone(1, pose(8.0, 8.0)), drone(2, pose(12.0, 8.0)))
    blocked = MappedFormationPlanner().plan(
        request("line", layout, selected),
        map_artifact,
        zone(map_artifact),
    )
    outside = MappedFormationPlanner().plan(
        request("line", layout, selected),
        map_artifact,
        zone(
            map_artifact,
            polygon_xy=((9.5, 9.5), (10.5, 9.5), (10.5, 10.5), (9.5, 10.5), (9.5, 9.5)),
        ),
    )

    assert blocked.code == "slot_blocked"
    assert outside.code == "slot_outside_formation_zone"
