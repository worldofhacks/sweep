from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
RECORDING_COMPOSE = ROOT / "docker-compose.recording.yml"
MEDIA_CONFIG = ROOT / "media" / "mediamtx.yml"

IMAGE_DIGEST = "sha256:1b029d11049be75630e9b73bb0d5f47b08a7db4eaee89a80bf8f53bc40e56414"
IMAGE_REF = f"bluenviron/mediamtx:1.20.1@{IMAGE_DIGEST}"
DEFAULT_MAX_BYTES = 20 * 1024**3
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3
MAX_SEGMENTS = 10_000
ALLOWED_STREAMS = {f"drone{index}" for index in range(1, 5)}
MAX_TREE_ENTRIES = MAX_SEGMENTS + len(ALLOWED_STREAMS) + 1
LOCK_PATH = Path("/tmp/sweep-mediamtx-recording.lock")


class RecordingError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    session_id: str
    recording_root: Path
    export_root: Path
    max_bytes: int
    min_free_bytes: int
    poll_interval: float
    extra_compose_files: tuple[Path, ...] = ()

    @property
    def session_dir(self) -> Path:
        return self.recording_root / self.session_id

    @property
    def run_dir(self) -> Path:
        return self.session_dir / self.run_id

    @property
    def export_dir(self) -> Path:
        return self.export_root / self.run_id

    @property
    def compose_files(self) -> tuple[Path, ...]:
        return (BASE_COMPOSE, RECORDING_COMPOSE, *self.extra_compose_files)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configuration(spec: RunSpec) -> dict[str, str]:
    values = {"mediamtx": _sha256(MEDIA_CONFIG)}
    values.update(
        {
            f"compose[{index}]:{path.name}": _sha256(path)
            for index, path in enumerate(spec.compose_files)
        }
    )
    return values


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _absolute_no_symlinks(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RecordingError(f"{label} must not contain symbolic-link components: {current}")
    return absolute


def _validate_identifier(value: str, label: str) -> str:
    if not (1 <= len(value) <= 128 and value[0].isalnum() and value.isascii()):
        raise RecordingError(f"{label} must be a canonical 1-128 character ASCII identifier")
    if any(character not in "._-" and not character.isalnum() for character in value):
        raise RecordingError(f"{label} contains an unsupported character")
    return value


def _prepare(spec: RunSpec) -> None:
    _absolute_no_symlinks(spec.recording_root, "recording root")
    _absolute_no_symlinks(spec.export_root, "export root")
    if spec.recording_root.exists() and not spec.recording_root.is_dir():
        raise RecordingError(f"recording root is not a directory: {spec.recording_root}")
    spec.recording_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not spec.export_root.is_dir():
        raise RecordingError(
            f"durable export root must already exist and be mounted: {spec.export_root}"
        )
    if spec.export_root.is_relative_to(ROOT):
        raise RecordingError("durable export root must be outside the source checkout")
    if spec.export_root.is_relative_to(spec.recording_root) or spec.recording_root.is_relative_to(
        spec.export_root
    ):
        raise RecordingError("recording and durable export roots must not contain each other")
    if os.path.lexists(spec.run_dir):
        raise RecordingError(f"recording run already exists: {spec.run_dir}")
    if os.path.lexists(spec.export_dir):
        raise RecordingError(f"durable run destination already exists: {spec.export_dir}")

    required = spec.max_bytes + spec.min_free_bytes
    for label, root in (("recording", spec.recording_root), ("durable export", spec.export_root)):
        if shutil.disk_usage(root).free < required:
            raise RecordingError(f"{label} filesystem requires at least {required} free bytes")
    if os.path.lexists(spec.session_dir) and (
        spec.session_dir.is_symlink() or not spec.session_dir.is_dir()
    ):
        raise RecordingError(f"recording session path is not a directory: {spec.session_dir}")
    spec.session_dir.mkdir(exist_ok=True, mode=0o700)
    try:
        spec.run_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise RecordingError(f"recording run already exists: {spec.run_dir}") from exc


@contextmanager
def _lock() -> Iterator[None]:
    import fcntl

    try:
        descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise RecordingError(f"could not open recording lock: {LOCK_PATH}") from exc
    with os.fdopen(descriptor, "a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecordingError("another recording operation is active") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _compose_command(spec: RunSpec, *arguments: str) -> list[str]:
    command = ["docker", "compose"]
    for compose_file in spec.compose_files:
        command.extend(("-f", str(compose_file)))
    return [*command, *arguments]


def _compose_environment(spec: RunSpec, base: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **(os.environ if base is None else base),
        "SWEEP_RECORDING_RUN_DIR": str(spec.run_dir),
        "SWEEP_RECORDING_UID": str(os.getuid()),
        "SWEEP_RECORDING_GID": str(os.getgid()),
    }


def _command(
    command: list[str], *, timeout: float, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or ""
        detail = detail.strip()[-1000:]
        raise RecordingError(
            f"command failed ({' '.join(command)})" + (f": {detail}" if detail else "")
        ) from exc


def _verify_image(spec: RunSpec, environment: dict[str, str]) -> None:
    container_id = _command(
        _compose_command(spec, "ps", "--quiet", "mediamtx"),
        timeout=20,
        environment=environment,
    ).stdout.strip()
    if not container_id or "\n" in container_id:
        raise RecordingError("could not identify exactly one running MediaMTX container")
    configured_image = _command(
        ["docker", "inspect", container_id, "--format", "{{.Config.Image}}"], timeout=20
    ).stdout.strip()
    if configured_image != IMAGE_REF:
        raise RecordingError("running MediaMTX container does not use the pinned image reference")


def _service_running(spec: RunSpec, environment: dict[str, str]) -> bool:
    result = _command(
        _compose_command(spec, "ps", "--status", "running", "--quiet", "mediamtx"),
        timeout=20,
        environment=environment,
    )
    return bool(result.stdout.strip())


def _stop_service(spec: RunSpec, environment: dict[str, str]) -> None:
    for arguments, timeout in (
        (("stop", "--timeout", "20", "mediamtx"), 45),
        (("rm", "--force", "mediamtx"), 30),
    ):
        _command(_compose_command(spec, *arguments), timeout=timeout, environment=environment)


def _tree(root: Path, label: str) -> Iterator[Path]:
    count = 0
    for path in root.rglob("*"):
        count += 1
        if count > MAX_TREE_ENTRIES:
            raise RecordingError(f"{label} exceeds {MAX_TREE_ENTRIES} entries")
        if path.is_symlink():
            raise RecordingError(f"{label} contains a symbolic link: {path}")
        yield path


def _tree_bytes(run_dir: Path) -> int:
    return sum(path.stat().st_size for path in _tree(run_dir, "recording tree") if path.is_file())


def _limit_reason(used: int, free: int, maximum: int, reserve: int) -> str | None:
    if used >= maximum:
        return f"recording byte budget reached ({used} >= {maximum})"
    if free <= reserve:
        return f"recording free-space reserve reached ({free} <= {reserve})"
    return None


def _probe(path: Path) -> tuple[str, str]:
    arguments = "ffprobe -v error -show_entries format=format_name,duration -of json".split()
    result = _command(
        [*arguments, str(path)],
        timeout=30,
    )
    try:
        details = json.loads(result.stdout)["format"]
        format_name, duration = details["format_name"], details["duration"]
        parsed_duration = float(duration)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecordingError(f"ffprobe returned invalid metadata for {path}") from exc
    if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
        raise RecordingError(f"recording is not an fMP4-compatible segment: {path}")
    if not math.isfinite(parsed_duration) or parsed_duration <= 0:
        raise RecordingError(f"recording has no positive finite duration: {path}")
    return format_name, duration


def _segments(run_dir: Path) -> list[dict[str, object]]:
    paths: list[Path] = []
    for path in _tree(run_dir, "recording tree"):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir)
        if path.suffix.lower() != ".mp4":
            raise RecordingError(f"unexpected file in recording run: {relative.as_posix()}")
        if len(relative.parts) != 2 or relative.parts[0] not in ALLOWED_STREAMS:
            raise RecordingError(f"unexpected stream path: {relative.as_posix()}")
        paths.append(path)
    if not paths:
        raise RecordingError("recording run contains zero finalized MP4 segments")
    if len(paths) > MAX_SEGMENTS:
        raise RecordingError(f"recording run exceeds {MAX_SEGMENTS} segments")

    result: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda value: value.relative_to(run_dir).as_posix()):
        size = path.stat().st_size
        if size <= 0:
            raise RecordingError(f"recording segment is empty: {path}")
        format_name, duration = _probe(path)
        result.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size_bytes": size,
                "sha256": _sha256(path),
                "format_name": format_name,
                "duration_seconds": duration,
            }
        )
    return result


