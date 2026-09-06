from planner.models import CommandOperation, Plan
from planner.test_navigation_runtime import _intent, _runtime, _snapshot
from relay.auth import verify_event_signature
from relay.control_localization import (
    ClockMapping,
    ControlLocalizationPins,
    ControlLocalizationProjector,
    ControlPose,
)
from relay.navigation_control import NavigationControl, NavigationControlConfig

KEY = b"navigation-node-key-at-least-32bytes"


class _Session:
    session_id = "session"

    def __init__(self, pose: ControlPose) -> None:
        self._pose = pose

    def control_pose(self, drone_id: int) -> ControlPose | None:
        return self._pose if drone_id == self._pose.drone_id else None


def _projector() -> ControlLocalizationProjector:
    clock = ClockMapping("capture", "relay", 0, 100_000, 1_000, 5, True)
    pin = ControlLocalizationPins(1, "map", "geometry", "camera", "body", ("tag",), clock)
    return ControlLocalizationProjector(
        {1: pin},
        relay_clock_id="relay",
        max_clock_error_ms=5,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.3,
    )


def _pose() -> ControlPose:
    return ControlPose(
        100_000,
        "diagnostic-pose",
        "session",
        1,
        1,
        "map",
        "geometry",
        "camera",
        "body",
        100_000,
        99_900,
        500,
        1_500,
        1_000,
        "map_enu",
        28,
        "ready",
    )


def test_signed_navigation_packets_bind_the_exact_phone_route() -> None:
    runtime = _runtime()
    snapshot = _snapshot()
    intent = _intent()
    runtime.require_phone_authorization = True
    projector = _projector()
    runtime.configure_control_localization(
        projector.pins,
        max_fix_age_ms=projector.max_fix_age_ms,
        max_position_uncertainty_p95_m=projector.max_position_uncertainty_p95_m,
    )
    runtime.maximum_aircraft = 1
    control = NavigationControl(NavigationControlConfig(runtime, projector, "config", {1: KEY}))
    session = _Session(_pose())
    approved = control.approved_snapshot(snapshot, session)

    prepared = runtime.prepare(intent, approved)
    assert isinstance(prepared, Plan)
    command = next(item for item in prepared.commands if item.operation is CommandOperation.GOTO)
    authorization = control.authorize(prepared, command, approved, session.session_id)
    initial = control.initial_pose(1, session, approved.now_ms)

    for packet in (authorization, initial):
        unsigned = dict(packet)
        signature = unsigned.pop("signature")
        assert verify_event_signature(unsigned, signature, KEY)
    assert authorization["route_id"] == prepared.intent_id
    assert authorization["command_id"] == command.command_id
    assert initial["status"] == "ready"

    stale = control.periodic_poses(session, approved.now_ms + 501)[0]
    assert stale["status"] == "hold"
    assert stale["flight_approved"] is True
    assert all(
        stale[field] is None
        for field in (
            "pose_time_ms",
            "fix_time_ms",
            "x_mm",
            "y_mm",
            "z_mm",
            "position_uncertainty_mm",
        )
    )
