import hashlib
import json
from pathlib import Path

import pytest

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
    for binding in provenance["bindings"]:
        payload = (output / binding["file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == binding["byte_sha256"]
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
