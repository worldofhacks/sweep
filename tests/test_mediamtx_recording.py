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
from types import SimpleNamespace

import pytest

from media import recording
from relay.control_localization_contracts import session_identifier as relay_session_identifier

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
            "video_stream": {"index": 0, "codec_name": "h264", "width": 160, "height": 90},
        }
    ]


def _make_media(path: Path, source: str, output: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _command(
        *"ffmpeg -hide_banner -loglevel error -f lavfi -i".split(),
        source,
        *output.split(),
        str(path),
    )


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
            "SWEEP_RECORDING_OWNER": "test-owner",
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
    assert service["labels"][recording.OWNER_LABEL] == "test-owner"
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


def test_lock_follows_actual_compose_identity_across_recording_roots(tmp_path: Path) -> None:
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


def test_record_refuses_an_existing_service_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    prepared: list[bool] = []
    stopped: list[bool] = []
    monkeypatch.setattr(
        recording,
        "_compose_container_id",
        lambda *args, **kwargs: "existing-container-id",
    )
    monkeypatch.setattr(recording, "_prepare", lambda *_: prepared.append(True))
    monkeypatch.setattr(recording, "_stop_service", lambda *_: stopped.append(True))

    with pytest.raises(recording.RecordingError, match="already exists"):
        recording.record(spec)

    assert prepared == []
    assert stopped == []
    assert not spec.run_dir.exists()


def test_record_never_recreates_or_stops_a_service_that_races_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    commands: list[list[str]] = []
    stopped: list[bool] = []
    container_ids = iter((None, "raced-container-id"))
    monkeypatch.setattr(
        recording, "_compose_container_id", lambda *args, **kwargs: next(container_ids)
    )

    def command(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(recording, "_command", command)
    monkeypatch.setattr(
        recording,
        "_owned_service",
        lambda *_, **__: (_ for _ in ()).throw(
            recording.RecordingError("MediaMTX container is not owned by this operation")
        ),
    )
    monkeypatch.setattr(recording, "_discover_owned_service", lambda *_: None)
    monkeypatch.setattr(recording, "_stop_service", lambda *_: stopped.append(True))

    with pytest.raises(recording.RecordingError, match="not owned"):
        recording.record(spec)

    startup = next(command for command in commands if "create" in command)
    assert "--no-recreate" in startup
    assert not any(command[:2] == ["docker", "start"] for command in commands)
    assert stopped == []
    assert spec.run_dir.is_dir()
    assert not spec.export_dir.exists()


def test_owned_service_cleanup_targets_only_the_immutable_container_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = recording.OwnedService("owned-container-id", "owner-token")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        recording,
        "_inspect_container",
        lambda *_: {"Config": {"Labels": {recording.OWNER_LABEL: "owner-token"}}},
    )

    def command(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(recording, "_command", command)
    recording._stop_service(service)
    assert commands == [
        ["docker", "stop", "--time", "20", "owned-container-id"],
        ["docker", "rm", "--force", "owned-container-id"],
    ]


def test_prepare_refuses_reused_working_or_durable_run(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    recording._prepare(spec).close()
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


def test_replaced_run_directory_cannot_bypass_byte_monitoring(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    prepared = recording._prepare(spec)
    original = spec.session_dir / "original-run-inode"
    try:
        spec.run_dir.rename(original)
        spec.run_dir.mkdir(mode=0o700)
        (original / "bytes-written-through-docker-bind").write_bytes(b"x" * 2048)

        with pytest.raises(recording.RecordingError, match="run directory identity changed"):
            recording._budget_status(spec, prepared)
        assert recording._tree_bytes(original) == 2048
        assert recording._tree_bytes(spec.run_dir) == 0
    finally:
        prepared.close()


def test_replaced_export_root_cannot_redirect_publication(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    prepared = recording._prepare(spec)
    segments = _fixture_segments(spec)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    original = tmp_path / "original-durable-inode"
    try:
        spec.export_root.rename(original)
        spec.export_root.mkdir(mode=0o700)
        with pytest.raises(recording.RecordingError, match="export root identity changed"):
            recording._export(spec, plan, prepared)
        assert not (spec.export_root / spec.run_id).exists()
        assert not (original / spec.run_id).exists()
        assert spec.run_dir.is_dir()
    finally:
        prepared.close()


def test_publication_writes_only_to_the_pinned_run_after_a_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    prepared = recording._prepare(spec)
    segments = _fixture_segments(spec)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    pinned_run = spec.session_dir / "pinned-run-inode"
    original_ensure = recording._ensure_publication_space

    def replace_after_space_check(*args: object, **kwargs: object) -> None:
        original_ensure(*args, **kwargs)  # type: ignore[arg-type]
        spec.run_dir.rename(pinned_run)
        spec.run_dir.mkdir(mode=0o700)

    monkeypatch.setattr(recording, "_ensure_publication_space", replace_after_space_check)
    try:
        with pytest.raises(recording.RecordingError, match="run directory identity changed"):
            recording._export(spec, plan, prepared)
        assert not (spec.run_dir / "recording-manifest.json").exists()
        assert (pinned_run / "recording-manifest.json").read_bytes() == plan.manifest
        assert not spec.export_dir.exists()
    finally:
        prepared.close()


def test_publication_cannot_report_success_after_the_export_root_is_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    prepared = recording._prepare(spec)
    segments = _fixture_segments(spec)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    hidden_root = tmp_path / "hidden-pinned-export-root"
    original_rename = recording.os.rename

    def rename_then_hide(source: object, destination: object, **kwargs: object) -> None:
        original_rename(source, destination, **kwargs)
        if kwargs.get("dst_dir_fd") == prepared.export_root.descriptor:
            original_rename(spec.export_root, hidden_root)
            spec.export_root.mkdir(mode=0o700)

    monkeypatch.setattr(recording.os, "rename", rename_then_hide)
    try:
        with pytest.raises(recording.RecordingError, match="export root identity changed"):
            recording._export(spec, plan, prepared)
        assert not spec.export_dir.exists()
        assert (hidden_root / spec.run_id / "recording-manifest.json").is_file()
    finally:
        prepared.close()


def test_limits_fail_closed_at_byte_and_free_space_boundaries() -> None:
    assert recording._limit_reason(99, 51, 100, 50) is None
    assert "byte budget" in recording._limit_reason(100, 51, 100, 50)
    assert "free-space reserve" in recording._limit_reason(99, 50, 100, 50)


def test_budget_status_reserves_cross_filesystem_export_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path, maximum=100)
    prepared = recording._prepare(spec)
    monkeypatch.setattr(recording, "_tree_usage_at", lambda *_: (99, 99))
    monkeypatch.setattr(recording, "_same_filesystem", lambda *_: False)
    monkeypatch.setattr(recording.PinnedDirectory, "block_size", lambda *_: 1)
    required = (
        99
        + recording.MAX_MANIFEST_BYTES
        + recording.ARCHIVE_METADATA_BLOCKS
        + 1
        + spec.min_free_bytes
    )
    durable_free = required

    def free(pin: recording.PinnedDirectory) -> int:
        return 10_000 if pin is prepared.run_dir else durable_free

    monkeypatch.setattr(recording.PinnedDirectory, "free_bytes", free)
    try:
        reasons, export_safe = recording._budget_status(spec, prepared)
        assert not export_safe
        assert reasons == (
            f"durable export requires more than {required} free bytes ({required} available)",
        )

        durable_free += 1
        assert recording._budget_status(spec, prepared) == ((), True)
    finally:
        prepared.close()


def test_parse_bounds_production_poll_interval(tmp_path: Path) -> None:
    arguments = "--run-id run-01 --session-id session-01 --export-root".split()
    arguments.extend((str(tmp_path), "--poll-interval"))
    assert recording._parse([*arguments, str(recording.MAX_POLL_INTERVAL)]).poll_interval == 1.0
    for invalid in (0, recording.MAX_POLL_INTERVAL + 0.001, float("inf"), float("nan")):
        with pytest.raises(recording.RecordingError, match="at most 1.0"):
            recording._parse([*arguments, str(invalid)])


@pytest.mark.parametrize(
    "value",
    (
        "a" * 128,
        "a" * 129,
        "a" * 512,
        "mission α / level-1",
    ),
)
def test_recording_accepts_the_exact_shared_relay_session_contract(
    tmp_path: Path, value: str
) -> None:
    assert relay_session_identifier(value) == value
    assert recording._validate_session_identifier(value) == value
    parsed = recording._parse(
        ["--run-id", "run-01", "--session-id", value, "--export-root", str(tmp_path)]
    )
    assert parsed.session_id == value
    assert parsed.session_dir.parent == parsed.recording_root
    assert parsed.session_dir.name.startswith("session-")
    assert "/" not in parsed.session_dir.name
    manifest = json.loads(
        recording._archive_plan(
            parsed,
            [],
            started_at="2026-09-06T00:00:00.000Z",
            stopped_at="2026-09-06T00:01:00.000Z",
            stop_reason="operator",
            elapsed_seconds=60,
            configuration={"fixture": "0" * 64},
        ).manifest
    )
    assert manifest["session_id"] == value


@pytest.mark.parametrize("value", ("a" * 513, " padded", "padded ", "line\nbreak"))
def test_recording_rejects_values_outside_the_shared_relay_session_contract(value: str) -> None:
    with pytest.raises(ValueError):
        relay_session_identifier(value)
    with pytest.raises(recording.RecordingError, match="canonical printable"):
        recording._validate_session_identifier(value)


def test_parse_bounds_duration_below_recording_retention(tmp_path: Path) -> None:
    arguments = [
        "--run-id",
        "run-01",
        "--session-id",
        "session-01",
        "--export-root",
        str(tmp_path),
        "--max-duration-seconds",
    ]
    assert (
        recording._parse([*arguments, str(recording.MAX_DURATION_SECONDS)]).max_duration_seconds
        == recording.MAX_DURATION_SECONDS
    )
    assert recording.MAX_DURATION_SECONDS < 24 * 60 * 60
    for invalid in (0, recording.MAX_DURATION_SECONDS + 0.001, float("inf"), float("nan")):
        with pytest.raises(recording.RecordingError, match="maximum duration"):
            recording._parse([*arguments, str(invalid)])


def test_record_enforces_the_duration_bound_before_any_docker_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _spec(tmp_path)
    spec = recording.RunSpec(
        **{**base.__dict__, "max_duration_seconds": recording.MAX_DURATION_SECONDS + 1}
    )
    docker_lookups: list[bool] = []
    monkeypatch.setattr(
        recording,
        "_compose_container_id",
        lambda *_args, **_kwargs: docker_lookups.append(True),
    )

    with pytest.raises(recording.RecordingError, match="maximum duration"):
        recording.record(spec)
    assert docker_lookups == []
    assert not spec.run_dir.exists()


@pytest.mark.parametrize(
    ("limited_after", "export_safe"), ((1, True), (2, True), (1, False), (2, False))
)
def test_record_rechecks_final_budget_and_never_reports_operator_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limited_after: int,
    export_safe: bool,
) -> None:
    spec = _spec(tmp_path)
    reason = "recording byte budget reached (100 >= 100)"
    if not export_safe:
        reason = "durable export requires more than 100 free bytes (100 available)"

    statuses = [((), True), ((), True)]
    statuses[limited_after - 1] = ((reason,), export_safe)
    monkeypatch.setattr(
        recording.threading,
        "Event",
        lambda: SimpleNamespace(set=lambda: None, wait=lambda _timeout: True),
    )
    monkeypatch.setattr(
        recording,
        "_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    )
    container_ids = iter((None, "container-id"))
    monkeypatch.setattr(
        recording, "_compose_container_id", lambda *args, **kwargs: next(container_ids)
    )
    monkeypatch.setattr(
        recording,
        "_owned_service",
        lambda container_id, token, **_kwargs: recording.OwnedService(container_id, token),
    )
    monkeypatch.setattr(recording, "_stop_service", lambda *_: None)
    monkeypatch.setattr(recording, "_budget_status", lambda *args, **kwargs: statuses.pop(0))
    monkeypatch.setattr(recording, "_segments", lambda *_: _fixture_segments(spec))

    expected = "recording byte budget" if export_safe else "durable export"
    with pytest.raises(recording.RecordingError, match=expected):
        recording.record(spec)
    assert not statuses
    if export_safe:
        manifest = json.loads((spec.export_dir / "recording-manifest.json").read_bytes())
        assert manifest["stop_reason"] == "safety_limit"
    else:
        assert spec.run_dir.is_dir()
        assert not spec.export_dir.exists()


def test_record_stops_at_the_monotonic_duration_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _spec(tmp_path)
    spec = recording.RunSpec(**{**base.__dict__, "max_duration_seconds": 10.0})
    container_ids = iter((None, "container-id"))
    clock = 100.0
    ownership_checkpoints: list[bool] = []

    def command(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal clock
        if arguments[:2] == ["docker", "start"]:
            clock = 111.0
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="")

    monkeypatch.setattr(recording, "_command", command)
    monkeypatch.setattr(
        recording, "_compose_container_id", lambda *args, **kwargs: next(container_ids)
    )

    def owned_service(
        container_id: str, token: str, *, expected_running: bool
    ) -> recording.OwnedService:
        ownership_checkpoints.append(expected_running)
        return recording.OwnedService(container_id, token)

    monkeypatch.setattr(recording, "_owned_service", owned_service)
    monkeypatch.setattr(recording, "_stop_service", lambda *_: None)
    monkeypatch.setattr(recording, "_budget_status", lambda *args, **kwargs: ((), True))
    monkeypatch.setattr(recording, "_segments", lambda *_: _fixture_segments(spec))
    monkeypatch.setattr(recording.time, "monotonic", lambda: clock)

    with pytest.raises(recording.RecordingError, match="duration budget reached"):
        recording.record(spec)

    manifest = json.loads((spec.export_dir / "recording-manifest.json").read_bytes())
    assert manifest["stop_reason"] == "safety_limit"
    assert manifest["elapsed_seconds"] == 11
    assert manifest["limits"]["max_duration_seconds"] == 10
    assert ownership_checkpoints == [False, True]


def test_catchable_signal_during_finalization_does_not_interrupt_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    original_handler = signal.getsignal(signal.SIGTERM)
    received: list[bool] = []
    container_ids = iter((None, "container-id"))
    monkeypatch.setattr(
        recording.threading,
        "Event",
        lambda: SimpleNamespace(set=lambda: received.append(True), wait=lambda _timeout: True),
    )
    monkeypatch.setattr(
        recording,
        "_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    )
    monkeypatch.setattr(
        recording, "_compose_container_id", lambda *args, **kwargs: next(container_ids)
    )
    monkeypatch.setattr(
        recording,
        "_owned_service",
        lambda container_id, token, **_kwargs: recording.OwnedService(container_id, token),
    )
    monkeypatch.setattr(recording, "_stop_service", lambda *_: None)
    monkeypatch.setattr(recording, "_budget_status", lambda *args, **kwargs: ((), True))

    def segments_during_signal(
        _run_dir: Path, _prepared: recording.PreparedRun
    ) -> list[dict[str, object]]:
        assert signal.getsignal(signal.SIGTERM) is not original_handler
        os.kill(os.getpid(), signal.SIGTERM)
        return _fixture_segments(spec)

    monkeypatch.setattr(recording, "_segments", segments_during_signal)
    archive = recording.record(spec)
    assert archive == spec.export_dir
    assert archive.is_dir()
    assert received == [True]
    assert signal.getsignal(signal.SIGTERM) is original_handler


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
    container_ids = iter((None, "container-id"))
    monkeypatch.setattr(
        recording, "_compose_container_id", lambda *args, **kwargs: next(container_ids)
    )

    def mutate_configuration(
        container_id: str, token: str, **_kwargs: object
    ) -> recording.OwnedService:
        override.write_text("services:\n  mediamtx:\n    environment:\n      TEST_VALUE: after\n")
        return recording.OwnedService(container_id, token)

    monkeypatch.setattr(recording, "_owned_service", mutate_configuration)
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

    def probe(_descriptor: int, path: Path) -> tuple[str, str, dict[str, object]]:
        calls.append(path.relative_to(tmp_path).as_posix())
        return (
            "mov,mp4,m4a,3gp,3g2,mj2",
            "1.000000",
            {"index": 0, "codec_name": "h264", "width": 160, "height": 90},
        )

    monkeypatch.setattr(recording, "_probe_descriptor", probe)
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


def test_segment_validation_rejects_audio_only_mp4(tmp_path: Path) -> None:
    _required_or_skip(
        all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), "FFmpeg is required"
    )
    path = tmp_path / "drone1" / "audio-only.mp4"
    _make_media(path, "sine=frequency=1000:sample_rate=8000", "-t 0.2 -c:a aac")
    with pytest.raises(recording.RecordingError, match="contains no video stream"):
        recording._segments(tmp_path)


def test_segment_validation_rejects_undecodable_video(tmp_path: Path) -> None:
    _required_or_skip(
        all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), "FFmpeg is required"
    )
    path = tmp_path / "drone1" / "corrupt.mp4"
    _make_media(path, "testsrc=size=160x90:rate=10", "-t 0.5 -c:v libx264 -pix_fmt yuv420p")
    payload = bytearray(path.read_bytes())
    marker = payload.find(b"mdat")
    assert marker > 4
    box_start = marker - 4
    box_size = int.from_bytes(payload[box_start:marker], "big")
    assert box_size > 8 and box_start + box_size <= len(payload)
    payload[marker + 4 : box_start + box_size] = bytes(box_size - 8)
    path.write_bytes(payload)

    with pytest.raises(
        recording.RecordingError, match="command failed .*ffmpeg|no decodable frame"
    ):
        recording._segments(tmp_path)


def test_segment_validation_rejects_corruption_after_a_decodable_first_gop(
    tmp_path: Path,
) -> None:
    _required_or_skip(
        all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), "FFmpeg is required"
    )
    path = tmp_path / "drone1" / "tail-corrupt.mp4"
    _make_media(
        path,
        "testsrc2=size=640x360:rate=30",
        "-t 5 -c:v libx264 -pix_fmt yuv420p -g 30",
    )
    payload = bytearray(path.read_bytes())
    marker = payload.find(b"mdat")
    assert marker > 4
    box_start = marker - 4
    box_size = int.from_bytes(payload[box_start:marker], "big")
    data_start = marker + 4
    if box_size == 1:
        box_size = int.from_bytes(payload[marker + 4 : marker + 12], "big")
        data_start = marker + 12
    box_end = box_start + box_size
    overwrite_start = data_start + (box_end - data_start) // 2
    payload[overwrite_start:box_end] = bytes(box_end - overwrite_start)
    path.write_bytes(payload)

    with pytest.raises(recording.RecordingError, match="command failed .*ffmpeg"):
        recording._segments(tmp_path)


def test_probe_accepts_exact_media_bounds_and_fully_decodes_with_a_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], float]] = []

    def command(
        arguments: list[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, timeout))
        if arguments[0] == "ffprobe":
            output = json.dumps(
                {
                    "format": {
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        "duration": str(recording.MAX_SEGMENT_DURATION_SECONDS),
                    },
                    "streams": [
                        {
                            "index": 7,
                            "codec_type": "video",
                            "codec_name": "h264",
                            "width": 7680,
                            "height": 4320,
                        }
                    ],
                }
            )
        elif "framehash" in arguments:
            output = "# format: frame checksums\n0, 0, 0, 1, 1, deadbeef\n"
        else:
            output = ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(recording, "_command", command)
    path = tmp_path / "bounded.mp4"
    path.write_bytes(b"fixture")
    facts = recording._probe(path)

    assert facts[2] == {
        "index": 7,
        "codec_name": "h264",
        "width": 7680,
        "height": 4320,
    }
    full_decode, timeout = calls[-1]
    assert full_decode[:5] == ["ffmpeg", "-nostdin", "-v", "error", "-xerror"]
    assert full_decode[-3:] == ["-f", "null", "-"]
    assert "-frames:v" not in full_decode
    assert timeout == recording.MAX_DECODE_SECONDS


