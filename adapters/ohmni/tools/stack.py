"""Own only the Ohmni stack launched by this script; default ports are 8010 and 5174."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("SWEEP_OHMNI_STATE_DIR", str(ROOT / ".sweep-ohmni")))
HOST_PORTS = {"relay": 8010, "console": 5174}


def process_identity(pid: int) -> dict | None:
    """Read one leader identity without relying on a reusable PID or command substring."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pgid=,lstart=,command="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    fields = result.stdout.strip().split(maxsplit=6)
    if result.returncode or len(fields) != 7 or not fields[0].isdigit():
        raise RuntimeError(f"cannot establish process identity for PID {pid}")
    return {"pgid": int(fields[0]), "started_at": " ".join(fields[1:6]), "command": fields[6]}


def ownership_record(name: str) -> dict:
    record = json.loads((STATE / f"{name}.json").read_text())
    if (
        not isinstance(record, dict)
        or record.get("version") != 1
        or type(record.get("pid")) is not int
        or record["pid"] <= 0
        or type(record.get("pgid")) is not int
        or record.get("pgid") != record["pid"]
        or not isinstance(record.get("started_at"), str)
        or not record["started_at"]
        or not isinstance(record.get("marker"), str)
        or not record["marker"]
    ):
        raise RuntimeError(f"{name} has no complete process ownership record; leaving it alone")
    return record


def verify_owner(name: str, record: dict) -> dict | None:
    identity = process_identity(record["pid"])
    if identity is not None and (
        identity["pgid"] != record["pgid"]
        or identity["started_at"] != record["started_at"]
        or record["marker"] not in identity["command"]
    ):
        raise RuntimeError(f"{name} PID now belongs to another process; leaving it alone")
    return identity


def listener_pids(port: int) -> set[int]:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return set()
    listeners = {
        int(line[1:])
        for line in result.stdout.splitlines()
        if line.startswith("p") and line[1:].isdigit()
    }
    if result.returncode or not listeners:
        raise RuntimeError(f"cannot establish listener ownership for host port {port}")
    return listeners


def require_ports_available(ports: tuple[int, ...] = tuple(HOST_PORTS.values())) -> None:
    for port in ports:
        # macOS permits a wildcard SO_REUSEADDR bind beside a specific-address listener.
        # Check all IPv4/IPv6 listeners first, then probe whether the actual bind is free.
        if listener_pids(port):
            raise RuntimeError(f"host port {port} is occupied; no robot session was changed")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError as error:
                raise RuntimeError(
                    f"host port {port} is occupied; no robot session was changed"
                ) from error


def host_ready(name: str, port: int) -> bool:
    """A listener must belong to the recorded live process group, including its children."""
    record = ownership_record(name)
    if verify_owner(name, record) is None:
        return False
    listeners = listener_pids(port)
    if not listeners:
        return False
    for pid in listeners:
        identity = process_identity(pid)
        if identity is None:
            return False
        if identity["pgid"] != record["pgid"]:
            raise RuntimeError(f"host port {port} belongs to an unowned process; leaving it alone")
    return True


def validate_relay_endpoint(env: dict[str, str], name: str) -> None:
    """Configuration errors must not print private endpoint values or redirect to :8000."""
    try:
        endpoint = urlsplit(env.get(name, ""))
        valid = (
            endpoint.scheme in {"ws", "wss"}
            and endpoint.hostname
            and endpoint.port == HOST_PORTS["relay"]
            and endpoint.username is None
            and endpoint.password is None
            and endpoint.path in {"", "/"}
            and not endpoint.query
            and not endpoint.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            f"set {name} to the intended relay WebSocket origin on port 8010 "
            "in the private configuration before starting this stack"
        )


def validate_configuration(env: dict[str, str], rows: list[dict]) -> None:
    validate_relay_endpoint(env, "SWEEP_RELAY_ORIGIN")
    for row in rows:
        validate_relay_endpoint(env_file(Path(row["env_file"])), "SWEEP_RELAY_URL")