def _verify_copy(root: Path, segments: list[dict[str, object]], manifest: bytes) -> None:
    expected = {"recording-manifest.json", *(str(segment["path"]) for segment in segments)}
    found: set[str] = set()
    for path in _tree(root, "exported run"):
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    if found != expected:
        raise RecordingError("exported run files do not exactly match the manifest")

    manifest_path = root / "recording-manifest.json"
    if manifest_path.read_bytes() != manifest:
        raise RecordingError("exported manifest bytes changed during copy")
    for segment in segments:
        path = root / str(segment["path"])
        if (
            not path.is_file()
            or path.stat().st_size != segment["size_bytes"]
            or _sha256(path) != segment["sha256"]
        ):
            raise RecordingError(f"exported segment does not match its manifest: {path}")


def _fsync_tree(root: Path) -> None:
    directories = {root}
    for path in _tree(root, "recording tree"):
        if path.is_file():
            with path.open("rb") as source:
                os.fsync(source.fileno())
        elif path.is_dir():
            directories.add(path)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)


def _same_filesystem(source: Path, destination: Path) -> bool:
    return os.stat(source).st_dev == os.stat(destination).st_dev


def _export(
    spec: RunSpec,
    segments: list[dict[str, object]],
    *,
    started_at: str,
    stopped_at: str,
    stop_reason: str,
    configuration: dict[str, str] | None = None,
) -> Path:
    manifest = _canonical_json(
        {
            "schema": "sweep.media.recording-manifest.v1",
            "run_id": spec.run_id,
            "session_id": spec.session_id,
            "started_at_utc": started_at,
            "stopped_at_utc": stopped_at,
            "stop_reason": stop_reason,
            "image": IMAGE_REF,
            "configuration_sha256": configuration or _configuration(spec),
            "limits": {"max_bytes": spec.max_bytes, "min_free_bytes": spec.min_free_bytes},
            "segments": segments,
        }
    )
    _atomic_write(spec.run_dir / "recording-manifest.json", manifest)
    _verify_copy(spec.run_dir, segments, manifest)
    if os.path.lexists(spec.export_dir):
        raise RecordingError(f"durable run destination already exists: {spec.export_dir}")

    if _same_filesystem(spec.run_dir, spec.export_root):
        _fsync_tree(spec.run_dir)
        spec.run_dir.rename(spec.export_dir)
        _fsync_directory(spec.export_root)
        _fsync_directory(spec.session_dir)
        return spec.export_dir

    staging = spec.export_root / f".{spec.run_id}.partial-{uuid.uuid4().hex}"
    try:
        shutil.copytree(spec.run_dir, staging, copy_function=shutil.copy2)
        _verify_copy(staging, segments, manifest)
        _fsync_tree(staging)
        staging.rename(spec.export_dir)
        _fsync_directory(spec.export_root)
        shutil.rmtree(spec.run_dir)
        _fsync_directory(spec.session_dir)
        return spec.export_dir
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def record(spec: RunSpec) -> Path:
    environment = _compose_environment(spec)
    if _service_running(spec, environment):
        raise RecordingError("MediaMTX is already running; stop it before starting a recording run")
    _prepare(spec)
    started_at = _utc_now()
    configuration = _configuration(spec)
    stop_requested = threading.Event()
    previous = {
        handled: signal.signal(handled, lambda _signum, _frame: stop_requested.set())
        for handled in (signal.SIGINT, signal.SIGTERM)
    }

    failure: str | None = None
    stop_reason = "operator"
    try:
        _command(
            _compose_command(spec, "up", "-d", "--pull", "missing", "mediamtx"),
            timeout=180,
            environment=environment,
        )
        _verify_image(spec, environment)
        if not _service_running(spec, environment):
            raise RecordingError("MediaMTX did not remain running after startup")
        event = {"event": "recording_started", "run_id": spec.run_id, "session_id": spec.session_id}
        print(json.dumps(event, sort_keys=True), flush=True)
        next_service_check = time.monotonic()
        while not stop_requested.wait(spec.poll_interval):
            failure = _limit_reason(
                _tree_bytes(spec.run_dir),
                shutil.disk_usage(spec.run_dir).free,
                spec.max_bytes,
                spec.min_free_bytes,
            )
            if failure:
                stop_reason = "safety_limit"
                break
            if time.monotonic() >= next_service_check:
                if not _service_running(spec, environment):
                    failure = "MediaMTX stopped unexpectedly"
                    stop_reason = "service_failure"
                    break
                next_service_check = time.monotonic() + 2
    finally:
        try:
            _stop_service(spec, environment)
        finally:
            for handled, handler in previous.items():
                signal.signal(handled, handler)

    stopped_at = _utc_now()
    segments = _segments(spec.run_dir)
    archive = _export(
        spec,
        segments,
        started_at=started_at,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        configuration=configuration,
    )
    print(json.dumps({"event": "recording_exported", "path": str(archive)}, sort_keys=True))
    if failure:
        raise RecordingError(f"{failure}; stopped safely and exported to {archive}")
    return archive


