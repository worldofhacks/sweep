from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from media import configure

ROOT = Path(__file__).resolve().parents[1]


def _cameras() -> dict[str, object]:
    return {
        "11": [
            {"camera_id": "head", "label": "Head", "stream": "ground1"},
            {"camera_id": "rear", "label": "Rear", "stream": "ground1-rear"},
        ],
        "12": [
            {"camera_id": "head", "label": "Head", "stream": "ground2"},
            {"camera_id": "rear", "label": "Rear", "stream": "ground2-rear"},
        ],
    }


def _environment(cameras: object, streams: object = None) -> dict[str, str]:
    override = configure.compose_override(cameras, {} if streams is None else streams)
    return override["services"]["mediamtx"]["environment"]


def _base_accounts() -> list[tuple[str, str, list[tuple[str, str]]]]:
    # Read only the deliberately simple committed account stanza; assert its
    # entire shape so positional Compose overrides cannot silently drift.
    source = (ROOT / "media" / "mediamtx.yml").read_text()
    stanza = source.split("authInternalUsers:\n", 1)[1].split("\npaths:\n", 1)[0]
    stanza = re.sub(r"(?m)^\s*#.*\n", "", stanza)
    accounts = []
    for block in stanza.split("  - user: ")[1:]:
        name, remaining = block.split("\n", 1)
        match = re.fullmatch(r"    pass: (\S+)\n    permissions:\n(.+?)\s*", remaining, re.S)
        assert match is not None
        permissions = re.findall(r"      - action: (\w+)(?:\n        path: ([^\n]+))?", match[2])
        assert len(permissions) == match[2].count("- action:")
        accounts.append((name, match[1], permissions))
    return accounts


def _resolve(tmp_path: Path, override: dict[str, object], values: dict[str, str]) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is required for offline configuration resolution")
    extra = tmp_path / "compose.json"
    extra.write_text(json.dumps(override))
    # An explicit empty env file and allowlisted process environment prevent the
    # real deployment's .env or media credentials from entering test output.
    environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(extra),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment | values,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)["services"]["mediamtx"]


def test_compatible_accounts_keep_exact_path_scopes_and_stable_indices():
    accounts = _base_accounts()
    expected = [
        "drone1",
        "drone2",
        "drone3",
        "drone4",
        "sweep-reader",
        "sweep-api",
        "ground1",
        "ground2",
        "ground3",
        "ground4",
    ]
    assert [row[0] for row in accounts] == expected
    assert len(accounts) == configure.BASE_ACCOUNT_COUNT
    for name, password, permissions in accounts:
        assert password == configure.LOCKED_PASSWORD
        if name in configure.LEGACY_STREAMS:
            assert permissions == [("publish", name)]
    assert accounts[5][2] == [("api", "")]
    assert [action for action, _ in accounts[4][2]] == ["read", "playback"]
    for _, pattern in accounts[4][2]:
        for stream in configure.LEGACY_STREAMS:
            assert re.fullmatch(pattern[1:], stream)
        for stream in ("ground5", "ground1-rear", "drone1-other", "other"):
            assert re.fullmatch(pattern[1:], stream) is None


@pytest.mark.parametrize("configured_password", [None, "", "fixture-ground-password"])
def test_fresh_compose_restores_ground_publish_credentials_fail_closed(
    tmp_path, configured_password
):
    values = (
        {} if configured_password is None else {"SWEEP_MEDIA_GROUND1_PASSWORD": configured_password}
    )
    service = _resolve(tmp_path, {"services": {"mediamtx": {}}}, values)
    environment = service["environment"]
    assert environment["MTX_AUTHINTERNALUSERS_6_PASS"] == (
        configured_password or configure.LOCKED_PASSWORD
    )
    assert environment["MTX_AUTHINTERNALUSERS_7_PASS"] == configure.LOCKED_PASSWORD
    assert environment["MTX_AUTHINTERNALUSERS_0_PASS"] == configure.LOCKED_PASSWORD
    assert environment["MTX_PATHDEFAULTS_RECORD"] == "false"
    assert any(
        port.get("host_ip") == "127.0.0.1" and port["target"] == 9997 for port in service["ports"]
    )


