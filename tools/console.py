#!/usr/bin/env python3
"""Build and operate the one local Sweep console; never start relay/device services."""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".sweep" / "console"
HOST, PORT = "127.0.0.1", 5173
URL = f"http://{HOST}:{PORT}/"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot read console metadata: {path.name}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Console metadata must be an object: {path.name}")
    return value


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_origin(value: object, schemes: tuple[str, ...], *, allow_path: bool) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in schemes
            and bool(parsed.hostname)
            and "@" not in parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 0 < parsed.port <= 65535)
            and (allow_path or parsed.path in ("", "/"))
        )
    except ValueError:
        return False


def configured_strings(environment: dict, keys: tuple[str, ...]) -> bool:
    return all(
        isinstance(environment.get(key), str) and bool(environment[key].strip()) for key in keys
    )


def bootstrap(environment: dict) -> dict | None:
    # A missing session never falls back to a demo or fabricated roster.
    keys = ("SWEEP_RELAY_ORIGIN", "SWEEP_SESSION_ID", "SWEEP_RELAY_TOKEN")
    if not configured_strings(environment, keys):
        return None
    if (
        not valid_origin(environment[keys[0]], ("ws", "wss"), allow_path=True)
        or environment[keys[1]] != environment[keys[1]].strip()
    ):
        return None
    return dict(
        zip(("baseUrl", "sessionId", "token"), (environment[key] for key in keys), strict=True)
    )


def media_configuration(environment: dict) -> dict | None:
    keys = ("SWEEP_MEDIA_WEBRTC_ORIGIN", "SWEEP_MEDIA_READ_USERNAME", "SWEEP_MEDIA_READ_PASSWORD")
    if not configured_strings(environment, keys) or not valid_origin(
        environment[keys[0]], ("http", "https"), allow_path=False
    ):
        return None
    return dict(
        zip(
            ("webrtcOrigin", "readerUsername", "readerPassword"),
            (environment[key] for key in keys),
            strict=True,
        )
    )


class ConsoleHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, release: Path, environment: dict, version: dict, **kwargs):
        self.release = release.resolve()
        self.environment = environment
        self.version = version
        super().__init__(*args, directory=str(release / "dist"), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, _format, *_args):
        # Request paths and runtime credentials do not belong in the process log.
        pass

    def list_directory(self, _path):
        self.send_error(404)
        return None

    def send_head(self):
        route = urlsplit(self.path).path
        payload = None
        status = 200
        if route == "/relay-bootstrap.json":
            value = bootstrap(self.environment)
            payload, status = {"relay": value}, 200 if value else 503
        elif route == "/runtime-config.json":
            value = media_configuration(self.environment)
            payload, status = {"media": value}, 200 if value else 503
        elif route == "/console-version.json":
            payload = self.version
        if payload is not None:
            import io

            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)
        # No file outside the copied build is ever served, including symlinks.
        candidate = Path(self.translate_path(self.path)).resolve()
        if not candidate.is_relative_to(self.release / "dist"):
            self.send_error(404)
            return None
        return super().send_head()


def current_version() -> dict | None:
    try:
        with urlopen(URL + "console-version.json", timeout=1) as response:
            value = json.load(response)
        return value if value.get("application") == "sweep-console" else None
    except (URLError, OSError, ValueError, AttributeError):
        return None


def process_identity(pid: int) -> str:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip()


def artifact_digest(directory: Path) -> str:
    if directory.is_symlink() or not (directory / "index.html").is_file():
        raise RuntimeError("Console release is incomplete: index.html is missing")
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Console build must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        elif not path.is_dir():
            raise RuntimeError("Console build contains an unsupported file type")
    return digest.hexdigest()


def validate_release(release: Path, expected: dict) -> dict:
    metadata = read_json(release / "version.json")
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Console release metadata does not match its immutable build")
    if (
        release.is_symlink()
        or metadata.get("application") != "sweep-console"
        or metadata.get("source_directory") != str(ROOT)
        or metadata.get("release") != str(release)
        or release.parent != STATE / "releases"
        or metadata.get("build_id") != release.name
        or metadata.get("artifact_sha256") != artifact_digest(release / "dist")
    ):
        raise RuntimeError("Console release integrity check failed")
    return metadata


