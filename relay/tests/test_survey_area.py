from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.audit import AuditLogError, SessionAuditLog
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
from relay.settings import RelaySettings
from relay.survey_area import (
    SurveyAreaLifecycle,
    SurveyCandidateRegistry,
    SurveyLifecycleRequest,
    _candidate_id,
)
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
        bindings.append(
            SourceBinding(
                SESSION,
                GROUND_ID,
                1,
                source,
                "ground",
                frames,
                kinds,
                range_mount_id="rplidar" if source == "ohmni-lidar" else None,
            )
        )
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

    def accepted_observation(self, observation):  # type: ignore[no-untyped-def]
        return self.lifecycle.accepted_observation(observation)


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
    intent_id: str,
    run_id: str,
    operation: str,
    event_id: str,
    epoch: int = 1,
    t: int = 1_756_700_000_000,
) -> dict[str, object]:
    return {
        "v": 1,
        "t": t,
        "type": "survey_lifecycle",
        "event_id": event_id,
        "session": SESSION,
        "operation": operation,
        "intent_id": intent_id,
        "run_id": run_id,
        "connection_epoch": epoch,
    }


def _start(session, intent_id):  # type: ignore[no-untyped-def]
    intent = intent_payload(intent_id=intent_id)
    intent.update(
        name="survey_area", args={"area_id": "floor-1"}, selection=[GROUND_ID], confirm=True
    )
    session.process_intent(intent, Principal("console", None, CONSOLE_KEY))
    assert session.execute_pending_intent(intent_id)[-1]["status"] == "executing"


def _scan(event_id: str, mount_id: str = "rplidar") -> dict[str, object]:
    return _observation(
        event_id,
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
            "mount_id": mount_id,
        },
    )


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
    candidate_id = _candidate_id(SESSION, "survey-1", "survey-survey-1")
    candidate = lifecycle.candidates.load(candidate_id)
    assert candidate["pose_identity"]["event_id"] == "pose-1"
    assert candidate["scans"][0]["event_id"] == "scan-1"
    directory = tmp_path / "candidates" / candidate_id
    assert (directory / "recording" / "observations.jsonl").is_file()
    assert (directory / "occupancy" / "occupancy.png").is_file()
    assert (directory / "occupancy" / "manifest.json").is_file()
    assert (directory / "pose_path.json").is_file()
    assert (directory / "tag_candidates.json").is_file()
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


@pytest.mark.parametrize(
    "field,value",
    (("v", True), ("operation", []), ("connection_epoch", 2_147_483_648)),
)
def test_survey_lifecycle_parser_rejects_boolean_version_unhashable_operation_and_epoch(
    field, value
):
    request = _lifecycle("survey-1", "survey-survey-1", "complete", "event")
    request[field] = value
    with pytest.raises(ValueError):
        SurveyLifecycleRequest.parse(request)


def test_survey_rejects_wrong_mount_and_allows_cancel_after_expiry(tmp_path):
    session, lifecycle, adapter, clock = _session(tmp_path)
    _start(session, "survey-pinned")
    accepted = session.process_observation(_scan("wrong-mount", "other-lidar"), adapter)
    from relay.observations import Observation

    failed = lifecycle.accepted_observation(Observation.parse(accepted[0]))
    assert failed[0]["reason"] == "survey_source_changed"

    _start(session, "survey-cancel-expired")
    clock.advance(30 * 60 * 1_000 + 1)
    cancelled = session.process_frame(
        _lifecycle(
            "survey-cancel-expired",
            "survey-survey-cancel-expired",
            "cancel",
            "cancel-expired",
            t=clock(),
        ),
        Principal("console", None, CONSOLE_KEY),
    )
    assert cancelled[0]["status"] == "invalidated"


