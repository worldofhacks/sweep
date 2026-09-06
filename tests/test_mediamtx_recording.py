from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from media import recording

ROOT = Path(__file__).resolve().parents[1]


def _command(
    *args: str,
    timeout: float = 30,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def _required_or_skip(condition: bool, message: str) -> None:
    if condition:
        return
    if os.environ.get("SWEEP_REQUIRE_MEDIA_RECORDING_TEST") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _test_environment() -> dict[str, str]:
    inherited = set(
        "DOCKER_CONFIG DOCKER_CONTEXT DOCKER_HOST HOME LANG LC_ALL LOGNAME PATH TMPDIR USER "
        "XDG_CONFIG_HOME".split()
    )
    environment = {key: value for key, value in os.environ.items() if key in inherited}
    environment.update(
        {
            "SWEEP_MEDIA_HOST": "127.0.0.1",
            "SWEEP_MEDIA_DRONE1_PASSWORD": "recording-test-password",
            "SWEEP_MEDIA_DRONE2_PASSWORD": "recording-test-drone2",
            "SWEEP_MEDIA_DRONE3_PASSWORD": "recording-test-drone3",
            "SWEEP_MEDIA_DRONE4_PASSWORD": "recording-test-drone4",
            "SWEEP_MEDIA_READ_USERNAME": "sweep-reader",
            "SWEEP_MEDIA_READ_PASSWORD": "recording-test-reader",
            "SWEEP_MEDIA_API_USERNAME": "sweep-api",
            "SWEEP_MEDIA_API_PASSWORD": "recording-test-api",
        }
    )
    return environment


def _wait_for_compose_port(
    spec: recording.RunSpec,
    environment: dict[str, str],
    helper: subprocess.Popen[str],
) -> int:
    for _ in range(200):
        if helper.poll() is not None:
            stdout, stderr = helper.communicate()
            raise AssertionError(f"recording helper exited early: {stdout}\n{stderr}")
        result = _command(
            *recording._compose_command(spec, "port", "mediamtx", "8554"),
            environment=recording._compose_environment(spec, environment),
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            port = int(result.stdout.strip().rsplit(":", 1)[1])
            for _ in range(100):
                with socket.socket() as connection:
                    connection.settimeout(0.1)
                    if connection.connect_ex(("127.0.0.1", port)) == 0:
                        return port
                time.sleep(0.05)
        time.sleep(0.05)
    raise AssertionError("MediaMTX did not publish a dynamic RTSP port")


def _publisher(port: int, *, authenticated: bool, duration: str) -> list[str]:
    authority = "drone1:recording-test-password@" if authenticated else ""
    arguments = (
        "ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=160x90:rate=10 -t"
    ).split()
    return [
        *arguments,
        duration,
        *"-c:v libx264 -pix_fmt yuv420p -g 10 -f rtsp -rtsp_transport tcp".split(),
        f"rtsp://{authority}127.0.0.1:{port}/drone1",
    ]


def _spec(tmp_path: Path, *, run_id: str = "run-01", maximum: int = 1024**2) -> recording.RunSpec:
    recording_root = tmp_path / "recordings"
    export_root = tmp_path / "durable"
    recording_root.mkdir(exist_ok=True)
    export_root.mkdir(exist_ok=True)
    return recording.RunSpec(
        run_id=run_id,
        session_id="relay-session-01",
        recording_root=recording_root,
        export_root=export_root,
        max_bytes=maximum,
        min_free_bytes=1024,
        poll_interval=0.05,
    )


def _fixture_segments(spec: recording.RunSpec) -> list[dict[str, object]]:
    segment = spec.run_dir / "drone1" / "segment.mp4"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"fixture-segment")
    return [
        {
            "path": "drone1/segment.mp4",
            "size_bytes": segment.stat().st_size,
            "sha256": hashlib.sha256(segment.read_bytes()).hexdigest(),
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration_seconds": "1.000000",
        }
    ]


def test_recording_override_is_opt_in_exact_and_operator_owned(tmp_path: Path) -> None:
    compose_available = (
        shutil.which("docker") is not None
        and _command("docker", "compose", "version", check=False).returncode == 0
    )
    _required_or_skip(compose_available, "Docker Compose is required")
    environment = _test_environment()
    run_dir = tmp_path / "recordings" / "run-01"
    run_dir.mkdir(parents=True)
    environment.update(
        {
            "SWEEP_RECORDING_RUN_DIR": str(run_dir),
            "SWEEP_RECORDING_UID": str(os.getuid()),
            "SWEEP_RECORDING_GID": str(os.getgid()),
        }
    )
    default = json.loads(
        _command(
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "config",
            "--format",
            "json",
            environment=environment,
        ).stdout
    )
    resolved = json.loads(
        _command(
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.recording.yml",
            "config",
            "--format",
            "json",
            environment=environment,
        ).stdout
    )
    assert default["services"]["mediamtx"]["environment"]["MTX_PATHDEFAULTS_RECORD"] == "false"
    service = resolved["services"]["mediamtx"]
    assert service["image"] == recording.IMAGE_REF
    assert service["user"] == f"{os.getuid()}:{os.getgid()}"
    volume = next(item for item in service["volumes"] if item["target"] == "/recordings")
    assert volume["type"] == "bind"
    assert volume["source"] == str(run_dir)
    assert volume.get("bind", {}).get("create_host_path", False) is False
    assert (
        service["environment"]
        | {
            "MTX_PATHDEFAULTS_RECORD": "true",
            "MTX_PATHDEFAULTS_RECORDPATH": "/recordings/%path/%Y-%m-%d_%H-%M-%S-%f",
            "MTX_PATHDEFAULTS_RECORDFORMAT": "fmp4",
            "MTX_PATHDEFAULTS_RECORDPARTDURATION": "1s",
            "MTX_PATHDEFAULTS_RECORDSEGMENTDURATION": "1m",
            "MTX_PATHDEFAULTS_RECORDDELETEAFTER": "24h",
        }
        == service["environment"]
    )


def test_lock_follows_actual_compose_identity_across_recording_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording, "LOCK_ROOT", tmp_path)
    compose_available = (
        shutil.which("docker") is not None
        and _command("docker", "compose", "version", check=False).returncode == 0
    )
    _required_or_skip(compose_available, "Docker Compose is required")
    environment = _test_environment()
    first = _spec(tmp_path)
    other_root = tmp_path / "other-recordings"
    other_root.mkdir()
    second = recording.RunSpec(
        **{**first.__dict__, "recording_root": other_root, "run_id": "run-02"}
    )
    first_identity = recording._compose_identities(
        first, recording._compose_environment(first, environment)
    )
    second_identity = recording._compose_identities(
        second, recording._compose_environment(second, environment)
    )
    assert first_identity == second_identity
    with recording._lock(first_identity):
        with pytest.raises(recording.RecordingError, match="controls this MediaMTX"):
            with recording._lock(second_identity):
                pass

    override = tmp_path / "docker-compose.unique.yml"
    override.write_text("services:\n  mediamtx:\n    container_name: sweep-recording-unique\n")
    unique = recording.RunSpec(**{**second.__dict__, "extra_compose_files": (override,)})
    unique_environment = {**environment, "COMPOSE_PROJECT_NAME": "sweep-recording-unique"}
    unique_identity = recording._compose_identities(
        unique, recording._compose_environment(unique, unique_environment)
    )
    assert set(first_identity).isdisjoint(unique_identity)
    with recording._lock(first_identity), recording._lock(unique_identity):
        pass


