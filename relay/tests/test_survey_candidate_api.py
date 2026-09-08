"""Real artifact handoff over authenticated HTTP, using isolated canonical scan evidence."""

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.auth import Principal
from relay.observations import Observation
from relay.platform import PlatformServices
from relay.settings import RelaySettings
from relay.survey_area import SurveyCandidateRegistry, SurveyLifecycleError, _candidate_id
from relay.tests.conftest import CONSOLE_KEY, SESSION
from relay.tests.test_survey_area import _lifecycle, _scan, _session, _start

HEADERS = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}


def completed_candidate(tmp_path):
    session, lifecycle, adapter, _clock = _session(tmp_path / "recorder")
    lifecycle.candidates = SurveyCandidateRegistry(tmp_path / "runtime" / "survey_candidates")
    _start(session, "preview-survey")
    accepted = session.process_observation(_scan("preview-scan"), adapter)
    lifecycle.accepted_observation(Observation.parse(accepted[0]))
    result = session.process_frame(
        _lifecycle("preview-survey", "survey-preview-survey", "complete", "finish-preview"),
        Principal("console", None, CONSOLE_KEY),
    )
    candidate_id = _candidate_id(SESSION, "preview-survey", "survey-preview-survey")
    assert result[0]["result"] == {
        "candidate_id": candidate_id,
        "run_id": "survey-preview-survey",
        "connection_epoch": 1,
    }
    return lifecycle.candidates, candidate_id


def make_app(tmp_path):
    return create_app(
        RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path / "runtime"),
        platform_services_factory=lambda runtime: PlatformServices(runtime, environment={}),
    )


def connect(socket):
    socket.send_json({"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()})
    assert socket.receive_json()["type"] == "auth.accepted"
    assert socket.receive_json()["type"] == "state"


def test_completed_candidate_http_returns_exact_local_evidence_and_never_commands(tmp_path):
    registry, candidate_id = completed_candidate(tmp_path)
    with (
        TestClient(make_app(tmp_path)) as client,
        client.websocket_connect(f"/ws/{SESSION}") as socket,
    ):
        connect(socket)
        url = f"/api/sessions/{SESSION}/survey-candidates/{candidate_id}"
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = client.get(url, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        preview = response.json()
        assert set(preview) == {
            "v",
            "type",
            "candidate_id",
            "session",
            "intent_id",
            "run_id",
            "device_id",
            "connection_epoch",
            "area_id",
            "source",
            "pose_identity",
            "occupancy",
            "files",
            "image",
            "navigation_authority",
        }
        assert preview["type"] == "survey_candidate_preview"
        assert preview["navigation_authority"] is False
        assert preview["occupancy"]["grid"]["registered_to_world"] is False
        assert preview["occupancy"]["grid"]["frame"] == preview["source"]["odom_frame"] == "odom"
        assert preview["pose_identity"]["event_id"] == "pose-1"
        assert preview["occupancy"]["observations"]["records"] == 1
        image = base64.b64decode(preview["image"]["data_base64"], validate=True)
        assert image == (registry.root / candidate_id / "occupancy" / "occupancy.png").read_bytes()
        assert preview["image"]["mime_type"] == "image/png"
        assert {key: preview["image"][key] for key in ("bytes", "sha256")} == {
            "bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
        }
        assert (
            client.get("/metrics", headers=HEADERS).json()["sessions"][SESSION]["commands_issued"]
            == 0
        )


def test_candidate_http_is_session_scoped_and_returns_typed_missing_errors(tmp_path):
    _registry, candidate_id = completed_candidate(tmp_path)
    with (
        TestClient(make_app(tmp_path)) as client,
        client.websocket_connect("/ws/other-session") as socket,
    ):
        connect(socket)
        base = "/api/sessions/other-session/survey-candidates"
        missing = client.get(f"{base}/{candidate_id}", headers=HEADERS)
        assert missing.status_code == 409
        assert missing.json()["code"] == "survey_candidate_missing"
        assert missing.headers["cache-control"] == "no-store"
        invalid = client.get(f"{base}/not-a-candidate", headers=HEADERS)
        assert invalid.status_code == 409
        assert invalid.json()["code"] == "invalid_candidate_id"
        absent = client.get(
            f"/api/sessions/not-connected/survey-candidates/{candidate_id}", headers=HEADERS
        )
        assert absent.status_code == 409


@pytest.mark.parametrize(
    "damage", ["image", "metadata", "session", "identity", "missing-field", "symlink"]
)
def test_candidate_preview_refuses_changed_evidence(tmp_path, damage):
    registry, candidate_id = completed_candidate(tmp_path)
    directory = registry.root / candidate_id
    manifest_path = directory / "candidate.json"
    manifest = json.loads(manifest_path.read_bytes())
    if damage == "image":
        (directory / "occupancy" / "occupancy.png").write_bytes(b"changed")
    elif damage == "metadata":
        manifest["occupancy"]["grid"]["registered_to_world"] = True
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "session":
        manifest["session"] = "other-session"
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "identity":
        manifest["run_id"] = "different-run"
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "missing-field":
        del manifest["pose_identity"]
        manifest_path.write_text(json.dumps(manifest))
    else:
        image = directory / "occupancy" / "occupancy.png"
        outside = tmp_path / "outside.png"
        image.rename(outside)
        image.symlink_to(outside)
    with pytest.raises(SurveyLifecycleError):
        registry.preview(SESSION, candidate_id)


def test_preview_rechecks_artifact_after_inventory_validation(tmp_path, monkeypatch):
    registry, candidate_id = completed_candidate(tmp_path)
    load = registry.load

    def replaced(candidate_id):
        candidate = load(candidate_id)
        (registry.root / candidate_id / "occupancy" / "occupancy.png").write_bytes(b"changed")
        return candidate

    monkeypatch.setattr(registry, "load", replaced)
    with pytest.raises(SurveyLifecycleError, match="artifact changed"):
        registry.preview(SESSION, candidate_id)


def test_preview_has_an_independent_metadata_response_bound(tmp_path, monkeypatch):
    registry, candidate_id = completed_candidate(tmp_path)
    monkeypatch.setattr("relay.survey_area.MAX_SURVEY_PREVIEW_METADATA_BYTES", 1)
    with pytest.raises(SurveyLifecycleError) as error:
        registry.preview(SESSION, candidate_id)
    assert error.value.code == "survey_candidate_too_large"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("v",), True),
        (("device_id",), 12),
        (("connection_epoch",), 2),
        (("area_id",), " "),
        (("intent_id",), "preview-survey "),
        (("source", "source_id"), "another-lidar"),
        (("source", "mount_id"), "another-mount"),
        (("source", "odom_frame"), "world"),
        (("source", "lidar_frame"), "other-lidar-frame"),
        (("source", "source_id"), ""),
        (("pose_identity", "event_id"), "another-pose"),
        (("pose_identity", "source_id"), "another-pose-source"),
        (("pose_identity", "session"), "another-session"),
        (("pose_identity", "connection_epoch"), True),
        (("pose_identity", "frame"), "world"),
    ],
)
def test_preview_headers_must_match_independent_recording_provenance(tmp_path, path, value):
    registry, candidate_id = completed_candidate(tmp_path)
    manifest_path = registry.root / candidate_id / "candidate.json"
    manifest = json.loads(manifest_path.read_bytes())
    target = manifest
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SurveyLifecycleError) as error:
        registry.preview(SESSION, candidate_id)
    assert error.value.code == "invalid_candidate"


