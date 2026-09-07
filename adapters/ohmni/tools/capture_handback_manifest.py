"""Capture non-secret Ohmni state needed to restore Sweep-owned changes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

VENDOR_NODE_DIRECTORY = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files"
VENDOR_SOURCE = f"{VENDOR_NODE_DIRECTORY}/telebot_node.js"
VENDOR_NODE_EXECUTABLE = f"{VENDOR_NODE_DIRECTORY}/node"
SWEEP_ROOT = "/data/local/sweep"
SWEEP_MUSL_EXECUTABLE = f"{SWEEP_ROOT}/lib/ld-musl-x86_64.so.1"
_ADB_TIMEOUT_S = 30


class CaptureError(RuntimeError):
    pass


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _adb(adb: str, serial: str, *args: str, input: bytes = b"") -> bytes:
    try:
        completed = subprocess.run(
            [adb, "-s", serial, *args],
            input=input,
            capture_output=True,
            check=True,
            timeout=_ADB_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise CaptureError("ADB collection timed out") from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise CaptureError("ADB collection failed") from error
    return completed.stdout


def _root(adb: str, serial: str, script: str) -> bytes:
    return _adb(adb, serial, "shell", "-T", "su", "0", "sh", input=script.encode())


def _metadata(adb: str, serial: str) -> dict[str, str]:
    output = _root(
        adb,
        serial,
        f"""set -eu
source={VENDOR_SOURCE}
printf 'sha256='; sha256sum "$source" | cut -d ' ' -f 1
printf 'owner_mode='; stat -c '%u:%g:%a' "$source"
printf 'selinux='; ls -Zd "$source" | awk '{{print $1}}'
""",
    )
    values: dict[str, str] = {}
    for raw_line in output.replace(b"\r", b"").splitlines():
        key, separator, value = raw_line.decode("ascii").partition("=")
        if separator != "=" or key not in {"sha256", "owner_mode", "selinux"} or not value:
            raise CaptureError("vendor metadata response is malformed")
        values[key] = value
    if set(values) != {"sha256", "owner_mode", "selinux"}:
        raise CaptureError("vendor metadata response is incomplete")
    if not _is_sha256(values["sha256"]):
        raise CaptureError("vendor metadata contains an invalid SHA-256")
    return values


def _vendor_bytes(adb: str, serial: str) -> bytes:
    encoded = _root(adb, serial, f"set -eu\nbase64 {VENDOR_SOURCE}\n")
    try:
        return base64.b64decode(b"".join(encoded.replace(b"\r", b"").split()), validate=True)
    except ValueError as error:
        raise CaptureError("vendor source response is not base64") from error


def _sweep_paths(adb: str, serial: str) -> list[str]:
    output = _root(
        adb,
        serial,
        f"""set -eu
if [ -d {SWEEP_ROOT} ]; then
  find {SWEEP_ROOT} -type f -print
fi
""",
    )
    paths: list[str] = []
    for raw_line in output.replace(b"\r", b"").splitlines():
        path = raw_line.decode("utf-8")
        if not path.startswith(f"{SWEEP_ROOT}/"):
            raise CaptureError("Sweep inventory returned an unexpected path")
        paths.append(path)
    return sorted(paths)


def _sweep_metadata(adb: str, serial: str) -> list[dict[str, str]]:
    output = _root(
        adb,
        serial,
        f"""set -eu
root={SWEEP_ROOT}
for file in "$root"/* "$root"/.* "$root"/adapters/ohmni/runtime.py \
  "$root"/adapters/ohmni/return_controller.py "$root"/adapters/ohmni/camera_runner.py \
  "$root"/adapters/ohmni/run.sh "$root"/adapters/ohmni/camera.sh \
  "$root"/adapters/ohmni/*.before-*; do
  [ -f "$file" ] || continue
  printf "%s\t%s\t%s\t%s\n" \
    "$file" \
    "$(sha256sum "$file" | cut -d " " -f 1)" \
    "$(stat -c "%u:%g:%a" "$file")" \
    "$(ls -Zd "$file" | cut -d " " -f 1)"
done
""",
    )
    files: list[dict[str, str]] = []
    for raw_line in output.replace(b"\r", b"").splitlines():
        fields = raw_line.decode("utf-8").split("\t")
        if len(fields) != 4 or not all(fields):
            raise CaptureError("Sweep metadata response is malformed")
        path, sha256, owner_mode, selinux = fields
        if not path.startswith(f"{SWEEP_ROOT}/"):
            raise CaptureError("Sweep metadata returned an unexpected path")
        if not _is_sha256(sha256):
            raise CaptureError("Sweep metadata contains an invalid SHA-256")
        files.append(
            {
                "path": path,
                "sha256": sha256,
                "owner_mode": owner_mode,
                "selinux": selinux,
            }
        )
    return sorted(files, key=lambda item: item["path"])


def _reverse_mappings(adb: str, serial: str) -> list[str]:
    output = _adb(adb, serial, "reverse", "--list")
    return sorted(
        line.decode("ascii") for line in output.replace(b"\r", b"").splitlines() if line.strip()
    )


def _classify_process(cwd: str, executable: str, args: str) -> str | None:
    if cwd == VENDOR_NODE_DIRECTORY and executable == VENDOR_NODE_EXECUTABLE:
        return "vendor_native_node"
    if cwd != SWEEP_ROOT or executable != SWEEP_MUSL_EXECUTABLE:
        return None
    if "adapters.ohmni.camera_runner" in args:
        return "sweep_camera"
    if "adapters.ohmni" in args:
        return "sweep_runtime"
    return None


def _process_context(adb: str, serial: str) -> dict[str, object]:
    output = _root(
        adb,
        serial,
        """set -eu
for process in /proc/[0-9]*; do
  [ -r "$process/cmdline" ] || continue
  pid=${process##*/}
  ppid=$(awk '/^PPid:/ {print $2}' "$process/status")
  cwd=$(readlink "$process/cwd" 2>/dev/null || true)
  executable=$(readlink "$process/exe" 2>/dev/null || true)
  args=$(base64 "$process/cmdline" | tr -d '\r\n')
  printf 'process=%s\t%s\t%s\t%s\t%s\n' "$pid" "$ppid" "$cwd" "$executable" "$args"