def test_completion_checks_deadline_and_public_lifecycle_requires_console(tmp_path):
    session, lifecycle, adapter, clock = _session(tmp_path)
    _start(session, "survey-deadline")
    from relay.observations import Observation

    accepted = session.process_observation(_scan("scan-deadline"), adapter)
    lifecycle.accepted_observation(Observation.parse(accepted[0]))
    clock.advance(30 * 60 * 1_000 + 1)
    expired = session.process_frame(
        _lifecycle("survey-deadline", "survey-survey-deadline", "complete", "deadline", t=clock()),
        Principal("console", None, CONSOLE_KEY),
    )
    assert expired[0]["reason"] == "survey_timeout"
    refused = session.process_survey_lifecycle(
        _lifecycle("anything", "anything", "cancel", "not-console"), adapter
    )
    assert refused[0]["reason"] == "source_not_allowed"


def test_candidate_reload_rejects_symlinked_artifact(tmp_path):
    session, lifecycle, adapter, _clock = _session(tmp_path)
    _start(session, "survey-reload")
    from relay.observations import Observation

    accepted = session.process_observation(_scan("scan-reload"), adapter)
    lifecycle.accepted_observation(Observation.parse(accepted[0]))
    session.process_frame(
        _lifecycle("survey-reload", "survey-survey-reload", "complete", "complete-reload"),
        Principal("console", None, CONSOLE_KEY),
    )
    candidate_id = _candidate_id(SESSION, "survey-reload", "survey-survey-reload")
    artifact = tmp_path / "candidates" / candidate_id / "occupancy" / "manifest.json"
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    artifact.unlink()
    os.symlink(outside, artifact)
    with pytest.raises(ValueError, match="artifact"):
        lifecycle.candidates.load(candidate_id)


def test_completion_removes_published_artifact_when_audit_commit_fails(tmp_path, monkeypatch):
    session, lifecycle, adapter, _clock = _session(tmp_path)
    _start(session, "survey-audit")
    from relay.observations import Observation

    accepted = session.process_observation(_scan("scan-audit"), adapter)
    lifecycle.accepted_observation(Observation.parse(accepted[0]))
    monkeypatch.setattr(
        session.audit_log,
        "append_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AuditLogError("disk failed")),
    )
    with pytest.raises(AuditLogError):
        session.process_frame(
            _lifecycle("survey-audit", "survey-survey-audit", "complete", "complete-audit"),
            Principal("console", None, CONSOLE_KEY),
        )
    candidate_id = _candidate_id(SESSION, "survey-audit", "survey-survey-audit")
    assert not (tmp_path / "candidates" / candidate_id).exists()
    assert "survey-audit" in lifecycle._runs


@pytest.mark.parametrize(
    "relative", ["recording/observations.jsonl", "occupancy/occupancy.png", "pose_path.json"]
)
@pytest.mark.parametrize("damage", ["changed", "missing", "fifo"])
def test_candidate_reload_refuses_damaged_evidence_without_blocking(tmp_path, relative, damage):
    from relay.observations import Observation

    session, lifecycle, adapter, _clock = _session(tmp_path)
    _start(session, "reload-evidence")
    accepted = session.process_observation(_scan("reload-scan"), adapter)
    lifecycle.accepted_observation(Observation.parse(accepted[0]))
    session.process_frame(
        _lifecycle("reload-evidence", "survey-reload-evidence", "complete", "complete"),
        Principal("console", None, CONSOLE_KEY),
    )
    candidate_id = _candidate_id(SESSION, "reload-evidence", "survey-reload-evidence")
    path = lifecycle.candidates.root / candidate_id / relative
    path.unlink()
    if damage == "changed":
        path.write_bytes(b"changed")
    elif damage == "fifo":
        os.mkfifo(path)
    with pytest.raises(ValueError, match="artifact"):
        lifecycle.candidates.load(candidate_id)


