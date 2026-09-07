from __future__ import annotations

from relay.audit import SessionAuditLog
from relay.auth import Principal, sign_event
from relay.capabilities import (
    C1_CAPABILITY_PROFILE,
    SURVEY_ADDITIONAL_INTENT_NAMES,
    CapabilityProfile,
)
from relay.contracts import NodeType
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.session import RelayLimits, RelaySession
from relay.survey_area import SurveyAreaLifecycle, SurveyCandidateRegistry
from relay.tests.conftest import (
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    intent_payload,
    membership_payload,
)

GROUND_ID = 9
GROUND_KEY = b"ground-adapter-key-that-is-at-least-32-bytes"
PROFILE = CapabilityProfile(
    "survey-test", C1_CAPABILITY_PROFILE.enabled_intent_names | SURVEY_ADDITIONAL_INTENT_NAMES
)


def _configuration() -> ObservationConfiguration:
    declarations = []
    bindings = []
    for source, frames, kinds in (
        ("ohmni-pose", ("odom", "body"), ("pose",)),
        ("ohmni-lidar", ("odom", "lidar"), ("range_scan",)),
    ):
        bindings.append(SourceBinding(SESSION, GROUND_ID, 1, source, "ground", frames, kinds))
        declarations.extend(
            FrameDeclaration(
                frame,
                frame,
                "right_handed_z_up" if frame == "odom" else "forward_left_up",
                "m",
                SESSION,
                GROUND_ID,
                1,
                source,
            )
            for frame in frames
        )
    return ObservationConfiguration(tuple(bindings), FrameRegistry(tuple(declarations)))


class _SurveySink:
    capability_profile = PROFILE

    def __init__(self, lifecycle: SurveyAreaLifecycle) -> None:
        self.lifecycle = lifecycle

    def __call__(self, intent, _state):  # type: ignore[no-untyped-def]
        return self.lifecycle.start(intent)

    def survey_lifecycle(self, request):  # type: ignore[no-untyped-def]
        return self.lifecycle.process(request)


def _session(tmp_path):  # type: ignore[no-untyped-def]
    clock = MutableClock()
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5_000, 5_000, 1_000, 1_000),
        clock=clock,
        event_ids=EventIds(),
        capability_profile=PROFILE,
        node_types={GROUND_ID: NodeType.GROUND},
        observation_configuration=_configuration(),
    )
    lifecycle = SurveyAreaLifecycle(session, SurveyCandidateRegistry(tmp_path / "candidates"))
    session.intent_sink = _SurveySink(lifecycle)
    adapter = Principal("adapter", GROUND_ID, GROUND_KEY)
    session.process_membership(
        membership_payload(
            action="join",
            event_id="join",
            drone_id=GROUND_ID,
            key=GROUND_KEY,
            node_type="ground",
            capabilities=["ground_drive"],
        ),
        adapter,
    )
    pose = _observation(
        "pose-1",
        "ohmni-pose",
        {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
    )
    session.process_observation(pose, adapter)
    ready = {
        "v": 1,
        "t": clock(),
        "type": "membership",
        "event_id": "ready",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "action": "readiness",
        "connection_epoch": 1,
        "drive_authority": True,
        "safety_operator_present": True,
        "local_stop_ready": True,
        "heartbeat_ready": True,
        "pose_identity": {
            "event_id": "pose-1",
            "session": SESSION,
            "connection_epoch": 1,
            "frame": "odom",
        },
    }
    ready["signature"] = sign_event(ready, GROUND_KEY)
    session.process_membership(ready, adapter)
    return session, lifecycle, adapter, clock


def _observation(event_id: str, source_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "v": 1,
        "type": "observation",
        "event_id": event_id,
        "session": SESSION,
        "device_id": GROUND_ID,
        "connection_epoch": 1,
        "source_id": source_id,
        "node_type": "ground",
        "frame": "odom" if source_id == "ohmni-pose" else "lidar",
        "confidence": 1.0,
        "t_capture": None,
        "t_source_receipt": {"clock_id": "clock", "unit": "ms", "value": 1},
        "clock_mapping_id": None,
        "payload": payload,
    }


def _lifecycle(
    intent_id: str, run_id: str, operation: str, event_id: str, epoch: int = 1
) -> dict[str, object]:
    return {
        "v": 1,
        "t": 1_756_700_000_000,
        "type": "survey_lifecycle",
        "event_id": event_id,
        "session": SESSION,
        "operation": operation,
        "intent_id": intent_id,
        "run_id": run_id,
        "connection_epoch": epoch,
    }


def test_survey_complete_uses_authenticated_lifecycle_and_saves_reloadable_candidate(tmp_path):
    session, lifecycle, adapter, _clock = _session(tmp_path)
    intent = intent_payload(intent_id="survey-1")
    intent.update(
        name="survey_area", args={"area_id": "floor-1"}, selection=[GROUND_ID], confirm=True
    )
    session.process_intent(intent, Principal("console", None, CONSOLE_KEY))
    started = session.execute_pending_intent("survey-1")
    assert started[-1]["status"] == "executing"
    scan = _observation(
        "scan-1",
        "ohmni-lidar",
        {
            "kind": "range_scan",
            "sensor_pose": {
                "parent_frame": "odom",
                "child_frame": "lidar",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
            "angle_min_rad": 0.0,
            "angle_increment_rad": 0.1,
            "range_min_m": 0.1,
            "range_max_m": 4.0,
            "ranges_m": [1.0],
            "mount_id": "rplidar",
        },
    )
    accepted = session.process_observation(scan, adapter)
    lifecycle.accepted_observation(
        __import__("relay.observations", fromlist=["Observation"]).Observation.parse(accepted[0])
    )
    completed = session.process_frame(
        _lifecycle("survey-1", "survey-survey-1", "complete", "complete-1"),
        Principal("console", None, CONSOLE_KEY),
    )
    assert completed[0]["status"] == "completed"
    candidate = lifecycle.candidates.load("candidate-survey-survey-1")
    assert candidate["pose_identity"]["event_id"] == "pose-1"
    assert candidate["scans"][0]["event_id"] == "scan-1"
    replay = session.process_frame(
        _lifecycle("survey-1", "survey-survey-1", "complete", "complete-1"),
        Principal("console", None, CONSOLE_KEY),
    )
    assert replay[0]["reason"] == "replayed_event"


def test_survey_cancel_and_wrong_epoch_do_not_save_partial_candidate(tmp_path):
    session, lifecycle, _adapter, _clock = _session(tmp_path)
    intent = intent_payload(intent_id="survey-cancel")
    intent.update(
        name="survey_area", args={"area_id": "floor-1"}, selection=[GROUND_ID], confirm=True
    )
    session.process_intent(intent, Principal("console", None, CONSOLE_KEY))
    session.execute_pending_intent("survey-cancel")
    wrong = session.process_frame(
        _lifecycle("survey-cancel", "survey-survey-cancel", "complete", "wrong", epoch=2),
        Principal("console", None, CONSOLE_KEY),
    )
    assert wrong[0]["reason"] == "stale_survey_epoch"
    cancelled = session.process_frame(
        _lifecycle("survey-cancel", "survey-survey-cancel", "cancel", "cancel"),
        Principal("console", None, CONSOLE_KEY),
    )
    assert cancelled[0]["status"] == "invalidated"
    assert not list((tmp_path / "candidates").glob("*.json"))