def build() -> dict:
    subprocess.run(["pnpm", "build"], cwd=ROOT / "console", check=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--", "console", "tools/console.py"], cwd=ROOT
        )
    )
    staging = STATE / "releases" / f".staging-{uuid4().hex}"
    staging.mkdir(parents=True, mode=0o700)
    try:
        # Preserve symlinks during copying so the validation rejects them rather
        # than copying their targets into a publicly served static artifact.
        source = ROOT / "console" / "dist"
        if source.is_symlink():
            raise RuntimeError("Console build directory must not be a symlink")
        shutil.copytree(source, staging / "dist", symlinks=True)
        digest = artifact_digest(staging / "dist")
        build_id = f"{revision[:12]}-{digest[:12]}" + ("-dirty" if dirty else "")
        release = STATE / "releases" / build_id
        metadata = {
            "application": "sweep-console",
            "build_id": build_id,
            "source_revision": revision,
            "source_directory": str(ROOT),
            "source_dirty": dirty,
            "artifact_sha256": digest,
            "url": URL,
            "data_mode": "real-relay-only",
            "release": str(release),
        }
        if release.exists():
            metadata = validate_release(release, metadata)
        else:
            metadata["built_at"] = datetime.now(UTC).isoformat()
            write_json(staging / "version.json", metadata)
            staging.rename(release)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    write_json(STATE / "active-build.json", metadata)
    print(f"Built {build_id}. Running console changes only after restart.")
    return metadata


def serve() -> None:
    active = read_json(STATE / "active-build.json")
    release_name = active.get("build_id")
    if not isinstance(release_name, str) or not re.fullmatch(
        r"[a-f0-9]{12}-[a-f0-9]{12}(?:-dirty)?", release_name
    ):
        raise RuntimeError("Console active build has an invalid release name")
    metadata = validate_release(STATE / "releases" / release_name, active)
    environment_path = STATE / "runtime.json"
    environment = read_json(environment_path) if environment_path.exists() else {}
    relay = bootstrap(environment)
    metadata.update(
        {
            "instance": str(uuid4()),
            "pid": os.getpid(),
            "relay_origin": relay["baseUrl"] if relay else None,
            "session": relay["sessionId"] if relay else None,
        }
    )
    handler = functools.partial(
        ConsoleHandler, release=Path(metadata["release"]), environment=environment, version=metadata
    )
    # Binding is the cross-checkout singleton. An occupied port is an error;
    # there is no port fallback and no relay/device lifecycle ownership here.
    server = ThreadingHTTPServer((HOST, PORT), handler)
    try:
        process_start = process_identity(os.getpid())
        if not process_start:
            raise RuntimeError("Cannot verify console process ownership")
        identity = {
            "pid": os.getpid(),
            "process_start": process_start,
            "instance": metadata["instance"],
        }
        write_json(STATE / "process.json", identity)
        print(f"Sweep console {metadata['build_id']} listening at {URL}", flush=True)
        server.serve_forever()
    finally:
        server.server_close()


def start() -> None:
    existing = current_version()
    if existing:
        if existing.get("source_directory") != str(ROOT):
            raise RuntimeError(
                f"Port 5173 belongs to another Sweep checkout: {existing.get('source_directory')}"
            )
        print(f"Already running {existing['build_id']} at {URL}")
        return
    if not (STATE / "active-build.json").exists():
        build()
    with (STATE / "console.log").open("a") as log:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_serve"],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    for _ in range(60):
        if child.poll() is not None:
            raise RuntimeError(
                f"Console could not bind port 5173. See {STATE / 'console.log'}; "
                "no alternate port was started."
            )
        version = current_version()
        if version and version.get("pid") == child.pid:
            print(f"Running {version['build_id']} at {URL}")
            return
        time.sleep(0.1)
    child.terminate()
    raise RuntimeError("Console startup timed out; process terminated.")


def stop() -> None:
    path = STATE / "process.json"
    if not path.exists():
        print("No console process owned by this launcher.")
        return
    owned = read_json(path)
    if (
        type(owned.get("pid")) is not int
        or owned["pid"] <= 0
        or not isinstance(owned.get("process_start"), str)
        or not owned["process_start"].strip()
        or not isinstance(owned.get("instance"), str)
        or not owned["instance"].strip()
    ):
        raise RuntimeError("Console process record is invalid; no process was stopped")
    if process_identity(owned["pid"]) != owned["process_start"]:
        path.unlink()
        print("Console is already stopped; removed stale process record.")
        return
    version = current_version()
    if version is not None and version.get("instance") != owned["instance"]:
        raise RuntimeError("Port 5173 belongs to another instance; no process was stopped.")
    os.kill(owned["pid"], signal.SIGTERM)
    for _ in range(50):
        if process_identity(owned["pid"]) != owned["process_start"]:
            path.unlink(missing_ok=True)
            print("Console stopped. Relay and devices are unchanged.")
            return
        time.sleep(0.1)
    raise RuntimeError("Console has not exited; no force-kill was issued.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("build", "start", "stop", "restart", "status", "_serve")
    )
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "_serve":
        serve()
        return
    with (STATE / "launcher.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == "build":
            build()
        elif args.command == "start":
            start()
        elif args.command == "stop":
            stop()
        elif args.command == "restart":
            stop()
            start()
        else:
            print(json.dumps(current_version() or {"running": False, "url": URL}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
