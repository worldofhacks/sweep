"""Synthetic mapped-navigation setup for the isolated browser demo."""

from __future__ import annotations

from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    GridLevel,
    MotionConfig,
    NavigationArtifact,
    NavigationDispatchAcceptance,
    NavigationPermission,
    Pose,
    Zone,
    preview_evidence,
)
from planner.navigation_runtime import NavigationExecutionConfig, NavigationRuntime

_MOTION = MotionConfig(0.15, 0.2, 0.05, 0.03, 0.1, 0.05, 0.2)


def _zone(zone_id: str, x: float, y: float, *aliases: str, slots: int = 1) -> Zone:
    return Zone(
        zone_id,
        "level_1",
        True,
        (
            (x - 1.0, y - 1.6),
            (x + 1.0, y - 1.6),
            (x + 1.0, y + 1.6),
            (x - 1.0, y + 1.6),
            (x - 1.0, y - 1.6),
        ),
        0.0,
        4.0,
        tuple(
            ArrivalSlot(
                f"{zone_id}-{index}",
                zone_id,
                Pose(x, y + index - 1, 1.0, "level_1"),
                0.5,
                0.5,
            )
            for index in range(1, slots + 1)
        ),
        aliases,
    )


def navigation_demo_runtime() -> NavigationRuntime:
    zones = (
        _zone("lobby", 1.5, 1.5, "lobby"),
        _zone("formation-one", 3.5, 1.5, "formation one"),
        _zone("formation-two", 5.5, 1.5, "formation two"),
        _zone("atrium", 6.5, 1.5, "atrium", slots=2),
        _zone("kitchen", 1.5, 3.5, "kitchen"),
    )
    artifact = NavigationArtifact(
        ArtifactPin("map-v2", "a" * 64),
        ArtifactPin("geometry-v2", "b" * 64),
        ArtifactPin("preview", "c" * 64),
        preview_evidence("synthetic"),
        0.75,
        ((-2.0, -2.0), (10.0, -2.0), (10.0, 7.0), (-2.0, 7.0), (-2.0, -2.0)),
        -1.0,
        5.0,
        (GridLevel("level_1", 1.0, (0.0, 0.0), 1.0, 8, 5, frozenset()),),
        zones,
    )

    def acceptance(plan, current):
        return NavigationDispatchAcceptance(
            "demo-acceptance",
            plan.map_pin,
            plan.geometry_pin,
            plan.navigation_pin,
            plan.plan_revision,
        )

    return NavigationRuntime(
        lambda: artifact,
        NavigationExecutionConfig("level_1", _MOTION, 0.5, 0.05, 5_000, 0.5, 5_000),
        NavigationPermission(frozenset(zone.zone_id for zone in zones)),
        acceptance,
    )