def test_maximum_length_intent_id_has_a_cancellable_run_id(tmp_path):
    session, lifecycle, _adapter, _clock = _session(tmp_path)
    intent_id = "s" * 128
    _start(session, intent_id)
    run = lifecycle._runs[intent_id]
    cancelled = session.process_frame(
        _lifecycle(intent_id, run.run_id, "cancel", "cancel-long"),
        Principal("console", None, CONSOLE_KEY),
    )
    assert cancelled[0]["status"] == "invalidated"


def test_websocket_survey_completion_publishes_rendered_occupancy_candidate(tmp_path):
    clock = MutableClock()
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={GROUND_ID: GROUND_KEY},
        log_dir=tmp_path,
        node_types={GROUND_ID: NodeType.GROUND},
        observation_configuration=_configuration(),
    )
    lifecycles: list[SurveyAreaLifecycle] = []

    def sink_factory(session):  # type: ignore[no-untyped-def]
        lifecycle = SurveyAreaLifecycle(session, SurveyCandidateRegistry(tmp_path / "candidates"))
        lifecycles.append(lifecycle)
        return _SurveySink(lifecycle)

    app = create_app(
        settings,
        clock=clock,
        intent_sink_factory=sink_factory,
        capability_profile=PROFILE,
    )
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"/ws/{SESSION}") as console,
            client.websocket_connect(f"/ws/{SESSION}") as adapter,
        ):
            console.send_json(
                {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
            )
            console.receive_json()
            console.receive_json()
            adapter.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "drone_id": GROUND_ID,
                    "token": GROUND_KEY.decode(),
                }
            )
            adapter.receive_json()
            adapter.receive_json()
            adapter.send_json(
                membership_payload(
                    action="join",
                    event_id="ws-join",
                    drone_id=GROUND_ID,
                    key=GROUND_KEY,
                    node_type="ground",
                    capabilities=["ground_drive"],
                )
            )
            adapter.send_json(
                _observation(
                    "ws-pose",
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
            )
            ready = {
                "v": 1,
                "t": clock(),
                "type": "membership",
                "event_id": "ws-ready",
                "session": SESSION,
                "drone_id": GROUND_ID,
                "action": "readiness",
                "connection_epoch": 1,
                "drive_authority": True,
                "safety_operator_present": True,
                "local_stop_ready": True,
                "heartbeat_ready": True,
                "pose_identity": {
                    "event_id": "ws-pose",
                    "session": SESSION,
                    "connection_epoch": 1,
                    "frame": "odom",
                },
            }
            ready["signature"] = sign_event(ready, GROUND_KEY)
            adapter.send_json(ready)
            for _ in range(40):
                event = console.receive_json()
                if (
                    event.get("type") == "state"
                    and event.get("drones")
                    and event["drones"][0].get("membership") == "ready"
                ):
                    break
            else:
                pytest.fail("websocket ground node did not become ready")
            console.send_json(
                {
                    **intent_payload(intent_id="ws-survey"),
                    "name": "survey_area",
                    "args": {"area_id": "floor-1"},
                    "selection": [GROUND_ID],
                    "confirm": True,
                }
            )
            for _ in range(40):
                event = console.receive_json()
                if event.get("intent_id") == "ws-survey" and event.get("status") == "executing":
                    break
            else:
                pytest.fail("websocket survey did not start")
            adapter.send_json(_scan("ws-scan"))
            for _ in range(40):
                event = console.receive_json()
                if event.get("event_id") == "ws-scan":
                    break
            else:
                pytest.fail("websocket scan was not accepted")
            console.send_json(
                _lifecycle("ws-survey", "survey-ws-survey", "complete", "ws-complete")
            )
            for _ in range(40):
                event = console.receive_json()
                if event.get("intent_id") == "ws-survey" and event.get("status") == "completed":
                    break
            else:
                pytest.fail("websocket survey did not complete")
    candidate_id = _candidate_id(SESSION, "ws-survey", "survey-ws-survey")
    candidate = lifecycles[0].candidates.load(candidate_id)
    assert candidate["occupancy"]["files"]["occupancy.png"]["bytes"] > 0
