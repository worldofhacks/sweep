from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

from .tools import capture_handback_manifest
from .tools.capture_handback_manifest import (
    SWEEP_MUSL_EXECUTABLE,
    SWEEP_ROOT,
    VENDOR_NODE_DIRECTORY,
    VENDOR_NODE_EXECUTABLE,
    CaptureError,
    _classify_process,
    capture,
)


def _adb(tmp_path: Path, source: bytes, *, mismatched_hash: bool = False) -> Path:
    adb = tmp_path / "adb"
    sha256 = "0" * 64 if mismatched_hash else hashlib.sha256(source).hexdigest()
    run_sha256 = hashlib.sha256(b"run").hexdigest()
    environment_sha256 = hashlib.sha256(b"environment").hexdigest()
    inventory = "\n".join(
        (
            f"/data/local/sweep/run.sh\t{run_sha256}\t0:0:700\tu:object_r:system_file:s0",
            f"/data/local/sweep/node.env\t{environment_sha256}\t0:0:600\tu:object_r:system_file:s0",
        )
    )
    process_context = "\n".join(
        (
            "process=2029\t1298\t/data/data/com.ohmnilabs.telebot_rtc\t/system/bin/app_process32\t",
            "process=2511\t1\t/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files\t"
            "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/node\t"
            + base64.b64encode(b"node").decode(),
            "process=6397\t1\t/data/local/sweep\t/data/local/sweep/lib/ld-musl-x86_64.so.1\t"
            + base64.b64encode(b"python -m adapters.ohmni").decode(),
            "process=6410\t1\t/data/local/sweep\t/data/local/sweep/lib/ld-musl-x86_64.so.1\t"
            + base64.b64encode(b"python -m adapters.ohmni.camera_runner").decode(),
            "vendor_app=2029",
            "pid_file_node=6397",
            "pid_file_camera=6410",
        )
    )
    adb.write_text(
        f"#!{sys.executable}\n"
        "import base64, sys\n"
        "args = sys.argv[1:]\n"
        "script = sys.stdin.buffer.read()\n"
        "source_path = (b'/data/data/com.ohmnilabs.telebot_rtc/'\n"
        "               b'files/assets/node-files/telebot_node.js')\n"
        "if args[2:] == ['get-state']:\n"
        "    print('device')\n"
        "elif args[2:] == ['reverse', '--list']:\n"
        "    print('10.10.0.74:5555 tcp:8554 tcp:18554')\n"
        "elif b\"printf 'sha256='\" in script:\n"
        f"    print('sha256={sha256}')\n"
        "    print('owner_mode=1000:1000:600')\n"
        "    print('selinux=u:object_r:system_app_data_file:s0')\n"
        "elif b'base64 ' + source_path in script:\n"
        f"    print(base64.b64encode({source!r}).decode())\n"
        "elif b'-type f -print' in script:\n"
        "    print('/data/local/sweep/run.sh')\n"
        "    print('/data/local/sweep/node.env')\n"
        "elif b'for file in' in script:\n"
        f"    print({inventory!r})\n"
        "elif b'/proc/[0-9]*' in script:\n"
        f"    print({process_context!r})\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    adb.chmod(0o700)
    return adb


def test_capture_writes_a_nonsecret_manifest_and_exact_vendor_snapshot(tmp_path: Path) -> None:
    source = b"original vendor source\n"
    manifest_path = capture(
        str(_adb(tmp_path, source)), "10.10.0.74:5555", 12, tmp_path / "capture"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["unit"] == 12
    assert manifest["vendor_source"] == {
        "owner_mode": "1000:1000:600",
        "path": "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/telebot_node.js",
        "selinux": "u:object_r:system_app_data_file:s0",
        "sha256": hashlib.sha256(source).hexdigest(),
        "snapshot": "telebot_node.js.original",
    }
    assert manifest["adb_reverse_before"] == ["10.10.0.74:5555 tcp:8554 tcp:18554"]
    assert manifest["process_context"] == {
        "observed": {
            "sweep_camera": [{"pid": 6410, "ppid": 1}],
            "sweep_runtime": [{"pid": 6397, "ppid": 1}],
            "vendor_app": [{"pid": 2029, "ppid": 1298}],
            "vendor_native_node": [{"pid": 2511, "ppid": 1}],
        },
        "pid_files": {
            "camera": {"pid": 6410, "state": "matched"},
            "node": {"pid": 6397, "state": "matched"},
        },
    }
    assert "sweep_services_before" not in manifest
    assert manifest["sweep_inventory"]["paths"] == [
        "/data/local/sweep/node.env",
        "/data/local/sweep/run.sh",
    ]
    assert len(manifest["sweep_inventory"]["metadata"]) == 2
    assert (manifest_path.parent / "telebot_node.js.original").read_bytes() == source
    assert (manifest_path.parent / "telebot_node.js.original").stat().st_mode & 0o777 == 0o600
    assert manifest_path.stat().st_mode & 0o777 == 0o600


def test_process_identity_uses_vendor_cwd_and_executable_not_a_source_name() -> None:
    assert (
        _classify_process(VENDOR_NODE_DIRECTORY, VENDOR_NODE_EXECUTABLE, "node")
        == "vendor_native_node"
    )
    assert _classify_process("/tmp", "/tmp/node", "telebot_node.js") is None
    assert (
        _classify_process(
            SWEEP_ROOT,
            SWEEP_MUSL_EXECUTABLE,
            "python -m adapters.ohmni.camera_runner",
        )
        == "sweep_camera"
    )


def test_capture_refuses_a_transport_snapshot_that_disagrees_with_root_hash(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="do not match"):
        capture(
            str(_adb(tmp_path, b"original vendor source\n", mismatched_hash=True)),
            "10.10.0.74:5555",
            12,
            tmp_path / "capture",
        )
    assert not (tmp_path / "capture").exists()


def test_capture_times_out_without_creating_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adb = tmp_path / "slow-adb"
    adb.write_text(
        f"#!{sys.executable}\nimport time\nprint('private', flush=True)\ntime.sleep(1)\n"
    )
    adb.chmod(0o700)
    monkeypatch.setattr(capture_handback_manifest, "_ADB_TIMEOUT_S", 0.01)
    with pytest.raises(CaptureError, match="ADB collection timed out") as error:
        capture(str(adb), "10.10.0.74:5555", 12, tmp_path / "capture")
    assert "private" not in str(error.value)
    assert not (tmp_path / "capture").exists()
