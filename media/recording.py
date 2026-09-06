from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "docker-compose.yml"
RECORDING_COMPOSE = ROOT / "docker-compose.recording.yml"
MEDIA_CONFIG = ROOT / "media" / "mediamtx.yml"
LOCK_ROOT = Path("/tmp").resolve()

IMAGE_DIGEST = "sha256:1b029d11049be75630e9b73bb0d5f47b08a7db4eaee89a80bf8f53bc40e56414"
IMAGE_REF = f"bluenviron/mediamtx:1.20.1@{IMAGE_DIGEST}"
DEFAULT_MAX_BYTES = 20 * 1024**3
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3
DEFAULT_MAX_DURATION_SECONDS = 4 * 60 * 60
MAX_DURATION_SECONDS = 23 * 60 * 60
MAX_POLL_INTERVAL = 1.0
MAX_SEGMENTS = 10_000
MAX_PROBE_STREAMS = 32
MAX_SEGMENT_DURATION_SECONDS = 120.0
MAX_VIDEO_DIMENSION = 8_192
MAX_VIDEO_PIXELS = 7_680 * 4_320
MAX_DECODE_SECONDS = 300.0
MAX_FINALIZATION_SECONDS = 60 * 60
MAX_MANIFEST_BYTES = 8 * 1024**2
ARCHIVE_METADATA_BLOCKS = 16
OWNER_LABEL = "org.worldofhacks.sweep.recording-owner"
ALLOWED_STREAMS = {f"drone{index}" for index in range(1, 5)}
MAX_TREE_ENTRIES = MAX_SEGMENTS + len(ALLOWED_STREAMS) + 1


class RecordingError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalizationBudget:
    """One aggregate monotonic deadline shared by every segment-validation step."""

    deadline: float
    limit_seconds: float
    run_dir: Path
    cancelled: Callable[[], bool] = field(repr=False)

    def _remaining(self) -> float:
        if self.cancelled():
            raise RecordingError(
                "recording finalization was cancelled; finalized evidence remains "
                f"unexported at {self.run_dir}"
            )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RecordingError(
                f"recording finalization exceeded the {self.limit_seconds:g}-second "
                f"aggregate budget; finalized evidence remains unexported at {self.run_dir}"
            )
        return remaining

    def timeout(self, maximum: float) -> float:
        return min(maximum, self._remaining())

    def checkpoint(self) -> None:
        self._remaining()


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
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS
    extra_compose_files: tuple[Path, ...] = ()
    owner_token: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)

    @property
    def session_storage_key(self) -> str:
        digest = hashlib.sha256(self.session_id.encode()).hexdigest()
        return f"session-{digest}"

    @property
    def session_dir(self) -> Path:
        return self.recording_root / self.session_storage_key

    @property
    def run_dir(self) -> Path:
        return self.session_dir / self.run_id

    @property
    def export_dir(self) -> Path:
        return self.export_root / self.run_id

    @property
    def compose_files(self) -> tuple[Path, ...]:
        return (BASE_COMPOSE, RECORDING_COMPOSE, *self.extra_compose_files)


@dataclass(frozen=True)
class PinnedDirectory:
    path: Path
    label: str
    descriptor: int
    device: int
    inode: int

    @classmethod
    def open(cls, path: Path, label: str) -> PinnedDirectory:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RecordingError(f"cannot pin {label}: {path}") from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise RecordingError(f"{label} is not a directory: {path}")
        return cls(path, label, descriptor, metadata.st_dev, metadata.st_ino)

    def assert_current(self) -> None:
        try:
            descriptor_metadata = os.fstat(self.descriptor)
            path_metadata = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise RecordingError(f"{self.label} identity changed: {self.path}") from exc
        expected = (self.device, self.inode)
        if (
            not stat.S_ISDIR(path_metadata.st_mode)
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != expected
            or (path_metadata.st_dev, path_metadata.st_ino) != expected
        ):
            raise RecordingError(f"{self.label} identity changed: {self.path}")

    def free_bytes(self) -> int:
        self.assert_current()
        usage = os.fstatvfs(self.descriptor)
        return usage.f_bavail * usage.f_frsize

    def block_size(self) -> int:
        self.assert_current()
        usage = os.fstatvfs(self.descriptor)
        return max(1, usage.f_frsize)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass(frozen=True)
class PreparedRun:
    recording_root: PinnedDirectory
    session_dir: PinnedDirectory
    run_dir: PinnedDirectory
    export_root: PinnedDirectory

    @property
    def directories(self) -> tuple[PinnedDirectory, ...]:
        return (self.recording_root, self.session_dir, self.run_dir, self.export_root)

    def assert_current(self) -> None:
        for directory in self.directories:
            directory.assert_current()

    def close(self) -> None:
        for directory in reversed(self.directories):
            directory.close()


@dataclass(frozen=True)
class OwnedService:
    container_id: str
    owner_token: str


@dataclass(frozen=True)
class ArchivePlan:
    manifest: bytes
    segments: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class TreeEntry:
    relative: Path
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def from_stat(cls, relative: Path, metadata: os.stat_result) -> TreeEntry:
        return cls(
            relative=relative,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )

    def matches(self, metadata: os.stat_result) -> bool:
        return (
            self.device,
            self.inode,
            self.mode,
            self.size,
            self.modified_ns,
            self.changed_ns,
        ) == (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )


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


def _validate_session_identifier(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 512
        or value != value.strip()
        or not value.isprintable()
    ):
        raise RecordingError(
            "session id must be canonical printable text of at most 512 characters"
        )
    return value


def _validate_runtime_limits(
    max_bytes: object,
    min_free_bytes: object,
    poll_interval: object,
    max_duration_seconds: object,
) -> None:
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or type(min_free_bytes) is not int
        or min_free_bytes <= 0
    ):
        raise RecordingError("byte budget and free-space reserve must be positive integers")
    if type(poll_interval) not in {int, float} or not (
        math.isfinite(poll_interval) and 0 < poll_interval <= MAX_POLL_INTERVAL
    ):
        raise RecordingError(
            f"poll interval must be greater than zero and at most {MAX_POLL_INTERVAL}"
        )
    if type(max_duration_seconds) not in {int, float} or not (
        math.isfinite(max_duration_seconds) and 0 < max_duration_seconds <= MAX_DURATION_SECONDS
    ):
        raise RecordingError(
            f"maximum duration must be greater than zero and at most {MAX_DURATION_SECONDS} seconds"
        )