@pytest.mark.parametrize(
    ("duration", "width", "height", "message"),
    (
        (recording.MAX_SEGMENT_DURATION_SECONDS + 0.001, 160, 90, "duration exceeds"),
        (recording.MAX_SEGMENT_DURATION_SECONDS, 7681, 4320, "invalid metadata"),
    ),
)
def test_probe_rejects_media_bounds_at_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration: float,
    width: int,
    height: int,
    message: str,
) -> None:
    probe = json.dumps(
        {
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": str(duration),
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                }
            ],
        }
    )
    monkeypatch.setattr(
        recording,
        "_command",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(arguments, 0, probe, ""),
    )
    path = tmp_path / "over-bound.mp4"
    path.write_bytes(b"fixture")

    with pytest.raises(recording.RecordingError, match=message):
        recording._probe(path)


@pytest.mark.parametrize("complete_archive", (False, True))
def test_publication_reserve_accounts_for_manifest_files_and_allocation_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_archive: bool,
) -> None:
    spec = _spec(tmp_path)
    prepared = recording._prepare(spec)
    segments = _fixture_segments(spec)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    block_size = 4096
    if complete_archive:
        allocation = recording._archive_allocation(plan, block_size)
        file_blocks = recording._allocated_size(len(plan.manifest), block_size)
        file_blocks += sum(
            recording._allocated_size(int(segment["size_bytes"]), block_size)
            for segment in plan.segments
        )
        assert (
            allocation - file_blocks
            == (
                2
                + len({str(segment["path"]).split("/", 1)[0] for segment in plan.segments})
                + len(plan.segments)
                + recording.ARCHIVE_METADATA_BLOCKS
            )
            * block_size
        )
    else:
        allocation = recording._allocated_size(len(plan.manifest), block_size)
        allocation += (recording.ARCHIVE_METADATA_BLOCKS + 2) * block_size
    assert allocation > sum(int(segment["size_bytes"]) for segment in segments) + len(plan.manifest)
    free = spec.min_free_bytes + allocation
    monkeypatch.setattr(recording.PinnedDirectory, "block_size", lambda *_: block_size)
    monkeypatch.setattr(recording.PinnedDirectory, "free_bytes", lambda *_: free)
    try:
        with pytest.raises(recording.RecordingError, match="publication requires more than"):
            recording._ensure_publication_space(
                spec, plan, prepared, complete_archive=complete_archive
            )
        free += 1
        recording._ensure_publication_space(spec, plan, prepared, complete_archive=complete_archive)
    finally:
        prepared.close()


