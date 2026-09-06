from planner.models import CommandOperation, PreparedExecution
from planner.test_navigation_runtime import stack
from relay.auth import verify_event_signature
from relay.control_config import ControlRuntimeConfig
from relay.control_localization import ClockMapping, ControlLocalizationPins, ControlPose
from relay.navigation_control import NavigationControl, NavigationControlConfig

KEY = b"navigation-node-key-at-least-32bytes"


class _Session:
    session_id = "session"

    def __init__(self, pose: ControlPose) -> None:
        self._pose = pose

    def control_pose(self, drone_id: int) -> ControlPose | None:
        return self._pose if drone_id == self._pose.drone_id else None


def control_config() -> ControlRuntimeConfig:
    clock = ClockMapping("capture", "relay", 0, 100_000, 1_000, 5, True)
    pin = ControlLocalizationPins(1, "map", "geometry", "camera", "body", ("tag",), clock)
    return ControlRuntimeConfig({1: pin}, 5, 500, 200, 200, 0.3)


def pose() -> ControlPose:
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


def test_explicit_host_approval_turns_a_diagnostic_pose_into_signed_route_evidence() -> None:
    controller, _, _, snapshot, current, _, intent = stack()
    runtime = controller.planner.navigation
    runtime.require_phone_authorization = True
    config = control_config()
    runtime.configure_control_localization(
        config.pins,
        max_fix_age_ms=config.max_fix_age_ms,
        max_position_uncertainty_p95_m=config.max_position_uncertainty_p95_m,
    )
    runtime.maximum_aircraft = 1
    control = NavigationControl(NavigationControlConfig(runtime, config, "config", {1: KEY}))
    session = _Session(pose())
    approved = control.approved_snapshot(snapshot, session)

    prepared = controller.prepare(intent, approved, current_snapshot=lambda: approved)

    assert isinstance(prepared, PreparedExecution)
    command = next(
        item for item in prepared.plan.commands if item.operation is CommandOperation.GOTO
    )
    authorization = control.authorize(prepared.plan, command, approved, session.session_id)
    initial = control.initial_pose(1, session, approved.now_ms)
    for packet in (authorization, initial):
        unsigned = dict(packet)
        signature = unsigned.pop("signature")
        assert verify_event_signature(unsigned, signature, KEY)
    assert authorization["flight_approved"] is True
    assert authorization["max_position_uncertainty_mm"] == 30
    assert authorization["tube_radius_mm"] == 130
    assert initial["status"] == "ready"
    assert approved.aircraft[1].control_provenance is not None
    refreshed = control.periodic_poses(session, approved.now_ms)[0]
    stale = control.periodic_poses(session, approved.now_ms + 501)[0]
    for packet in (refreshed, stale):
        unsigned = dict(packet)
        signature = unsigned.pop("signature")
        assert verify_event_signature(unsigned, signature, KEY)
    assert refreshed["status"] == "ready"
    assert refreshed["seq"] > initial["seq"]
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
    assert control.periodic_poses(session, approved.now_ms + 502) == []


def test_diagnostic_pose_never_becomes_navigation_evidence_without_host_approval() -> None:
    controller, _, _, snapshot, _, _, intent = stack()
    runtime = controller.planner.navigation
    runtime.require_phone_authorization = True
    config = control_config()
    runtime.configure_control_localization(
        config.pins,
        max_fix_age_ms=config.max_fix_age_ms,
        max_position_uncertainty_p95_m=config.max_position_uncertainty_p95_m,
    )
    runtime.maximum_aircraft = 1

    refused = controller.prepare(intent, snapshot, current_snapshot=lambda: snapshot)

    assert not isinstance(refused, PreparedExecution)