def _pin_run(spec: RunSpec) -> PreparedRun:
    pins: list[PinnedDirectory] = []
    try:
        for path, label in (
            (spec.recording_root, "recording root"),
            (spec.session_dir, "recording session directory"),
            (spec.run_dir, "recording run directory"),
            (spec.export_root, "durable export root"),
        ):
            pins.append(PinnedDirectory.open(path, label))
    except BaseException:
        for pin in reversed(pins):
            pin.close()
        raise
    return PreparedRun(*pins)


def _prepare(spec: RunSpec) -> PreparedRun:
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

    for label, root in (("recording", spec.recording_root), ("durable export", spec.export_root)):
        filesystem = os.statvfs(root)
        block_size = max(1, filesystem.f_frsize)
        overhead = _allocated_size(MAX_MANIFEST_BYTES, block_size)
        overhead += (MAX_TREE_ENTRIES + ARCHIVE_METADATA_BLOCKS) * block_size
        required = spec.max_bytes + spec.min_free_bytes + overhead
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
    return _pin_run(spec)


def _compose_identities(spec: RunSpec, environment: dict[str, str]) -> tuple[str, str]:
    output = _command(
        _compose_command(spec, "config", "--format", "json"), timeout=30, environment=environment
    ).stdout
    try:
        resolved = json.loads(output)
        project = resolved["name"]
        container = resolved["services"]["mediamtx"]["container_name"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RecordingError("Compose did not resolve the MediaMTX runtime identity") from exc
    if not all(isinstance(value, str) and 1 <= len(value) <= 255 for value in (project, container)):
        raise RecordingError("Compose returned an invalid MediaMTX runtime identity")
    return f"project:{project}", f"container:{container}"


@contextmanager
def _lock(identities: tuple[str, ...]) -> Iterator[None]:
    import fcntl

    locks = []
    try:
        for identity in sorted(set(identities)):
            name = hashlib.sha256(identity.encode()).hexdigest()
            descriptor = os.open(
                LOCK_ROOT / f"sweep-recording-{name}.lock",
                os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR,
                0o600,
            )
            lock = os.fdopen(descriptor, "a+")
            metadata = os.fstat(lock.fileno())
            if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
                lock.close()
                raise RecordingError("recording identity lock has unsafe ownership or type")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                lock.close()
                raise RecordingError("another recording operation controls this MediaMTX") from exc
            locks.append(lock)
        yield
    finally:
        for lock in reversed(locks):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


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
        "SWEEP_RECORDING_OWNER": spec.owner_token,
    }


def _command(
    command: list[str],
    *,
    timeout: float,
    environment: dict[str, str] | None = None,
    check: bool = True,
    pass_fds: tuple[int, ...] = (),
    finalization: FinalizationBudget | None = None,
) -> subprocess.CompletedProcess[str]:
    if finalization is not None:
        timeout = finalization.timeout(timeout)
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if finalization is not None:
            try:
                finalization.checkpoint()
            except RecordingError as budget_error:
                raise budget_error from exc
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or ""
        detail = detail.strip()[-1000:]
        raise RecordingError(
            f"command failed ({' '.join(command)})" + (f": {detail}" if detail else "")
        ) from exc
    if finalization is not None:
        finalization.checkpoint()
    return result


def _compose_container_id(
    spec: RunSpec, environment: dict[str, str], *, include_stopped: bool
) -> str | None:
    arguments = ["ps"]
    if include_stopped:
        arguments.append("--all")
    arguments.extend(("--quiet", "mediamtx"))
    output = _command(
        _compose_command(spec, *arguments), timeout=20, environment=environment
    ).stdout.strip()
    if not output:
        return None
    if "\n" in output:
        raise RecordingError("Compose resolved more than one MediaMTX container")
    return output