def test_manifest_size_bound_accepts_exact_and_rejects_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    baseline = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    monkeypatch.setattr(recording, "MAX_MANIFEST_BYTES", len(baseline.manifest))
    assert len(
        recording._archive_plan(
            spec,
            segments,
            started_at="2026-09-06T00:00:00.000Z",
            stopped_at="2026-09-06T00:01:00.000Z",
            stop_reason="operator",
            elapsed_seconds=60,
        ).manifest
    ) == len(baseline.manifest)
    monkeypatch.setattr(recording, "MAX_MANIFEST_BYTES", len(baseline.manifest) - 1)
    with pytest.raises(recording.RecordingError, match="manifest exceeds"):
        recording._archive_plan(
            spec,
            segments,
            started_at="2026-09-06T00:00:00.000Z",
            stopped_at="2026-09-06T00:01:00.000Z",
            stop_reason="operator",
            elapsed_seconds=60,
        )


@pytest.mark.parametrize("cross_filesystem", (False, True))
def test_export_manifest_is_canonical_atomic_and_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cross_filesystem: bool
) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    if cross_filesystem:
        monkeypatch.setattr(recording, "_same_filesystem", lambda *_: False)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    destination = recording._export(spec, plan)
    assert destination == spec.export_dir
    assert not spec.run_dir.exists()
    manifest_path = destination / "recording-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest_path.read_bytes() == recording._canonical_json(manifest)
    assert manifest["run_id"] == spec.run_id
    assert manifest["session_id"] == spec.session_id
    assert manifest["stopped_at_utc"] == "2026-09-06T00:01:00.000Z"
    assert manifest["elapsed_seconds"] == 60
    assert manifest["limits"]["max_duration_seconds"] == spec.max_duration_seconds
    assert manifest["image"] == recording.IMAGE_REF
    assert not list(destination.parent.glob(f".{spec.run_id}.partial-*"))