def _parse(argv: list[str] | None) -> RunSpec:
    parser = argparse.ArgumentParser(description="Run and preserve a bounded MediaMTX recording")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--recording-root", type=Path, default=ROOT / "recordings")
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--extra-compose-file", action="append", type=Path, default=[])
    arguments = parser.parse_args(argv)
    if arguments.max_bytes <= 0 or arguments.min_free_bytes <= 0:
        raise RecordingError("byte budget and free-space reserve must be greater than zero")
    if not math.isfinite(arguments.poll_interval) or arguments.poll_interval <= 0:
        raise RecordingError("poll interval must be finite and greater than zero")
    extra_files = tuple(
        _absolute_no_symlinks(path, "extra Compose file") for path in arguments.extra_compose_file
    )
    if any(not path.is_file() for path in extra_files):
        raise RecordingError("an extra Compose file is missing or not a regular file")
    return RunSpec(
        run_id=_validate_identifier(arguments.run_id, "run id"),
        session_id=_validate_identifier(arguments.session_id, "session id"),
        recording_root=_absolute_no_symlinks(arguments.recording_root, "recording root"),
        export_root=_absolute_no_symlinks(arguments.export_root, "export root"),
        max_bytes=arguments.max_bytes,
        min_free_bytes=arguments.min_free_bytes,
        poll_interval=arguments.poll_interval,
        extra_compose_files=extra_files,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        if sys.platform not in {"darwin", "linux"}:
            raise RecordingError("recording operations support macOS and Linux only")
        for tool in ("docker", "ffprobe"):
            if shutil.which(tool) is None:
                raise RecordingError(f"required program is unavailable: {tool}")
        spec = _parse(argv)
        with _lock():
            record(spec)
    except (OSError, RecordingError) as exc:
        print(f"recording error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