def _inspect_container(container_id: str) -> dict[str, object] | None:
    command = ["docker", "inspect", container_id]
    result = _command(command, timeout=20, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "No such object" in detail or "No such container" in detail:
            return None
        raise RecordingError(
            f"command failed ({' '.join(command)})" + (f": {detail[-1000:]}" if detail else "")
        )
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecordingError("Docker returned invalid container metadata") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise RecordingError("Docker returned invalid container metadata")
    return values[0]


def _owned_service(
    container_id: str, owner_token: str, *, expected_running: bool = True
) -> OwnedService:
    details = _inspect_container(container_id)
    if details is None:
        raise RecordingError("MediaMTX disappeared during startup")
    try:
        configured_image = details["Config"]["Image"]  # type: ignore[index]
        labels = details["Config"]["Labels"]  # type: ignore[index]
        running = details["State"]["Running"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise RecordingError("Docker returned incomplete MediaMTX metadata") from exc
    if configured_image != IMAGE_REF:
        raise RecordingError("running MediaMTX container does not use the pinned image reference")
    if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != owner_token:
        raise RecordingError("MediaMTX container is not owned by this recording operation")
    if running is not expected_running:
        state = "running" if expected_running else "stopped"
        raise RecordingError(f"MediaMTX was not {state} at the ownership checkpoint")
    return OwnedService(container_id, owner_token)


def _discover_owned_service(
    spec: RunSpec, environment: dict[str, str], owner_token: str
) -> OwnedService | None:
    container_id = _compose_container_id(spec, environment, include_stopped=True)
    if container_id is None:
        return None
    details = _inspect_container(container_id)
    if details is None:
        return None
    try:
        labels = details["Config"]["Labels"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise RecordingError("Docker returned incomplete MediaMTX metadata") from exc
    if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != owner_token:
        return None
    return OwnedService(container_id, owner_token)


def _service_running(service: OwnedService) -> bool:
    details = _inspect_container(service.container_id)
    if details is None:
        return False
    try:
        labels = details["Config"]["Labels"]  # type: ignore[index]
        running = details["State"]["Running"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise RecordingError("Docker returned incomplete MediaMTX metadata") from exc
    if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != service.owner_token:
        raise RecordingError("MediaMTX ownership changed during recording")
    return running is True


def _stop_service(service: OwnedService) -> bool:
    """Stop/remove the owned service and report a confirmed controlled stop."""
    details = _inspect_container(service.container_id)
    if details is None:
        return False
    try:
        labels = details["Config"]["Labels"]  # type: ignore[index]
        running = details["State"]["Running"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise RecordingError("Docker returned incomplete MediaMTX metadata") from exc
    if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != service.owner_token:
        raise RecordingError("refusing to stop a MediaMTX container owned by another operation")
    if not isinstance(running, bool):
        raise RecordingError("Docker returned incomplete MediaMTX metadata")

    stopped = _command(
        ["docker", "stop", "--time", "20", service.container_id],
        timeout=45,
        check=False,
    )
    removed = _command(["docker", "rm", "--force", service.container_id], timeout=30, check=False)
    if removed.returncode != 0:
        detail = removed.stderr.strip()[-1000:]
        raise RecordingError(
            "could not remove the owned MediaMTX container" + (f": {detail}" if detail else "")
        )
    # A nonzero stop means the service may have exited between inspection and the
    # controlled stop request.  A later successful force-remove cannot turn that
    # ambiguity back into an operator or safety-limit success.
    return running and stopped.returncode == 0


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _relative_parts(relative: Path) -> tuple[str, ...]:
    parts = relative.parts
    if relative.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise RecordingError(f"unsafe relative recording path: {relative}")
    return parts


def _open_directory_at(root_descriptor: int, relative: Path) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in _relative_parts(relative):
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_file_at(root_descriptor: int, relative: Path) -> int:
    parts = _relative_parts(relative)
    parent = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
    except BaseException:
        os.close(parent)
        raise
    os.close(parent)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise RecordingError(f"recording path is not a regular file: {relative}")
    return descriptor


def _scan_tree_at(
    root_descriptor: int,
    label: str,
    finalization: FinalizationBudget | None = None,
) -> list[TreeEntry]:
    entries: list[TreeEntry] = []
    pending = [(Path(), os.dup(root_descriptor))]
    try:
        while pending:
            if finalization is not None:
                finalization.checkpoint()
            parent_relative, descriptor = pending.pop()
            try:
                children = list(os.scandir(descriptor))
                if finalization is not None:
                    finalization.checkpoint()
                for child in children:
                    if finalization is not None:
                        finalization.checkpoint()
                    metadata = child.stat(follow_symlinks=False)
                    relative = parent_relative / child.name
                    entry = TreeEntry.from_stat(relative, metadata)
                    entries.append(entry)
                    if len(entries) > MAX_TREE_ENTRIES:
                        raise RecordingError(f"{label} exceeds {MAX_TREE_ENTRIES} entries")
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RecordingError(f"{label} contains a symbolic link: {relative}")
                    if stat.S_ISDIR(metadata.st_mode):
                        child_descriptor = os.open(
                            child.name, _directory_flags(), dir_fd=descriptor
                        )
                        if not entry.matches(os.fstat(child_descriptor)):
                            os.close(child_descriptor)
                            raise RecordingError(f"{label} changed during inspection: {relative}")
                        pending.append((relative, child_descriptor))
                    elif not stat.S_ISREG(metadata.st_mode):
                        raise RecordingError(f"{label} contains a special file: {relative}")
            finally:
                os.close(descriptor)
    except BaseException:
        for _, descriptor in pending:
            os.close(descriptor)
        raise
    if finalization is not None:
        finalization.checkpoint()
    ordered = sorted(entries, key=lambda entry: entry.relative.as_posix())
    if finalization is not None:
        finalization.checkpoint()
    return ordered


def _tree_bytes(run_dir: Path) -> int:
    pinned = PinnedDirectory.open(run_dir, "recording run directory")
    try:
        return sum(
            entry.size
            for entry in _scan_tree_at(pinned.descriptor, "recording tree")
            if stat.S_ISREG(entry.mode)
        )
    finally:
        pinned.close()


def _allocated_size(size: int, block_size: int) -> int:
    return ((size + block_size - 1) // block_size) * block_size


def _tree_usage_at(
    root_descriptor: int,
    block_size: int,
    finalization: FinalizationBudget | None = None,
) -> tuple[int, int]:
    logical = 0
    allocated = block_size
    for entry in _scan_tree_at(root_descriptor, "recording tree", finalization):
        if finalization is not None:
            finalization.checkpoint()
        if stat.S_ISREG(entry.mode):
            logical += entry.size
            allocated += _allocated_size(entry.size, block_size) + block_size
        else:
            allocated += block_size
    return logical, allocated


def _limit_reason(used: int, free: int, maximum: int, reserve: int) -> str | None:
    if used >= maximum:
        return f"recording byte budget reached ({used} >= {maximum})"
    if free <= reserve:
        return f"recording free-space reserve reached ({free} <= {reserve})"
    return None


@contextmanager
def _prepared_context(spec: RunSpec, prepared: PreparedRun | None) -> Iterator[PreparedRun]:
    if prepared is not None:
        prepared.assert_current()
        yield prepared
        return
    temporary = _pin_run(spec)
    try:
        temporary.assert_current()
        yield temporary
    finally:
        temporary.close()


def _budget_status(
    spec: RunSpec,
    prepared: PreparedRun | None = None,
    finalization: FinalizationBudget | None = None,
) -> tuple[tuple[str, ...], bool]:
    if finalization is not None:
        finalization.checkpoint()
    with _prepared_context(spec, prepared) as pinned:
        export_block = pinned.export_root.block_size()
        used, export_allocation = _tree_usage_at(
            pinned.run_dir.descriptor, export_block, finalization
        )
        pinned.assert_current()
        reasons = []
        recording_reason = _limit_reason(
            used, pinned.run_dir.free_bytes(), spec.max_bytes, spec.min_free_bytes
        )
        if recording_reason:
            reasons.append(recording_reason)

        export_safe = True
        if not _same_filesystem(pinned.run_dir, pinned.export_root):
            if finalization is not None:
                finalization.checkpoint()
            required = (
                export_allocation
                + _allocated_size(MAX_MANIFEST_BYTES, export_block)
                + (ARCHIVE_METADATA_BLOCKS + 1) * export_block
                + spec.min_free_bytes
            )
            free = pinned.export_root.free_bytes()
            if free <= required:
                reasons.append(
                    f"durable export requires more than {required} free bytes ({free} available)"
                )
                export_safe = False
        pinned.assert_current()
        if finalization is not None:
            finalization.checkpoint()
        return tuple(reasons), export_safe


def _probe_descriptor(
    descriptor: int,
    path: Path,
    finalization: FinalizationBudget | None = None,
) -> tuple[str, str, dict[str, object]]:
    media_path = f"/dev/fd/{descriptor}"
    arguments = (
        "ffprobe -v error -show_entries "
        "format=format_name,duration:stream=index,codec_type,codec_name,width,height -of json"
    ).split()
    os.lseek(descriptor, 0, os.SEEK_SET)
    result = _command(
        [*arguments, media_path],
        timeout=30,
        pass_fds=(descriptor,),
        finalization=finalization,
    )
    try:
        probe = json.loads(result.stdout)
        details = probe["format"]
        format_name, duration = details["format_name"], details["duration"]
        parsed_duration = float(duration)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecordingError(f"ffprobe returned invalid metadata for {path}") from exc
    if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
        raise RecordingError(f"recording is not an fMP4-compatible segment: {path}")
    if not math.isfinite(parsed_duration) or parsed_duration <= 0:
        raise RecordingError(f"recording has no positive finite duration: {path}")
    if parsed_duration > MAX_SEGMENT_DURATION_SECONDS:
        raise RecordingError(f"recording duration exceeds the segment bound: {path}")

    streams = probe.get("streams")
    if not isinstance(streams, list) or len(streams) > MAX_PROBE_STREAMS:
        raise RecordingError(f"recording has invalid or excessive stream metadata: {path}")
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise RecordingError(f"recording contains no video stream: {path}")
    index, codec, width, height = (
        video.get("index"),
        video.get("codec_name"),
        video.get("width"),
        video.get("height"),
    )
    if (
        type(index) is not int
        or not 0 <= index < MAX_PROBE_STREAMS
        or not isinstance(codec, str)
        or not (1 <= len(codec) <= 64)
        or not codec.isascii()
        or type(width) is not int
        or type(height) is not int
        or not (1 <= width <= MAX_VIDEO_DIMENSION and 1 <= height <= MAX_VIDEO_DIMENSION)
        or width * height > MAX_VIDEO_PIXELS
    ):
        raise RecordingError(f"recording video stream has invalid metadata: {path}")

    os.lseek(descriptor, 0, os.SEEK_SET)
    decoded = _command(
        [
            *"ffmpeg -nostdin -v error -xerror -i".split(),
            media_path,
            *f"-map 0:{index} -frames:v 1 -f framehash -".split(),
        ],
        timeout=30,
        pass_fds=(descriptor,),
        finalization=finalization,
    )
    if not any(
        line.strip() and not line.lstrip().startswith("#") for line in decoded.stdout.splitlines()
    ):
        raise RecordingError(f"recording video stream produced no decodable frame: {path}")
    decode_timeout = min(MAX_DECODE_SECONDS, max(30.0, parsed_duration * 4.0))
    os.lseek(descriptor, 0, os.SEEK_SET)
    _command(
        [
            *"ffmpeg -nostdin -v error -xerror -err_detect explode -i".split(),
            media_path,
            *f"-map 0:{index} -an -sn -dn -f null -".split(),
        ],
        timeout=decode_timeout,
        pass_fds=(descriptor,),
        finalization=finalization,
    )
    video_facts = {"index": index, "codec_name": codec, "width": width, "height": height}
    return format_name, duration, video_facts


def _probe(
    path: Path, finalization: FinalizationBudget | None = None
) -> tuple[str, str, dict[str, object]]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecordingError(f"recording path is not a regular file: {path}")
        if finalization is None:
            return _probe_descriptor(descriptor, path)
        return _probe_descriptor(descriptor, path, finalization)
    finally:
        os.close(descriptor)


def _segments_at(
    root_descriptor: int,
    run_dir: Path,
    finalization: FinalizationBudget | None = None,
) -> list[dict[str, object]]:
    if finalization is not None:
        finalization.checkpoint()
    files: list[TreeEntry] = []
    for entry in _scan_tree_at(root_descriptor, "recording tree", finalization):
        if finalization is not None:
            finalization.checkpoint()
        if not stat.S_ISREG(entry.mode):
            continue
        relative = entry.relative
        if relative.suffix.lower() != ".mp4":
            raise RecordingError(f"unexpected file in recording run: {relative.as_posix()}")
        if len(relative.parts) != 2 or relative.parts[0] not in ALLOWED_STREAMS:
            raise RecordingError(f"unexpected stream path: {relative.as_posix()}")
        files.append(entry)
    if not files:
        raise RecordingError("recording run contains zero finalized MP4 segments")
    if len(files) > MAX_SEGMENTS:
        raise RecordingError(f"recording run exceeds {MAX_SEGMENTS} segments")

    result: list[dict[str, object]] = []
    for entry in files:
        if finalization is not None:
            finalization.checkpoint()
        path = run_dir / entry.relative
        if entry.size <= 0:
            raise RecordingError(f"recording segment is empty: {path}")
        descriptor = _open_file_at(root_descriptor, entry.relative)
        try:
            if not entry.matches(os.fstat(descriptor)):
                raise RecordingError(f"recording file changed during inspection: {entry.relative}")
            if finalization is None:
                format_name, duration, video_stream = _probe_descriptor(descriptor, path)
                digest = _descriptor_sha256(descriptor)
            else:
                format_name, duration, video_stream = _probe_descriptor(
                    descriptor, path, finalization
                )
                digest = _descriptor_sha256(descriptor, finalization)
            if not entry.matches(os.fstat(descriptor)):
                raise RecordingError(f"recording file changed during inspection: {entry.relative}")
            result.append(
                {
                    "path": entry.relative.as_posix(),
                    "size_bytes": entry.size,
                    "sha256": digest,
                    "format_name": format_name,
                    "duration_seconds": duration,
                    "video_stream": video_stream,
                }
            )
        finally:
            os.close(descriptor)
    return result


def _segments(
    run_dir: Path,
    prepared: PreparedRun | None = None,
    finalization: FinalizationBudget | None = None,
) -> list[dict[str, object]]:
    if prepared is not None:
        prepared.assert_current()
        result = _segments_at(prepared.run_dir.descriptor, run_dir, finalization)
        prepared.assert_current()
        return result
    pinned = PinnedDirectory.open(run_dir, "recording run directory")
    try:
        pinned.assert_current()
        return _segments_at(pinned.descriptor, run_dir, finalization)
    finally:
        pinned.close()


def _descriptor_bytes(
    descriptor: int,
    maximum: int,
    finalization: FinalizationBudget | None = None,
) -> bytes:
    if finalization is not None:
        finalization.checkpoint()
    size = os.fstat(descriptor).st_size
    if size > maximum:
        raise RecordingError(f"recording file exceeds the {maximum}-byte read bound")
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        if finalization is not None:
            finalization.checkpoint()
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise RecordingError("recording file changed during inspection")
        chunks.append(chunk)
        offset += len(chunk)
    if os.fstat(descriptor).st_size != size:
        raise RecordingError("recording file changed during inspection")
    if finalization is not None:
        finalization.checkpoint()
    return b"".join(chunks)


def _descriptor_sha256(descriptor: int, finalization: FinalizationBudget | None = None) -> str:
    digest = hashlib.sha256()
    size = os.fstat(descriptor).st_size
    offset = 0
    while offset < size:
        if finalization is not None:
            finalization.checkpoint()
        chunk = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise RecordingError("recording file changed during inspection")
        digest.update(chunk)
        offset += len(chunk)
    if os.fstat(descriptor).st_size != size:
        raise RecordingError("recording file changed during inspection")
    if finalization is not None:
        finalization.checkpoint()
    return digest.hexdigest()


def _atomic_write_at(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    finalization: FinalizationBudget | None = None,
) -> None:
    temporary = f".{name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        if finalization is not None:
            finalization.checkpoint()
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                if finalization is not None:
                    finalization.checkpoint()
                written = output.write(view[offset : offset + 1024 * 1024])
                if written is None or written <= 0:
                    raise RecordingError("recording manifest write stalled")
                offset += written
            output.flush()
            if finalization is not None:
                finalization.checkpoint()
            os.fsync(output.fileno())
        if finalization is not None:
            finalization.checkpoint()
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        if finalization is not None:
            finalization.checkpoint()
        os.fsync(directory_descriptor)
        if finalization is not None:
            finalization.checkpoint()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def _verify_copy_at(
    root_descriptor: int,
    segments: tuple[dict[str, object], ...],
    manifest: bytes,
    finalization: FinalizationBudget | None = None,
) -> None:
    expected_files = {"recording-manifest.json", *(str(item["path"]) for item in segments)}
    expected_directories: set[str] = set()
    for item in segments:
        relative = Path(str(item["path"]))
        _relative_parts(relative)
        expected_directories.update(
            parent.as_posix() for parent in relative.parents if parent != Path(".")
        )

    entries = _scan_tree_at(root_descriptor, "exported run", finalization)
    found_files = {entry.relative.as_posix() for entry in entries if stat.S_ISREG(entry.mode)}
    found_directories = {entry.relative.as_posix() for entry in entries if stat.S_ISDIR(entry.mode)}
    if finalization is not None:
        finalization.checkpoint()
    if found_files != expected_files or found_directories != expected_directories:
        raise RecordingError("exported run does not exactly match the manifest")

    descriptor = _open_file_at(root_descriptor, Path("recording-manifest.json"))
    try:
        if _descriptor_bytes(descriptor, MAX_MANIFEST_BYTES, finalization) != manifest:
            raise RecordingError("exported manifest bytes changed during copy")
    finally:
        os.close(descriptor)
    for item in segments:
        if finalization is not None:
            finalization.checkpoint()
        relative = Path(str(item["path"]))
        descriptor = _open_file_at(root_descriptor, relative)
        try:
            if (
                os.fstat(descriptor).st_size != item["size_bytes"]
                or _descriptor_sha256(descriptor, finalization) != item["sha256"]
            ):
                raise RecordingError(f"exported segment does not match its manifest: {relative}")
        finally:
            os.close(descriptor)


def _fsync_tree_at(
    root_descriptor: int,
    finalization: FinalizationBudget | None = None,
) -> None:
    entries = _scan_tree_at(root_descriptor, "recording tree", finalization)
    for entry in entries:
        if finalization is not None:
            finalization.checkpoint()
        if not stat.S_ISREG(entry.mode):
            continue
        descriptor = _open_file_at(root_descriptor, entry.relative)
        try:
            if not entry.matches(os.fstat(descriptor)):
                raise RecordingError(
                    f"recording file changed during synchronization: {entry.relative}"
                )
            if finalization is not None:
                finalization.checkpoint()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = sorted(
        (entry for entry in entries if stat.S_ISDIR(entry.mode)),
        key=lambda entry: len(entry.relative.parts),
        reverse=True,
    )
    for entry in directories:
        if finalization is not None:
            finalization.checkpoint()
        descriptor = _open_directory_at(root_descriptor, entry.relative)
        try:
            if not entry.matches(os.fstat(descriptor)):
                raise RecordingError(
                    f"recording directory changed during synchronization: {entry.relative}"
                )
            if finalization is not None:
                finalization.checkpoint()
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if finalization is not None:
        finalization.checkpoint()
    os.fsync(root_descriptor)
    if finalization is not None:
        finalization.checkpoint()


def _copy_file_at(
    source_root: int,
    destination_root: int,
    relative: Path,
    expected_size: int,
    expected_sha256: str,
    finalization: FinalizationBudget | None = None,
) -> None:
    parts = _relative_parts(relative)
    source = _open_file_at(source_root, relative)
    destination_parent: int | None = None
    destination: int | None = None
    try:
        if finalization is not None:
            finalization.checkpoint()
        destination_parent = (
            _open_directory_at(destination_root, Path(*parts[:-1]))
            if len(parts) > 1
            else os.dup(destination_root)
        )
        if os.fstat(source).st_size != expected_size:
            raise RecordingError(f"recording segment changed before copy: {relative}")
        destination = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=destination_parent,
        )
        digest = hashlib.sha256()
        offset = 0
        while offset < expected_size:
            if finalization is not None:
                finalization.checkpoint()
            chunk = os.pread(source, min(1024 * 1024, expected_size - offset), offset)
            if not chunk:
                raise RecordingError(f"recording segment changed during copy: {relative}")
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                if finalization is not None:
                    finalization.checkpoint()
                count = os.write(destination, chunk[written:])
                if count <= 0:
                    raise RecordingError(f"recording segment copy stalled: {relative}")
                written += count
            offset += len(chunk)
        if os.fstat(source).st_size != expected_size or digest.hexdigest() != expected_sha256:
            raise RecordingError(f"recording segment changed during copy: {relative}")
        if finalization is not None:
            finalization.checkpoint()
        os.fsync(destination)
        if finalization is not None:
            finalization.checkpoint()
    except BaseException:
        if destination is not None:
            os.close(destination)
            destination = None
        if destination_parent is not None:
            try:
                os.unlink(parts[-1], dir_fd=destination_parent)
            except FileNotFoundError:
                pass
        raise
    finally:
        if destination is not None:
            os.close(destination)
        if destination_parent is not None:
            os.close(destination_parent)
        os.close(source)


def _entry_at(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _assert_entry_matches_pin(parent_descriptor: int, name: str, pinned: PinnedDirectory) -> None:
    metadata = _entry_at(parent_descriptor, name)
    descriptor_metadata = os.fstat(pinned.descriptor)
    expected = (pinned.device, pinned.inode)
    if (
        metadata is None
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != expected
    ):
        raise RecordingError(f"{pinned.label} identity changed: {pinned.path}")


def _assert_published(
    spec: RunSpec,
    prepared: PreparedRun,
    archive_descriptor: int,
) -> None:
    for pinned in (
        prepared.recording_root,
        prepared.session_dir,
        prepared.export_root,
    ):
        pinned.assert_current()
    descriptor_metadata = os.fstat(archive_descriptor)
    entry_metadata = _entry_at(prepared.export_root.descriptor, spec.export_dir.name)
    try:
        path_metadata = os.stat(spec.export_dir, follow_symlinks=False)
    except OSError as exc:
        raise RecordingError(f"durable published run identity changed: {spec.export_dir}") from exc
    expected = (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
    if (
        entry_metadata is None
        or not stat.S_ISDIR(entry_metadata.st_mode)
        or (entry_metadata.st_dev, entry_metadata.st_ino) != expected
        or not stat.S_ISDIR(path_metadata.st_mode)
        or (path_metadata.st_dev, path_metadata.st_ino) != expected
    ):
        raise RecordingError(f"durable published run identity changed: {spec.export_dir}")


def _remove_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: tuple[int, int] | None = None,
) -> None:
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if expected is not None and (metadata.st_dev, metadata.st_ino) != expected:
            raise RecordingError(f"refusing to remove a replaced directory: {name}")
        for child in list(os.scandir(descriptor)):
            child_metadata = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(child_metadata.st_mode):
                _remove_directory_at(
                    descriptor,
                    child.name,
                    expected=(child_metadata.st_dev, child_metadata.st_ino),
                )
            else:
                os.unlink(child.name, dir_fd=descriptor)
        current = _entry_at(parent_descriptor, name)
        if current is None or (current.st_dev, current.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RecordingError(f"refusing to remove a replaced directory: {name}")
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def _same_filesystem(source: Path | PinnedDirectory, destination: Path | PinnedDirectory) -> bool:
    source_device = source.device if isinstance(source, PinnedDirectory) else os.stat(source).st_dev
    destination_device = (
        destination.device
        if isinstance(destination, PinnedDirectory)
        else os.stat(destination).st_dev
    )
    return source_device == destination_device


def _archive_plan(
    spec: RunSpec,
    segments: list[dict[str, object]],
    *,
    started_at: str,
    stopped_at: str,
    stop_reason: str,
    elapsed_seconds: float,
    configuration: dict[str, str] | None = None,
    finalization: FinalizationBudget | None = None,
) -> ArchivePlan:
    if finalization is not None:
        finalization.checkpoint()
    manifest = _canonical_json(
        {
            "schema": "sweep.media.recording-manifest.v1",
            "run_id": spec.run_id,
            "session_id": spec.session_id,
            "started_at_utc": started_at,
            "stopped_at_utc": stopped_at,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "stop_reason": stop_reason,
            "image": IMAGE_REF,
            "configuration_sha256": (
                configuration if configuration is not None else _configuration(spec)
            ),
            "limits": {
                "max_bytes": spec.max_bytes,
                "min_free_bytes": spec.min_free_bytes,
                "max_duration_seconds": spec.max_duration_seconds,
                "max_finalization_seconds": MAX_FINALIZATION_SECONDS,
            },
            "segments": segments,
        }
    )
    if len(manifest) > MAX_MANIFEST_BYTES:
        raise RecordingError(f"recording manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    if finalization is not None:
        finalization.checkpoint()
    return ArchivePlan(manifest, tuple(segments))


def _archive_allocation(plan: ArchivePlan, block_size: int) -> int:
    stream_directories = {str(segment["path"]).split("/", 1)[0] for segment in plan.segments}
    files = _allocated_size(len(plan.manifest), block_size)
    files += sum(
        _allocated_size(int(segment["size_bytes"]), block_size) for segment in plan.segments
    )
    metadata_entries = 2 + len(stream_directories) + len(plan.segments)
    metadata = (metadata_entries + ARCHIVE_METADATA_BLOCKS) * block_size
    return files + metadata


def _ensure_publication_space(
    spec: RunSpec,
    plan: ArchivePlan,
    prepared: PreparedRun,
    *,
    complete_archive: bool,
    finalization: FinalizationBudget | None = None,
) -> None:
    if finalization is not None:
        finalization.checkpoint()
    prepared.assert_current()
    destination = prepared.export_root if complete_archive else prepared.run_dir
    block_size = destination.block_size()
    if complete_archive:
        allocation = _archive_allocation(plan, block_size)
    else:
        allocation = _allocated_size(len(plan.manifest), block_size)
        allocation += (ARCHIVE_METADATA_BLOCKS + 2) * block_size
    required = spec.min_free_bytes + allocation
    free = destination.free_bytes()
    if finalization is not None:
        finalization.checkpoint()
    if free <= required:
        raise RecordingError(
            f"recording publication requires more than {required} free bytes ({free} available)"
        )


def _rename_for_publication(
    source_name: str,
    destination_name: str,
    *,
    source_descriptor: int,
    destination_descriptor: int,
    finalization: FinalizationBudget | None,
    publication_committed: Callable[[], None] | None,
) -> None:
    """Linearize the deadline/cancellation check with the irreversible publish rename."""
    handled = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous_mask: set[signal.Signals] | None = None
    if finalization is not None and hasattr(signal, "pthread_sigmask"):
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
    try:
        if finalization is not None:
            finalization.checkpoint()
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_descriptor,
            dst_dir_fd=destination_descriptor,
        )
        if publication_committed is not None:
            publication_committed()
    finally:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _export(
    spec: RunSpec,
    plan: ArchivePlan,
    prepared: PreparedRun | None = None,
    finalization: FinalizationBudget | None = None,
    publication_committed: Callable[[], None] | None = None,
) -> Path:
    with _prepared_context(spec, prepared) as pinned:
        if finalization is not None:
            finalization.checkpoint()
        pinned.assert_current()
        if _entry_at(pinned.export_root.descriptor, spec.export_dir.name) is not None:
            raise RecordingError(f"durable run destination already exists: {spec.export_dir}")

        if _same_filesystem(pinned.run_dir, pinned.export_root):
            _ensure_publication_space(
                spec,
                plan,
                pinned,
                complete_archive=False,
                finalization=finalization,
            )
            _atomic_write_at(
                pinned.run_dir.descriptor,
                "recording-manifest.json",
                plan.manifest,
                finalization,
            )
            pinned.assert_current()
            _verify_copy_at(pinned.run_dir.descriptor, plan.segments, plan.manifest, finalization)
            _fsync_tree_at(pinned.run_dir.descriptor, finalization)
            pinned.assert_current()
            if finalization is not None:
                finalization.checkpoint()
            if pinned.run_dir.free_bytes() <= spec.min_free_bytes:
                raise RecordingError("recording publication would breach the free-space reserve")
            if _entry_at(pinned.export_root.descriptor, spec.export_dir.name) is not None:
                raise RecordingError(f"durable run destination already exists: {spec.export_dir}")
            _assert_entry_matches_pin(
                pinned.session_dir.descriptor, spec.run_dir.name, pinned.run_dir
            )
            _rename_for_publication(
                spec.run_dir.name,
                spec.export_dir.name,
                source_descriptor=pinned.session_dir.descriptor,
                destination_descriptor=pinned.export_root.descriptor,
                finalization=finalization,
                publication_committed=publication_committed,
            )
            os.fsync(pinned.export_root.descriptor)
            os.fsync(pinned.session_dir.descriptor)
            _assert_published(spec, pinned, pinned.run_dir.descriptor)
            _verify_copy_at(pinned.run_dir.descriptor, plan.segments, plan.manifest)
            _assert_published(spec, pinned, pinned.run_dir.descriptor)
            return spec.export_dir

        _ensure_publication_space(
            spec,
            plan,
            pinned,
            complete_archive=True,
            finalization=finalization,
        )
        staging_name = f".{spec.run_id}.partial-{uuid.uuid4().hex}"
        staging_descriptor: int | None = None
        staging_identity: tuple[int, int] | None = None
        published = False
        try:
            if finalization is not None:
                finalization.checkpoint()
            os.mkdir(staging_name, mode=0o700, dir_fd=pinned.export_root.descriptor)
            staging_descriptor = os.open(
                staging_name, _directory_flags(), dir_fd=pinned.export_root.descriptor
            )
            staging_metadata = os.fstat(staging_descriptor)
            staging_identity = (staging_metadata.st_dev, staging_metadata.st_ino)
            stream_directories = sorted(
                {str(segment["path"]).split("/", 1)[0] for segment in plan.segments}
            )
            for stream in stream_directories:
                if finalization is not None:
                    finalization.checkpoint()
                if stream not in ALLOWED_STREAMS:
                    raise RecordingError(f"unexpected stream path in archive plan: {stream}")
                os.mkdir(stream, mode=0o700, dir_fd=staging_descriptor)
            for segment in plan.segments:
                if finalization is not None:
                    finalization.checkpoint()
                pinned.assert_current()
                relative = Path(str(segment["path"]))
                if len(relative.parts) != 2 or relative.parts[0] not in ALLOWED_STREAMS:
                    raise RecordingError(f"unexpected stream path in archive plan: {relative}")
                size = segment["size_bytes"]
                digest = segment["sha256"]
                if type(size) is not int or size <= 0 or not isinstance(digest, str):
                    raise RecordingError(f"invalid segment facts in archive plan: {relative}")
                _copy_file_at(
                    pinned.run_dir.descriptor,
                    staging_descriptor,
                    relative,
                    size,
                    digest,
                    finalization,
                )
            _atomic_write_at(
                staging_descriptor,
                "recording-manifest.json",
                plan.manifest,
                finalization,
            )
            pinned.assert_current()
            _verify_copy_at(staging_descriptor, plan.segments, plan.manifest, finalization)
            _fsync_tree_at(staging_descriptor, finalization)
            pinned.assert_current()
            if finalization is not None:
                finalization.checkpoint()
            if pinned.export_root.free_bytes() <= spec.min_free_bytes:
                raise RecordingError("recording publication would breach the free-space reserve")
            if _entry_at(pinned.export_root.descriptor, spec.export_dir.name) is not None:
                raise RecordingError(f"durable run destination already exists: {spec.export_dir}")
            current_staging = _entry_at(pinned.export_root.descriptor, staging_name)
            if (
                current_staging is None
                or (
                    current_staging.st_dev,
                    current_staging.st_ino,
                )
                != staging_identity
            ):
                raise RecordingError("durable export staging directory identity changed")
            _rename_for_publication(
                staging_name,
                spec.export_dir.name,
                source_descriptor=pinned.export_root.descriptor,
                destination_descriptor=pinned.export_root.descriptor,
                finalization=finalization,
                publication_committed=publication_committed,
            )
            published = True
            os.fsync(pinned.export_root.descriptor)
            _assert_published(spec, pinned, staging_descriptor)
            _verify_copy_at(staging_descriptor, plan.segments, plan.manifest)
            _assert_entry_matches_pin(
                pinned.session_dir.descriptor, spec.run_dir.name, pinned.run_dir
            )
            _remove_directory_at(
                pinned.session_dir.descriptor,
                spec.run_dir.name,
                expected=(pinned.run_dir.device, pinned.run_dir.inode),
            )
            os.fsync(pinned.session_dir.descriptor)
            _assert_published(spec, pinned, staging_descriptor)
            return spec.export_dir
        finally:
            if staging_descriptor is not None:
                os.close(staging_descriptor)
            if not published and staging_identity is not None:
                current_staging = _entry_at(pinned.export_root.descriptor, staging_name)
                if (
                    current_staging is not None
                    and (
                        current_staging.st_dev,
                        current_staging.st_ino,
                    )
                    == staging_identity
                ):
                    _remove_directory_at(
                        pinned.export_root.descriptor,
                        staging_name,
                        expected=staging_identity,
                    )


def record(spec: RunSpec) -> Path:
    _validate_identifier(spec.run_id, "run id")
    _validate_session_identifier(spec.session_id)
    _validate_runtime_limits(
        spec.max_bytes,
        spec.min_free_bytes,
        spec.poll_interval,
        spec.max_duration_seconds,
    )
    environment = _compose_environment(spec)
    existing = _compose_container_id(spec, environment, include_stopped=True)
    if existing is not None:
        raise RecordingError(
            "MediaMTX already exists for this Compose identity; stop it before recording"
        )
    prepared = _prepare(spec)
    stop_requested = threading.Event()
    requested_signal: int | None = None
    finalizing = False
    publication_committed = False
    cancel_finalization = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal cancel_finalization, requested_signal
        repeated = requested_signal is not None
        if requested_signal is None:
            requested_signal = signum
        stop_requested.set()
        if finalizing and not publication_committed and repeated:
            cancel_finalization = True
            raise RecordingError(
                "recording finalization cancelled by a repeated stop signal; finalized "
                f"evidence remains unexported at {spec.run_dir}"
            )

    failures: list[str] = []
    stop_reason = "operator"
    stop_decided = False
    owned: OwnedService | None = None
    started_at = ""
    started_monotonic = 0.0
    previous = {}
    try:
        configuration = _configuration(spec)
        for handled in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[handled] = signal.signal(handled, request_stop)
        try:
            _command(
                _compose_command(spec, "create", "--pull", "missing", "--no-recreate", "mediamtx"),
                timeout=180,
                environment=environment,
            )
            container_id = _compose_container_id(spec, environment, include_stopped=True)
            if container_id is None:
                raise RecordingError("could not identify the recording MediaMTX container")
            owned = _owned_service(container_id, spec.owner_token, expected_running=False)
            prepared.assert_current()
            started_at = _utc_now()
            started_monotonic = time.monotonic()
            _command(["docker", "start", owned.container_id], timeout=60)
            owned = _owned_service(container_id, spec.owner_token, expected_running=True)
            prepared.assert_current()
            if _configuration(spec) != configuration:
                raise RecordingError("recording configuration changed during MediaMTX startup")
            event = {
                "event": "recording_started",
                "run_id": spec.run_id,
                "session_id": spec.session_id,
                "container_id": owned.container_id,
            }
            print(json.dumps(event, sort_keys=True), flush=True)
            next_service_check = started_monotonic
            deadline = started_monotonic + spec.max_duration_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append(
                        f"recording duration budget reached ({spec.max_duration_seconds} seconds)"
                    )
                    stop_reason = "safety_limit"
                    break
                if stop_requested.wait(min(spec.poll_interval, remaining)):
                    # A signal and a service crash can race. Observe the immutable owned
                    # container before assigning an operator/signal success reason.
                    if requested_signal is not None and not _service_running(owned):
                        failures.append("MediaMTX stopped unexpectedly")
                        stop_reason = "service_failure"
                    break
                if time.monotonic() >= deadline:
                    failures.append(
                        f"recording duration budget reached ({spec.max_duration_seconds} seconds)"
                    )
                    stop_reason = "safety_limit"
                    break
                reasons, _export_safe = _budget_status(spec, prepared)
                if reasons:
                    failures.extend(reason for reason in reasons if reason not in failures)
                    stop_reason = "safety_limit"
                    break
                if time.monotonic() >= next_service_check:
                    if not _service_running(owned):
                        failures.append("MediaMTX stopped unexpectedly")
                        stop_reason = "service_failure"
                        break
                    next_service_check = time.monotonic() + 2
            stop_decided = True
        finally:
            if owned is None:
                owned = _discover_owned_service(spec, environment, spec.owner_token)
            if owned is not None:
                # Linearize every non-service stop reason against the immutable owned
                # container in the same inspection that authorizes cleanup. A crash
                # coinciding with a duration or storage boundary is still a service failure.
                was_running = _stop_service(owned)
                if stop_decided and stop_reason != "service_failure" and was_running is False:
                    if "MediaMTX stopped unexpectedly" not in failures:
                        failures.append("MediaMTX stopped unexpectedly")
                    stop_reason = "service_failure"

        stopped_at = _utc_now()
        stopped_monotonic = time.monotonic()
        if stop_reason == "operator" and requested_signal is not None:
            stop_reason = {
                signal.SIGINT: "operator",
                signal.SIGTERM: "sigterm",
                signal.SIGHUP: "sighup",
            }[requested_signal]

        finalization = FinalizationBudget(
            deadline=time.monotonic() + MAX_FINALIZATION_SECONDS,
            limit_seconds=MAX_FINALIZATION_SECONDS,
            run_dir=spec.run_dir,
            cancelled=lambda: cancel_finalization,
        )
        finalizing = True
        try:
            prepared.assert_current()
            post_stop_reasons, post_stop_export_safe = _budget_status(spec, prepared, finalization)
            finalization.checkpoint()
            failures.extend(reason for reason in post_stop_reasons if reason not in failures)
            segments = _segments(spec.run_dir, prepared, finalization)
            finalization.checkpoint()
            prepared.assert_current()
            final_reasons, final_export_safe = _budget_status(spec, prepared, finalization)
            finalization.checkpoint()
            failures.extend(reason for reason in final_reasons if reason not in failures)
            if (post_stop_reasons or final_reasons) and stop_reason in {
                "operator",
                "sigterm",
                "sighup",
            }:
                stop_reason = "safety_limit"
            if not (post_stop_export_safe and final_export_safe):
                detail = "; ".join(failures)
                raise RecordingError(
                    f"{detail}; finalized evidence remains unexported at {spec.run_dir}"
                )
            plan = _archive_plan(
                spec,
                segments,
                started_at=started_at,
                stopped_at=stopped_at,
                stop_reason=stop_reason,
                elapsed_seconds=stopped_monotonic - started_monotonic,
                configuration=configuration,
                finalization=finalization,
            )
            finalization.checkpoint()

            def mark_publication_committed() -> None:
                nonlocal publication_committed
                publication_committed = True

            archive = _export(
                spec,
                plan,
                prepared,
                finalization,
                mark_publication_committed,
            )
        finally:
            finalizing = False
        print(json.dumps({"event": "recording_exported", "path": str(archive)}, sort_keys=True))
        if failures:
            raise RecordingError(f"{'; '.join(failures)}; stopped safely and exported to {archive}")
        return archive
    finally:
        try:
            for handled, handler in previous.items():
                signal.signal(handled, handler)
        finally:
            prepared.close()


def _parse(argv: list[str] | None) -> RunSpec:
    parser = argparse.ArgumentParser(description="Run and preserve a bounded MediaMTX recording")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--recording-root", type=Path, default=ROOT / "recordings")
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--max-duration-seconds", type=float, default=DEFAULT_MAX_DURATION_SECONDS)
    parser.add_argument("--extra-compose-file", action="append", type=Path, default=[])
    arguments = parser.parse_args(argv)
    _validate_runtime_limits(
        arguments.max_bytes,
        arguments.min_free_bytes,
        arguments.poll_interval,
        arguments.max_duration_seconds,
    )
    extra_files = tuple(
        _absolute_no_symlinks(path, "extra Compose file") for path in arguments.extra_compose_file
    )
    if any(not path.is_file() for path in extra_files):
        raise RecordingError("an extra Compose file is missing or not a regular file")
    return RunSpec(
        run_id=_validate_identifier(arguments.run_id, "run id"),
        session_id=_validate_session_identifier(arguments.session_id),
        recording_root=_absolute_no_symlinks(arguments.recording_root, "recording root"),
        export_root=_absolute_no_symlinks(arguments.export_root, "export root"),
        max_bytes=arguments.max_bytes,
        min_free_bytes=arguments.min_free_bytes,
        poll_interval=arguments.poll_interval,
        max_duration_seconds=arguments.max_duration_seconds,
        extra_compose_files=extra_files,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        if sys.platform not in {"darwin", "linux"}:
            raise RecordingError("recording operations support macOS and Linux only")
        for tool in ("docker", "ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise RecordingError(f"required program is unavailable: {tool}")
        spec = _parse(argv)
        environment = _compose_environment(spec)
        identities = _compose_identities(spec, environment)
        with _lock(identities):
            if _compose_identities(spec, environment) != identities:
                raise RecordingError("MediaMTX runtime identity changed before startup")
            record(spec)
    except (OSError, RecordingError) as exc:
        print(f"recording error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