def test_export_refuses_unmanifested_directories(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    (spec.run_dir / "rogue-empty-directory").mkdir()
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )

    with pytest.raises(recording.RecordingError, match="does not exactly match"):
        recording._export(spec, plan)
    assert spec.run_dir.is_dir()
    assert not spec.export_dir.exists()


def test_cross_filesystem_export_verifies_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    segments = _fixture_segments(spec)
    original_verify = recording._verify_copy_at

    def tamper_with_staging(
        root_descriptor: int,
        details: tuple[dict[str, object], ...],
        manifest: bytes,
    ) -> None:
        staging = next(spec.export_root.glob(f".{spec.run_id}.partial-*"))
        (staging / str(details[0]["path"])).write_bytes(b"same-size-tamper")
        original_verify(root_descriptor, details, manifest)

    monkeypatch.setattr(recording, "_same_filesystem", lambda *_: False)
    monkeypatch.setattr(recording, "_verify_copy_at", tamper_with_staging)
    plan = recording._archive_plan(
        spec,
        segments,
        started_at="2026-09-06T00:00:00.000Z",
        stopped_at="2026-09-06T00:01:00.000Z",
        stop_reason="operator",
        elapsed_seconds=60,
    )
    with pytest.raises(recording.RecordingError, match="does not match its manifest"):
        recording._export(spec, plan)
    assert spec.run_dir.is_dir()
    assert not spec.export_dir.exists()
    assert not list(spec.export_root.glob(f".{spec.run_id}.partial-*"))