def replace_occupancy(registry, candidate_id, transform):
    directory = registry.root / candidate_id
    manifest_path = directory / "candidate.json"
    manifest = json.loads(manifest_path.read_bytes())
    occupancy_path = directory / "occupancy" / "manifest.json"
    occupancy = json.loads(occupancy_path.read_bytes())
    transform(occupancy)
    encoded = json.dumps(occupancy).encode()
    occupancy_path.write_bytes(encoded)
    manifest["occupancy"] = occupancy
    manifest["files"]["occupancy/manifest.json"] = {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("format",), "unknown-grid-format"),
        (("input", "run_id"), "other-run"),
        (("input", "source", "device_id"), 12),
        (("input", "source", "connection_epoch"), True),
        (("input", "source", "session"), "other-session"),
        (("input", "source", "source_id"), "other-source"),
        (("input", "observations_sha256"), "0" * 64),
        (("mount_id",), "other-mount"),
        (("grid", "frame"), "world"),
        (("grid", "registered_to_world"), True),
    ],
)
def test_preview_rejects_inconsistent_occupancy_identity_even_with_matching_file_digest(
    tmp_path, path, value
):
    registry, candidate_id = completed_candidate(tmp_path)

    def update(occupancy):
        target = occupancy
        for name in path[:-1]:
            target = target[name]
        target[path[-1]] = value

    replace_occupancy(registry, candidate_id, update)
    with pytest.raises(SurveyLifecycleError) as error:
        registry.preview(SESSION, candidate_id)
    assert error.value.code == "invalid_candidate"


@pytest.mark.parametrize("field", ["candidate.json", "occupancy/manifest.json"])
@pytest.mark.parametrize("invalid_number", ["NaN", "Infinity", "1e309"])
def test_preview_metadata_numbers_are_finite(tmp_path, field, invalid_number):
    registry, candidate_id = completed_candidate(tmp_path)
    directory = registry.root / candidate_id
    path = directory / field
    encoded = path.read_bytes().rstrip()[:-1] + f',"invalid_number":{invalid_number}}}'.encode()
    path.write_bytes(encoded)
    if field != "candidate.json":
        manifest_path = directory / "candidate.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"][field] = {
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SurveyLifecycleError) as error:
        registry.preview(SESSION, candidate_id)
    assert error.value.code == "invalid_candidate"


def test_preview_occupancy_metadata_rejects_duplicate_keys(tmp_path):
    registry, candidate_id = completed_candidate(tmp_path)
    directory = registry.root / candidate_id
    occupancy_path = directory / "occupancy" / "manifest.json"
    encoded = occupancy_path.read_bytes().rstrip()[:-1] + b',"format":"duplicate"}'
    occupancy_path.write_bytes(encoded)
    manifest_path = directory / "candidate.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["occupancy/manifest.json"] = {
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SurveyLifecycleError) as error:
        registry.preview(SESSION, candidate_id)
    assert error.value.code == "invalid_candidate"
