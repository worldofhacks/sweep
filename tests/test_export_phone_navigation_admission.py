import hashlib
import json
import os
from pathlib import Path

import pytest

import tools.export_phone_navigation_admission as admission_exporter
from planner.test_navigation import FIXTURE
from planner.test_navigation_deployment import _flight_deployment_files, deployment_files
from relay.auth import verify_event_signature
from tools.export_phone_navigation_admission import export_phone_navigation_admission
from tools.map_geometry import generate


def _key(path: Path) -> Path:
    path.write_bytes(b"phone-navigation-provenance-key-0123456789")
    path.chmod(0o600)
    return path


def test_exported_admission_binds_the_exact_six_approved_artifact_bytes(tmp_path):
    deployment, _, _ = _flight_deployment_files(tmp_path)
    output = tmp_path / "phone-admission"
    manifest = export_phone_navigation_admission(deployment, 1, _key(tmp_path / "key"), output)

    assert set(manifest) == {
        "v",
        "enabled",
        "navigation_config_id",
        "navigation_config_sha256",
        "map_version",
        "map_sha256",
        "geometry_sha256",
        "camera_calibration_sha256",
        "body_extrinsics_sha256",
        "world_transform_sha256",
        "control_source_ids",
        "clock_lease_id",
        "clock_lease_expires_at_ms",
        "max_authorization_lifetime_ms",
        "provenance",
    }
    provenance = manifest["provenance"]
    assert provenance["session"] == "flight-session"
    assert [binding["kind"] for binding in provenance["bindings"]] == [
        "navigation_config",
        "map",
        "geometry",
        "camera_calibration",
        "body_extrinsics",
        "world_transform",
    ]
    unsigned = {name: value for name, value in provenance.items() if name != "signature"}
    key = (tmp_path / "key").read_bytes()
    assert verify_event_signature(unsigned, provenance["signature"], key)
    bindings = {binding["kind"]: binding for binding in provenance["bindings"]}
    for binding in bindings.values():
        payload = (output / binding["file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == binding["byte_sha256"]
    assert bindings["map"]["semantic_sha256"] == manifest["map_sha256"]
    assert bindings["map"]["byte_sha256"] != bindings["map"]["semantic_sha256"]
    assert bindings["geometry"]["semantic_sha256"] == bindings["geometry"]["byte_sha256"]
    assert json.loads((output / "navigation-admission.json").read_text()) == manifest


def test_export_rejects_a_simulation_deployment_and_nonprivate_key(tmp_path):
    geometry = tmp_path / "geometry"
    accepted = json.loads((FIXTURE / "accepted_versions.json").read_text())
    generate(FIXTURE, FIXTURE / "geometry_authoring.json", geometry, accepted)
    deployment = deployment_files(tmp_path, (FIXTURE, geometry, accepted))
    key = _key(tmp_path / "key")
    with pytest.raises(ValueError, match="flight deployment"):
        export_phone_navigation_admission(deployment, 1, key, tmp_path / "output")

    deployment, _, _ = _flight_deployment_files(tmp_path / "flight")
    key.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        export_phone_navigation_admission(deployment, 1, key, tmp_path / "other-output")


def test_export_uses_no_follow_private_key_and_leaves_no_partial_output(tmp_path, monkeypatch):
    deployment, _, _ = _flight_deployment_files(tmp_path)
    key = _key(tmp_path / "key")
    key_link = tmp_path / "key-link"
    os.symlink(key, key_link)
    with pytest.raises(ValueError, match="opened safely"):
        export_phone_navigation_admission(deployment, 1, key_link, tmp_path / "linked-output")

    output = tmp_path / "atomic-output"
    def fail_publish(*_):
        raise OSError("no")

    monkeypatch.setattr(admission_exporter, "_replace_directory", fail_publish)
    with pytest.raises(OSError, match="no"):
        export_phone_navigation_admission(deployment, 1, key, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".atomic-output.*"))


def test_export_refuses_a_provenance_key_replaced_after_its_fd_was_opened(tmp_path, monkeypatch):
    key = _key(tmp_path / "key")
    replacement = _key(tmp_path / "replacement")
    original = admission_exporter._read_descriptor

    def replace_path(descriptor, name, maximum, **kwargs):
        value = original(descriptor, name, maximum, **kwargs)
        if kwargs.get("private_key"):
            os.replace(replacement, key)
        return value

    monkeypatch.setattr(admission_exporter, "_read_descriptor", replace_path)
    with pytest.raises(ValueError, match="changed while it was read"):
        admission_exporter._private_key(key)


def test_export_revalidates_after_copying_its_artifact_snapshot(tmp_path, monkeypatch):
    deployment, _, _ = _flight_deployment_files(tmp_path)
    key = _key(tmp_path / "key")
    original = admission_exporter._artifact_bytes

    def mutate_after_snapshot(active_deployment, device_id):
        snapshot = original(active_deployment, device_id)
        tuning = active_deployment.path.parent / "device-1-navigation.json"
        tuning.write_text("{}")
        return snapshot

    monkeypatch.setattr(admission_exporter, "_artifact_bytes", mutate_after_snapshot)
    with pytest.raises(ValueError, match="navigation artifact inputs changed"):
        export_phone_navigation_admission(deployment, 1, key, tmp_path / "output")
