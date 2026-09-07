from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from media import configure, recording


def provisioning():
    return {
        "cameras": {
            "15": [
                {"camera_id": "front", "label": "Front", "stream": "ground5"},
                {"camera_id": "rear", "label": "Rear", "stream": "ground5_rear"},
            ],
            "25": [{"camera_id": "primary", "label": "Camera", "stream": "drone5"}],
        },
        "publishers": [
            {
                "user": "ground5",
                "password": "robot-media-only-secret-32-bytes-long",
                "streams": ["ground5", "ground5_rear"],
            },
            {
                "user": "drone5",
                "password": "drone-media-only-secret-32-bytes-long",
                "streams": ["drone5"],
            },
        ],
        "reader": {"user": "operator", "password": "reader-media-only-secret-32-bytes-long"},
        "api": {"user": "relay", "password": "api-media-only-secret-32-bytes-long"},
        "webrtc_hosts": ["127.0.0.1"],
    }


def test_provisioned_paths_and_permissions_are_exact_and_additive() -> None:
    config = configure.configuration(provisioning())
    assert set(config["paths"]) == {"ground5", "ground5_rear", "drone5"}
    accounts = config["authInternalUsers"]
    assert accounts[0]["permissions"] == [
        {"action": "publish", "path": stream} for stream in ("ground5", "ground5_rear")
    ]
    assert accounts[-1]["permissions"] == [{"action": "api"}]
    assert accounts[-2]["permissions"] == [
        {"action": action, "path": path}
        for path in sorted(config["paths"])
        for action in ("read", "playback")
    ]
    assert config["pathDefaults"] == {"record": False}
    assert "all_others" not in config["paths"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_publisher",
        "extra_stream",
        "duplicate_publisher",
        "duplicate_user",
        "anonymous_user",
        "short_password",
        "host_url",
        "unknown_field",
    ],
)
def test_provisioning_rejects_ambiguous_or_unconfigured_permissions(mutation: str) -> None:
    raw = provisioning()
    if mutation == "missing_publisher":
        raw["publishers"].pop()
    elif mutation == "extra_stream":
        raw["publishers"][0]["streams"].append("ground6")
    elif mutation == "duplicate_publisher":
        raw["publishers"].append(raw["publishers"][0])
    elif mutation == "duplicate_user":
        raw["reader"]["user"] = raw["api"]["user"]
    elif mutation == "anonymous_user":
        raw["reader"]["user"] = "any"
    elif mutation == "short_password":
        raw["api"]["password"] = "short"
    elif mutation == "host_url":
        raw["webrtc_hosts"] = ["https://host/private"]
    else:
        raw["unknown"] = True
    with pytest.raises(ValueError):
        configure.configuration(raw)


def test_bundle_is_private_standalone_and_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text(json.dumps(provisioning()))
    bundle = tmp_path / "private-bundle"
    configure.write_bundle(source, bundle)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    for file in bundle.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    compose = json.loads((bundle / "compose.json").read_text())
    service = compose["services"]["mediamtx"]
    assert service["image"] == recording.IMAGE_REF
    assert service["container_name"] == "sweep-mediamtx"
    assert service["environment"] == {"MTX_PATHDEFAULTS_RECORD": "false"}
    assert "127.0.0.1:9997:9997" in service["ports"]
    assert service["volumes"][0]["source"] == str(bundle / "mediamtx.json")
    assert not service["volumes"][0]["bind"]["create_host_path"]
    assert provisioning()["reader"]["password"] not in (bundle / "compose.json").read_text()
    with pytest.raises(ValueError):
        configure.write_bundle(source, bundle)


def test_provisioning_cli_never_echoes_credentials_on_failure(tmp_path: Path, capsys) -> None:
    source = tmp_path / "input.json"
    secret = provisioning()["reader"]["password"]
    source.write_text(secret)
    assert (
        configure.main(["--provisioning", str(source), "--output-dir", str(tmp_path / "bundle")])
        == 2
    )
    output = capsys.readouterr()
    assert secret not in output.err + output.out
    assert not (tmp_path / "bundle").exists()


def _spec(tmp_path: Path):
    return recording.RunSpec(
        "run",
        "session",
        tmp_path / "recordings",
        tmp_path / "exports",
        1024**3,
        0,
        0.5,
        streams=frozenset({"ground5", "ground5_rear", "drone5"}),
    )


def test_recording_accepts_only_the_explicit_additional_camera_paths(
    tmp_path: Path, monkeypatch
) -> None:
    spec = _spec(tmp_path)
    for stream in spec.streams:
        directory = spec.run_dir / stream
        directory.mkdir(parents=True)
        (directory / "segment.mp4").write_bytes(b"bounded-test-media")
    monkeypatch.setattr(
        recording, "_probe_descriptor", lambda *_: ("mp4", 1.0, {"codec_type": "video"})
    )
    segments = recording._segments(spec.run_dir, streams=spec.streams)
    assert {segment["path"].split("/")[0] for segment in segments} == spec.streams
    with pytest.raises(recording.RecordingError, match="unexpected stream path"):
        recording._segments(spec.run_dir, streams=frozenset({"ground5"}))
    for invalid in (
        frozenset(),
        frozenset({"../camera"}),
        frozenset(f"cam{i}" for i in range(513)),
    ):
        with pytest.raises(recording.RecordingError):
            replace(spec, streams=invalid)


def test_recording_cli_uses_generated_configuration_and_stream_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text(json.dumps(provisioning()))
    bundle = tmp_path / "bundle"
    configure.write_bundle(source, bundle)
    args = [
        "--run-id",
        "run",
        "--session-id",
        "session",
        "--export-root",
        str(tmp_path / "export"),
        "--base-compose-file",
        str(bundle / "compose.json"),
        "--media-config",
        str(bundle / "mediamtx.json"),
        "--stream",
        "ground5",
        "--stream",
        "ground5_rear",
    ]
    spec = recording._parse(args)
    assert spec.streams == frozenset({"ground5", "ground5_rear"})
    assert spec.compose_files[0] == bundle / "compose.json"
    assert spec.media_config == bundle / "mediamtx.json"
    assert recording._configuration(spec)["mediamtx"] == recording._sha256(spec.media_config)
    with pytest.raises(recording.RecordingError, match="unique"):
        recording._parse([*args, "--stream", "ground5"])


def test_custom_recording_requires_both_configs_and_an_explicit_stream_list(tmp_path: Path) -> None:
    common = [
        "--run-id",
        "run",
        "--session-id",
        "session",
        "--export-root",
        str(tmp_path / "export"),
    ]
    with pytest.raises(recording.RecordingError, match="both"):
        recording._parse([*common, "--base-compose-file", str(recording.BASE_COMPOSE)])
    with pytest.raises(recording.RecordingError, match="explicit stream"):
        recording._parse(
            [
                *common,
                "--base-compose-file",
                str(recording.BASE_COMPOSE),
                "--media-config",
                str(recording.MEDIA_CONFIG),
            ]
        )