def test_record_refuses_a_running_mediamtx_without_creating_or_stopping_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    stopped: list[bool] = []
    monkeypatch.setattr(recording, "_service_running", lambda *_: True)
    monkeypatch.setattr(recording, "_stop_service", lambda *_: stopped.append(True))

    with pytest.raises(recording.RecordingError, match="already running"):
        recording.record(spec)

    assert stopped == []
    assert not spec.run_dir.exists()


def test_prepare_refuses_reused_working_or_durable_run(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    recording._prepare(spec)
    with pytest.raises(recording.RecordingError, match="recording run already exists"):
        recording._prepare(spec)

    second = _spec(tmp_path, run_id="run-02")
    second.export_dir.mkdir()
    with pytest.raises(recording.RecordingError, match="durable run destination already exists"):
        recording._prepare(second)


def test_prepare_requires_external_mounted_export_root(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    missing = recording.RunSpec(
        **{**spec.__dict__, "export_root": tmp_path / "missing", "run_id": "missing-export"}
    )
    with pytest.raises(recording.RecordingError, match="must already exist"):
        recording._prepare(missing)

    inside_checkout = recording.RunSpec(
        **{**spec.__dict__, "export_root": ROOT / "media", "run_id": "inside-checkout"}
    )
    with pytest.raises(recording.RecordingError, match="outside the source checkout"):
        recording._prepare(inside_checkout)


def test_paths_reject_files_and_symbolic_link_components(tmp_path: Path) -> None:
    export_root = tmp_path / "durable"
    export_root.mkdir()
    file_root = tmp_path / "not-a-directory"
    file_root.write_text("not a directory")
    invalid = recording.RunSpec(
        run_id="run-01",
        session_id="session-01",
        recording_root=file_root,
        export_root=export_root,
        max_bytes=1024,
        min_free_bytes=1024,
        poll_interval=0.1,
    )
    with pytest.raises(recording.RecordingError, match="not a directory"):
        recording._prepare(invalid)

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    invalid = recording.RunSpec(
        **{**invalid.__dict__, "recording_root": linked, "run_id": "run-02"}
    )
    with pytest.raises(recording.RecordingError, match="symbolic-link components"):
        recording._prepare(invalid)


def test_limits_fail_closed_at_byte_and_free_space_boundaries() -> None:
    assert recording._limit_reason(99, 51, 100, 50) is None
    assert "byte budget" in recording._limit_reason(100, 51, 100, 50)
    assert "free-space reserve" in recording._limit_reason(99, 50, 100, 50)


def test_record_refuses_configuration_mutation_during_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    override = tmp_path / "docker-compose.mutated.yml"
    override.write_text("services:\n  mediamtx:\n    environment:\n      TEST_VALUE: before\n")
    spec = recording.RunSpec(**{**spec.__dict__, "extra_compose_files": (override,)})
    stopped: list[bool] = []

    monkeypatch.setattr(
        recording,
        "_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    )
    monkeypatch.setattr(
        recording,
        "_verify_image",
        lambda *_: override.write_text(
            "services:\n  mediamtx:\n    environment:\n      TEST_VALUE: after\n"
        ),
    )
    monkeypatch.setattr(recording, "_stop_service", lambda *_: stopped.append(True))

    with pytest.raises(recording.RecordingError, match="configuration changed during"):
        recording.record(spec)
    assert stopped == [True]
    assert spec.run_dir.is_dir()
    assert not spec.export_dir.exists()


def test_recording_tree_scan_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recording, "MAX_TREE_ENTRIES", 2)
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
    with pytest.raises(recording.RecordingError, match="exceeds 2 entries"):
        recording._tree_bytes(tmp_path)


def test_segment_validation_refuses_empty_invalid_and_unexpected_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(recording.RecordingError, match="zero finalized"):
        recording._segments(tmp_path)

    wrong = tmp_path / "drone5"
    wrong.mkdir()
    (wrong / "segment.mp4").write_bytes(b"not-video")
    with pytest.raises(recording.RecordingError, match="unexpected stream"):
        recording._segments(tmp_path)

    shutil.rmtree(wrong)
    calls: list[str] = []
    for stream, name in (("drone2", "b.mp4"), ("drone1", "a.mp4")):
        directory = tmp_path / stream
        directory.mkdir()
        (directory / name).write_bytes(name.encode())

    def probe(path: Path) -> tuple[str, str]:
        calls.append(path.relative_to(tmp_path).as_posix())
        return "mov,mp4,m4a,3gp,3g2,mj2", "1.000000"

    monkeypatch.setattr(recording, "_probe", probe)
    segments = recording._segments(tmp_path)
    assert calls == ["drone1/a.mp4", "drone2/b.mp4"]
    assert [item["path"] for item in segments] == calls


def test_segment_validation_uses_ffprobe_and_rejects_invalid_media(tmp_path: Path) -> None:
    _required_or_skip(shutil.which("ffprobe") is not None, "ffprobe is required")
    stream = tmp_path / "drone1"
    stream.mkdir()
    (stream / "invalid.mp4").write_bytes(b"not-an-mp4")
    with pytest.raises(recording.RecordingError, match="command failed .*ffprobe"):
        recording._segments(tmp_path)


@pytest.mark.parametrize("cross_filesystem", (False, True))
def test_export_manifest_is_canonical_atomic_and_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    if cross_filesystem:
        monkeypatch.setattr(recording, "_same_filesystem", lambda *_: False)
    destination = recording._export(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
    )
    assert destination == spec.export_dir
    assert not spec.run_dir.exists()
    manifest_path = destination / "recording-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest_path.read_bytes() == recording._canonical_json(manifest)
    assert manifest["run_id"] == spec.run_id
    assert manifest["session_id"] == spec.session_id
    assert manifest["stopped_at_utc"] == "2026-09-06T00:01:00.000Z"
    assert manifest["image"] == recording.IMAGE_REF
    assert not list(destination.parent.glob(f".{spec.run_id}.partial-*"))


def test_cross_filesystem_export_verifies_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    original_verify = recording._verify_copy

    def tamper_with_staging(root: Path, details: list[dict[str, object]], manifest: bytes) -> None:
        if root.parent == spec.export_root and root.name.startswith(f".{spec.run_id}.partial-"):
            (root / str(details[0]["path"])).write_bytes(b"same-size-tamper")
        original_verify(root, details, manifest)

    monkeypatch.setattr(recording, "_same_filesystem", lambda *_: False)
    monkeypatch.setattr(recording, "_verify_copy", tamper_with_staging)
    with pytest.raises(recording.RecordingError, match="does not match its manifest"):
        recording._export(
            spec,
            segments,
            started_at="2026-09-06T00:00:00.000Z",
            stopped_at="2026-09-06T00:01:00.000Z",
            stop_reason="operator",
        )
    assert spec.run_dir.is_dir()
    assert not spec.export_dir.exists()
    assert not list(spec.export_root.glob(f".{spec.run_id}.partial-*"))


@pytest.mark.parametrize(
    ("stop_mode", "maximum", "expected_code", "stop_reason"),
    (
        ("signal", 64 * 1024**2, 0, "operator"),
        ("budget", 10_000, 2, "safety_limit"),
        ("service", 64 * 1024**2, 2, "service_failure"),
        ("empty", 64 * 1024**2, 2, None),
    ),
)
def test_production_compose_lifecycle_records_and_archives_with_active_publisher(
    tmp_path: Path,
    stop_mode: str,
    maximum: int,
    expected_code: int,
    stop_reason: str | None,
) -> None:
    _required_or_skip(
        all(shutil.which(command) for command in ("docker", "ffmpeg", "ffprobe")),
        "Docker, ffmpeg, and ffprobe are required for the MediaMTX smoke test",
    )
    image = _command("docker", "image", "inspect", recording.IMAGE_REF, check=False, timeout=30)
    _required_or_skip(image.returncode == 0, f"pinned image is unavailable: {recording.IMAGE_REF}")

    environment = _test_environment()
    run_id = f"recording-{stop_mode}-{uuid.uuid4().hex[:12]}"
    project = f"sweep-recording-{uuid.uuid4().hex[:12]}"
    environment["COMPOSE_PROJECT_NAME"] = project
    container = f"{project}-mediamtx"
    override = tmp_path / "docker-compose.test.yml"
    override.write_text(
        "services:\n"
        "  mediamtx:\n"
        f"    container_name: {container}\n"
        "    ports: !override\n"
        '      - "127.0.0.1::8554"\n'
    )
    spec = recording.RunSpec(
        run_id=run_id,
        session_id="ci-session",
        recording_root=tmp_path / "recordings",
        export_root=tmp_path / "durable",
        max_bytes=maximum,
        min_free_bytes=1024,
        poll_interval=0.05,
        extra_compose_files=(override,),
    )
    spec.export_root.mkdir()
    helper: subprocess.Popen[str] | None = None
    publisher: subprocess.Popen[str] | None = None
    compose_environment = recording._compose_environment(spec, environment)
    try:
        command = [sys.executable, str(ROOT / "media" / "recording.py")]
        options = {
            "--run-id": spec.run_id,
            "--session-id": spec.session_id,
            "--recording-root": spec.recording_root,
            "--export-root": spec.export_root,
            "--max-bytes": spec.max_bytes,
            "--min-free-bytes": spec.min_free_bytes,
            "--poll-interval": spec.poll_interval,
            "--extra-compose-file": override,
        }
        for option, value in options.items():
            command.extend((option, str(value)))
        helper = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        port = _wait_for_compose_port(spec, environment, helper)
        unauthorized = _command(*_publisher(port, authenticated=False, duration="0.2"), check=False)
        assert unauthorized.returncode != 0
        assert "401 Unauthorized" in unauthorized.stderr

        if stop_mode == "empty":
            helper.send_signal(signal.SIGINT)
        else:
            publisher = subprocess.Popen(
                _publisher(port, authenticated=True, duration="30"),
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if stop_mode in {"service", "signal"}:
                for _ in range(200):
                    if any(path.stat().st_size > 0 for path in spec.run_dir.rglob("*.mp4")):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("MediaMTX did not begin a recording")
                assert publisher.poll() is None
            if stop_mode == "signal":
                helper.send_signal(signal.SIGINT)
            elif stop_mode == "service":
                _command("docker", "stop", "--time", "1", container)

        stdout, stderr = helper.communicate(timeout=45)
        assert helper.returncode == expected_code, f"{stdout}\n{stderr}"
        if stop_mode == "empty":
            assert "zero finalized MP4 segments" in stderr
            assert spec.run_dir.is_dir()
            assert not spec.export_dir.exists()
            assert not _command(
                *recording._compose_command(spec, "ps", "--all", "--quiet", "mediamtx"),
                environment=compose_environment,
            ).stdout.strip()
            return
        if stop_mode == "budget":
            assert "recording byte budget reached" in stderr
        elif stop_mode == "service":
            assert "MediaMTX stopped unexpectedly" in stderr
        assert publisher is not None
        publisher.communicate(timeout=15)
        assert publisher.returncode != 0

        manifest_path = spec.export_dir / "recording-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        assert manifest_path.read_bytes() == recording._canonical_json(manifest)
        assert manifest["run_id"] == run_id
        assert manifest["session_id"] == "ci-session"
        assert manifest["stop_reason"] == stop_reason
        assert manifest["image"] == recording.IMAGE_REF
        assert manifest["segments"]
        for segment_details in manifest["segments"]:
            segment = spec.export_dir / segment_details["path"]
            assert segment.stat().st_uid == os.getuid()
            assert segment.stat().st_size == segment_details["size_bytes"]
            assert recording._sha256(segment) == segment_details["sha256"]
        assert not spec.run_dir.exists()
        assert not _command(
            *recording._compose_command(spec, "ps", "--all", "--quiet", "mediamtx"),
            environment=compose_environment,
        ).stdout.strip()
    finally:
        if helper is not None and helper.poll() is None:
            helper.send_signal(signal.SIGTERM)
            helper.communicate(timeout=45)
        if publisher is not None and publisher.poll() is None:
            publisher.terminate()
            publisher.communicate(timeout=15)
        _command(
            *recording._compose_command(spec, "down", "--remove-orphans"),
            environment=compose_environment,
            check=False,
            timeout=45,
        )
        _command("docker", "rm", "--force", container, check=False, timeout=30)