def wait_host(name: str, process: subprocess.Popen, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited before becoming ready; no robot session was changed")
        if host_ready(name, port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"{name} did not become ready on port {port}; no robot session was changed")


def env_file(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise ValueError("expected NAME=value in private environment file")
        parsed = shlex.split(value, comments=True)
        if len(parsed) > 1:
            raise ValueError(f"quote the value of {name}")
        env[name.strip()] = parsed[0] if parsed else ""
    return env


def robots() -> list[dict]:
    path = os.environ.get("SWEEP_ROBOTS_FILE")
    if not path:
        raise ValueError("set SWEEP_ROBOTS_FILE to private JSON [{serial, env_file}, ...]")
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError("robot list is empty")
    for row in rows:
        if set(row) != {"serial", "env_file"} or not row["serial"]:
            raise ValueError("each robot needs exactly serial and env_file")
    return rows


def adb(row: dict, *args: str) -> None:
    subprocess.run(
        ["adb", "-s", row["serial"], *args], check=True, stdout=subprocess.DEVNULL, timeout=30
    )


def nodes(rows: list[dict], session: str, *, stop: bool = False) -> None:
    for row in rows:
        adb(row, "shell", "/data/local/sweep/run.sh stop")
        if stop:
            continue
        env = env_file(Path(row["env_file"]))
        env["SWEEP_SESSION_ID"] = session
        # Preserve each robot's actual spotter, launch pose, calibration and key. Never
        # fabricate claims, derive device IDs from IPs, or auto-claim a spotter here.
        path = STATE / f"node-{int(env['SWEEP_DEVICE_ID'])}.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(
                "\n".join(f"{key}={shlex.quote(value)}" for key, value in env.items()) + "\n"
            )
        try:
            adb(row, "push", str(path), "/data/local/sweep/node.env")
            adb(row, "shell", "/data/local/sweep/run.sh start")
        finally:
            path.unlink(missing_ok=True)


def stop_host(name: str) -> None:
    path = STATE / f"{name}.json"
    if not path.exists():
        return
    record = ownership_record(name)
    if verify_owner(name, record) is not None:
        # Recheck the group immediately before signaling. start_new_session makes its
        # leader PID equal the PGID; old/incomplete records never authorize a kill.
        if os.getpgid(record["pid"]) != record["pgid"]:
            raise RuntimeError(f"{name} changed process groups; leaving it alone")
        os.killpg(record["pgid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while verify_owner(name, record) is not None:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name} did not stop; no new session will be launched")
            time.sleep(0.1)
    path.unlink()


def start_host(name: str, args: list[str], cwd: Path, env: dict, marker: str) -> subprocess.Popen:
    with (STATE / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=log, stderr=log, start_new_session=True
        )
    identity = process_identity(process.pid)
    if identity is None or identity["pgid"] != process.pid or marker not in identity["command"]:
        raise RuntimeError(f"cannot establish ownership of newly launched {name}")
    (STATE / f"{name}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": process.pid,
                "pgid": identity["pgid"],
                "started_at": identity["started_at"],
                "marker": marker,
            }
        )
    )
    return process


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "nodes", "stop"), default="start", nargs="?")
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    rows = robots()
    session_path = STATE / "session"
    if args.action == "stop":
        nodes(rows, "", stop=True)
        stop_host("console")
        stop_host("relay")
        return
    relay_env_path = Path(os.environ["SWEEP_RELAY_ENV_FILE"]).resolve()
    env = {**os.environ, **env_file(relay_env_path)}
    validate_configuration(env, rows)
    if args.action == "nodes":
        for name, port in HOST_PORTS.items():
            if not host_ready(name, port):
                raise RuntimeError(f"{name} is not ready; no robot session was changed")
        nodes(rows, session_path.read_text().strip())
        return
    session = f"sweep-{time.time_ns() // 1_000_000}"
    env["SWEEP_SESSION_ID"] = session
    stop_host("console")
    stop_host("relay")
    require_ports_available()
    # MediaMTX stays in its repository compose service; no runtime is installed on robots.
    subprocess.run(["docker", "compose", "up", "-d", "mediamtx"], cwd=ROOT, env=env, check=True)
    relay_process = start_host(
        "relay",
        ["uv", "run", "python", "-m", "relay.main", "--host", "0.0.0.0", "--port", "8010"],
        ROOT,
        env,
        "relay.main",
    )
    console_process = start_host(
        "console",
        ["pnpm", "dev", "--host", "0.0.0.0", "--port", "5174", "--strictPort"],
        ROOT / "console",
        env,
        "pnpm",
    )
    wait_host("relay", relay_process, HOST_PORTS["relay"])
    wait_host("console", console_process, HOST_PORTS["console"])
    # Both leaders/listeners must still match after the other service starts.
    for name, port in HOST_PORTS.items():
        if not host_ready(name, port):
            raise RuntimeError(f"{name} stopped before node startup; no robot session was changed")
    session_path.write_text(session + "\n")
    nodes(rows, session)
    print(f"Started {session}; relay :8010, console :5174. Logs: {STATE}")


if __name__ == "__main__":
    main()