done
if command -v pidof >/dev/null 2>&1; then
  for pid in $(pidof com.ohmnilabs.telebot_rtc 2>/dev/null || true); do
    printf 'vendor_app=%s\n' "$pid"
  done
fi
for label in node camera; do
  file=/data/local/sweep/$label.pid
  [ -f "$file" ] || continue
  pid=$(cat "$file")
  printf 'pid_file_%s=%s\n' "$label" "$pid"
done
""",
    )
    process_by_pid: dict[int, tuple[int, str, str, str]] = {}
    vendor_app_pids: list[int] = []
    pid_files: dict[str, str] = {}
    for raw_line in output.replace(b"\r", b"").splitlines():
        name, separator, value = raw_line.decode("ascii").partition("=")
        if separator != "=":
            raise CaptureError("process context response is malformed")
        if name == "process":
            fields = value.split("\t")
            if len(fields) != 5 or not fields[0].isdecimal() or not fields[1].isdecimal():
                raise CaptureError("process context response is malformed")
            try:
                args = base64.b64decode(fields[4], validate=True).decode("utf-8", "surrogateescape")
            except ValueError as error:
                raise CaptureError("process context response is malformed") from error
            pid = int(fields[0])
            if pid in process_by_pid:
                raise CaptureError("process context response is malformed")
            process_by_pid[pid] = (int(fields[1]), fields[2], fields[3], args)
            continue
        if name == "vendor_app":
            if not value.isdecimal():
                raise CaptureError("process context response is malformed")
            vendor_app_pids.append(int(value))
            continue
        if not name.startswith("pid_file_"):
            raise CaptureError("process context response is malformed")
        label = name.removeprefix("pid_file_")
        if label not in {"node", "camera"} or label in pid_files:
            raise CaptureError("process context response is malformed")
        pid_files[label] = value

    observed: dict[str, list[dict[str, int]]] = {
        "vendor_app": [],
        "vendor_native_node": [],
        "sweep_runtime": [],
        "sweep_camera": [],
    }
    for pid, (ppid, cwd, executable, args) in process_by_pid.items():
        role = _classify_process(cwd, executable, args)
        if role is not None:
            observed[role].append({"pid": pid, "ppid": ppid})
    for pid in vendor_app_pids:
        if pid in process_by_pid:
            observed["vendor_app"].append({"pid": pid, "ppid": process_by_pid[pid][0]})
    for entries in observed.values():
        entries.sort(key=lambda entry: entry["pid"])

    classified = {entry["pid"]: role for role, entries in observed.items() for entry in entries}
    recorded_files: dict[str, dict[str, int | str]] = {}
    for label, raw_pid in pid_files.items():
        if not raw_pid.isdecimal():
            recorded_files[label] = {"state": "invalid"}
            continue
        pid = int(raw_pid)
        expected = "sweep_runtime" if label == "node" else "sweep_camera"
        if pid not in process_by_pid:
            state = "absent"
        elif classified.get(pid) == expected:
            state = "matched"
        else:
            state = "mismatched"
        recorded_files[label] = {"pid": pid, "state": state}
    return {"observed": observed, "pid_files": recorded_files}


def capture(adb: str, serial: str, unit: int, output: Path) -> Path:
    if unit < 1:
        raise CaptureError("unit must be positive")
    if output.exists():
        raise CaptureError("output directory already exists")
    _adb(adb, serial, "get-state")
    metadata = _metadata(adb, serial)
    source = _vendor_bytes(adb, serial)
    source_sha256 = hashlib.sha256(source).hexdigest()
    if source_sha256 != metadata["sha256"]:
        raise CaptureError("vendor source bytes do not match the root-shell hash")
    inventory = {
        "paths": _sweep_paths(adb, serial),
        "metadata": _sweep_metadata(adb, serial),
    }
    mappings = _reverse_mappings(adb, serial)
    processes = _process_context(adb, serial)
    output.mkdir(mode=0o700, parents=True)
    source_path = output / "telebot_node.js.original"
    source_path.write_bytes(source)
    source_path.chmod(0o600)
    manifest = {
        "v": 1,
        "kind": "ohmni_handback_capture",
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "unit": unit,
        "serial": serial,
        "vendor_source": {
            "path": VENDOR_SOURCE,
            "snapshot": source_path.name,
            **metadata,
        },
        "sweep_inventory": inventory,
        "adb_reverse_before": mappings,
        "process_context": processes,
        "notes": [
            "Sweep uses pid launchers and does not install an Android service.",
            "This capture does not inspect Android service registrations.",
            "This capture excludes private environment-file contents.",
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--unit", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--adb", default="adb")
    args = parser.parse_args()
    try:
        manifest = capture(args.adb, args.serial, args.unit, args.output)
    except CaptureError as error:
        parser.error(str(error))
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
