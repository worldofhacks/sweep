"""Own only the Ohmni stack launched by this script; default ports are 8010 and 5174."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE = Path(os.environ.get("SWEEP_OHMNI_STATE_DIR", str(ROOT / ".sweep-ohmni")))


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
    record = json.loads(path.read_text())
    pid = int(record["pid"])
    proc = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    if proc.returncode == 0:
        if record["marker"] not in proc.stdout:
            raise RuntimeError(f"{name} PID now belongs to another process; leaving it alone")
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.3)
    path.unlink()


def start_host(name: str, args: list[str], cwd: Path, env: dict, marker: str) -> None:
    with (STATE / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(
            args, cwd=cwd, env=env, stdout=log, stderr=log, start_new_session=True
        )
    (STATE / f"{name}.json").write_text(json.dumps({"pid": process.pid, "marker": marker}))


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
    if args.action == "nodes":
        nodes(rows, session_path.read_text().strip())
        return
    relay_env_path = Path(os.environ["SWEEP_RELAY_ENV_FILE"]).resolve()
    env = {**os.environ, **env_file(relay_env_path)}
    session = f"sweep-{time.time_ns() // 1_000_000}"
    env["SWEEP_SESSION_ID"] = session
    session_path.write_text(session + "\n")
    stop_host("console")
    stop_host("relay")
    # MediaMTX stays in its repository compose service; no runtime is installed on robots.
    subprocess.run(["docker", "compose", "up", "-d", "mediamtx"], cwd=ROOT, env=env, check=True)
    start_host(
        "relay",
        ["uv", "run", "python", "-m", "relay.main", "--host", "0.0.0.0", "--port", "8010"],
        ROOT,
        env,
        "relay.main",
    )
    start_host(
        "console",
        ["pnpm", "dev", "--host", "0.0.0.0", "--port", "5174", "--strictPort"],
        ROOT / "console",
        env,
        "pnpm",
    )
    nodes(rows, session)
    print(f"Started {session}; relay :8010, console :5174. Logs: {STATE}")


if __name__ == "__main__":
    main()