def test_two_ground_cameras_resolve_independent_accounts_and_exact_reader_paths(tmp_path):
    service = _resolve(
        tmp_path,
        configure.compose_override(_cameras(), {}),
        {
            "SWEEP_MEDIA_STREAM_GROUND1_REAR_PASSWORD": "fixture-rear-password",
        },
    )
    environment = service["environment"]
    assert environment["MTX_AUTHINTERNALUSERS_10_USER"] == "ground1-rear"
    assert environment["MTX_AUTHINTERNALUSERS_10_PASS"] == "fixture-rear-password"
    assert environment["MTX_AUTHINTERNALUSERS_11_USER"] == "ground2-rear"
    assert environment["MTX_AUTHINTERNALUSERS_11_PASS"] == configure.LOCKED_PASSWORD
    for index in (10, 11):
        assert environment[f"MTX_AUTHINTERNALUSERS_{index}_PERMISSIONS_0_ACTION"] == "publish"
        assert (
            environment[f"MTX_AUTHINTERNALUSERS_{index}_PERMISSIONS_0_PATH"]
            == (environment[f"MTX_AUTHINTERNALUSERS_{index}_USER"])
        )
    for index in (0, 1):
        pattern = environment[f"MTX_AUTHINTERNALUSERS_4_PERMISSIONS_{index}_PATH"][1:]
        for stream in (*configure.LEGACY_STREAMS, "ground1-rear", "ground2-rear"):
            assert re.fullmatch(pattern, stream)
        for stream in ("ground3-rear", "ground1-rear-other", "unconfigured"):
            assert re.fullmatch(pattern, stream) is None
    assert environment["MTX_PATHDEFAULTS_RECORD"] == "false"


def test_new_ground_unit_is_additive_and_does_not_create_a_source():
    cameras = _cameras()
    before = _environment(cameras)
    cameras["15"] = [
        {"camera_id": "head", "label": "Head", "stream": "ground5"},
        {"camera_id": "rear", "label": "Rear", "stream": "ground5-rear"},
    ]
    after = _environment(cameras)
    assert {
        key: value for key, value in before.items() if "_4_PERMISSIONS_" not in key
    }.items() <= (after.items())
    assert after["MTX_AUTHINTERNALUSERS_12_USER"] == "ground5"
    assert after["MTX_AUTHINTERNALUSERS_13_USER"] == "ground5-rear"
    assert all(key.startswith("MTX_AUTHINTERNALUSERS_") for key in after)


def test_explicit_camera_list_overrides_legacy_and_empty_stays_empty():
    cameras = {"11": [], "12": _cameras()["12"]}
    resolved = configure.explicit_cameras(
        cameras, {"11": "ground1", "12": "legacy", "13": "ground3"}
    )
    assert resolved[11] == ()
    assert [camera.stream for camera in resolved[12]] == ["ground2", "ground2-rear"]
    assert [camera.stream for camera in resolved[13]] == ["ground3"]
    assert "legacy" not in json.dumps(configure.compose_override(cameras, {"12": "legacy"}))


@pytest.mark.parametrize(
    "stream",
    ["../camera", "camera?token=x", "~.*", "camera/foo", "any", "sweep-api", "sweep-reader"],
)
def test_invalid_or_reserved_stream_refuses_without_granting_permissions(stream):
    with pytest.raises(ValueError):
        _environment({"11": [{"camera_id": "head", "label": "Head", "stream": stream}]})


def test_duplicate_stream_and_normalized_credential_collision_refuse():
    for first, second in (("same", "same"), ("head-rear", "head_rear"), ("Head", "head")):
        cameras = {
            "11": [
                {"camera_id": "head", "label": "Head", "stream": first},
                {"camera_id": "rear", "label": "Rear", "stream": second},
            ]
        }
        with pytest.raises(ValueError):
            _environment(cameras)


def test_camera_count_and_fleet_bounds_are_shared():
    cameras = {
        str(device): [
            {
                "camera_id": f"camera{camera}",
                "label": "Camera",
                "stream": f"unit{device}-camera{camera}",
            }
            for camera in range(8)
        ]
        for device in range(1, 65)
    }
    assert len([key for key in _environment(cameras) if key.endswith("_USER")]) == 512
    cameras["65"] = []
    with pytest.raises(ValueError):
        _environment(cameras)
    with pytest.raises(ValueError):
        _environment({"11": cameras["11"] + cameras["12"][:1]})


def test_cli_writes_only_references_never_reads_credentials_and_refuses_replacement(
    tmp_path, capsys
):
    target = tmp_path / "compose.json"
    values = {
        "SWEEP_MEDIA_CAMERAS_JSON": json.dumps(_cameras()),
        "SWEEP_MEDIA_STREAM_GROUND1_REAR_PASSWORD": "must-not-be-copied",
        "SWEEP_RELAY_TOKEN": "must-not-be-read",
    }
    assert configure.main(["--output", str(target)], environ=values) == 0
    data = target.read_text()
    assert "must-not" not in data
    assert "${SWEEP_MEDIA_STREAM_GROUND1_REAR_PASSWORD:-" in data
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(SystemExit, match="2"):
        configure.main(["--output", str(target)], environ=values)
    assert target.read_text() == data
    assert "must-not" not in str(capsys.readouterr())


@pytest.mark.parametrize("raw", ['{"11":[],"11":[]}', '{"11":[', "[]", '"secret-fixture"'])
def test_cli_invalid_json_leaves_no_output(tmp_path, capsys, raw):
    target = tmp_path / "compose.json"
    with pytest.raises(SystemExit, match="2"):
        configure.main(["--output", str(target)], environ={"SWEEP_MEDIA_CAMERAS_JSON": raw})
    assert not target.exists()
    assert "secret-fixture" not in str(capsys.readouterr())