def test_production_helper_preserves_a_preexisting_mediamtx_service(tmp_path: Path) -> None:
    _required_or_skip(shutil.which("docker") is not None, "Docker is required")
    image = _command("docker", "image", "inspect", recording.IMAGE_REF, check=False, timeout=30)
    _required_or_skip(image.returncode == 0, f"pinned image is unavailable: {recording.IMAGE_REF}")

    environment = _test_environment()
    project = f"sweep-existing-{uuid.uuid4().hex[:12]}"
    environment["COMPOSE_PROJECT_NAME"] = project
    container = f"{project}-mediamtx"
    override = tmp_path / "docker-compose.existing.yml"
    override.write_text(
        "services:\n"
        "  mediamtx:\n"
        f"    container_name: {container}\n"
        "    ports: !override\n"
        '      - "127.0.0.1::8554"\n'
    )
    normal_compose = [
        "docker",
        "compose",
        "-f",
        str(recording.BASE_COMPOSE),
        "-f",
        str(override),
    ]
    spec = recording.RunSpec(
        run_id="must-not-start",
        session_id="existing-session",
        recording_root=tmp_path / "recordings",
        export_root=tmp_path / "durable",
        max_bytes=64 * 1024**2,
        min_free_bytes=1024,
        poll_interval=0.05,
        extra_compose_files=(override,),
    )
    spec.export_root.mkdir()
    try:
        _command(
            *normal_compose, "up", "-d", "--pull", "missing", "mediamtx", environment=environment
        )
        original_id = _command(
            *normal_compose, "ps", "--quiet", "mediamtx", environment=environment
        ).stdout.strip()
        assert original_id
        helper = _command(
            sys.executable,
            str(ROOT / "media" / "recording.py"),
            "--run-id",
            spec.run_id,
            "--session-id",
            spec.session_id,
            "--recording-root",
            str(spec.recording_root),
            "--export-root",
            str(spec.export_root),
            "--max-bytes",
            str(spec.max_bytes),
            "--min-free-bytes",
            str(spec.min_free_bytes),
            "--extra-compose-file",
            str(override),
            environment=environment,
            check=False,
        )
        assert helper.returncode == 2
        assert "already exists" in helper.stderr
        current_id = _command(
            *normal_compose, "ps", "--quiet", "mediamtx", environment=environment
        ).stdout.strip()
        assert current_id == original_id
        assert (
            _command(
                "docker", "inspect", original_id, "--format", "{{.State.Running}}"
            ).stdout.strip()
            == "true"
        )
        assert not spec.run_dir.exists()
    finally:
        _command(
            *normal_compose,
            "down",
            "--remove-orphans",
            environment=environment,
            check=False,
            timeout=45,
        )
        _command("docker", "rm", "--force", container, check=False, timeout=30)


@pytest.mark.parametrize(
    ("stop_mode", "maximum", "expected_code", "stop_reason"),
    (
        ("signal", 64 * 1024**2, 0, "operator"),
        ("sighup", 64 * 1024**2, 0, "sighup"),
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
            if stop_mode in {"service", "signal", "sighup"}:
                for _ in range(200):
                    if any(path.stat().st_size > 0 for path in spec.run_dir.rglob("*.mp4")):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("MediaMTX did not begin a recording")
                assert publisher.poll() is None
            if stop_mode == "signal":
                helper.send_signal(signal.SIGINT)
            elif stop_mode == "sighup":
                helper.send_signal(signal.SIGHUP)
            elif stop_mode == "service":
                _command("docker", "stop", "--time", "1", container)

        stdout, stderr = helper.communicate(timeout=45)
        assert helper.returncode == expected_code, f"{stdout}\n{stderr}"
        events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
        started = next(event for event in events if event["event"] == "recording_started")
        assert isinstance(started["container_id"], str)
        assert len(started["container_id"]) >= 12
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
            assert segment_details["video_stream"]["codec_name"] == "h264"
            assert segment_details["video_stream"]["width"] == 160
            assert segment_details["video_stream"]["height"] == 90
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
