from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from planner.models import CommandOperation
from relay.auth import sign_event
from relay.contracts import command_event
from relay.tests.conftest import SESSION

from .fake import FakeGroundDevice
from .runtime import GroundRuntimeConfig, OhmniRuntime

GROUND_ID = 9
GROUND_KEY = b"ground-adapter-key-that-is-at-least-32-bytes"


def _runtime_for_local_test() -> tuple[OhmniRuntime, FakeGroundDevice]:
    device = FakeGroundDevice()
    runtime = OhmniRuntime(
        GroundRuntimeConfig(
            relay_url="ws://relay.example",
            session=SESSION,
            device_id=GROUND_ID,
            token=GROUND_KEY.decode(),
            adapter_id="fake-ohmni-9",
            lidar_mount_x_m=0.1,
            lidar_mount_y_m=0.0,
            lidar_mount_z_m=0.25,
            lidar_mount_yaw_deg=0.0,
        ),
        device,
    )
    runtime._epoch = 1
    runtime._roster_version = 1
    runtime._outbound = asyncio.Queue()
    return runtime, device


def _command(runtime: OhmniRuntime, *, operation: CommandOperation, seq: int) -> dict[str, object]:
    now = int(time.time_ns() // 1_000_000)
    unsigned = command_event(
        t=now,
        event_id=f"command-{seq}",
        session=SESSION,
        command_id=f"command-{seq}",
        intent_id=f"intent-{seq}",
        roster_version=1,
        drone_id=GROUND_ID,
        connection_epoch=1,
        seq=seq,
        issued_at=now,
        ttl_ms=1_000,
        operation=operation,
        args=(
            {"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 25}
            if operation is CommandOperation.GROUND_VELOCITY
            else {}
        ),
    )
    return {**unsigned, "signature": sign_event(unsigned, GROUND_KEY)}


def _heartbeat(runtime: OhmniRuntime, *, seq: int) -> dict[str, object]:
    now = int(time.time_ns() // 1_000_000)
    unsigned = {
        "v": 1,
        "t": now,
        "type": "control_heartbeat",
        "event_id": f"heartbeat-{seq}",
        "session": SESSION,
        "source": "relay",
        "drone_id": GROUND_ID,
        "connection_epoch": 1,
        "roster_version": 1,
        "seq": seq,
        "issued_at": now,
        "expires_at": now + runtime.config.heartbeat_failsafe_ms,
        "hold_after_ms": runtime.config.heartbeat_hold_ms,
        "failsafe_after_ms": runtime.config.heartbeat_failsafe_ms,
    }
    return {**unsigned, "signature": sign_event(unsigned, GROUND_KEY)}


def test_estop_latches_until_a_local_operator_rearm_and_stops_bypass_the_lease_gate() -> None:
    runtime, device = _runtime_for_local_test()
    runtime._on_heartbeat(_heartbeat(runtime, seq=1))
    assert device.enabled

    runtime._on_command(_command(runtime, operation=CommandOperation.ESTOP, seq=1))
    assert not device.enabled
    runtime._on_heartbeat(_heartbeat(runtime, seq=2))
    assert not device.enabled

    runtime.rearm_after_operator_confirmation()
    runtime._on_heartbeat(_heartbeat(runtime, seq=3))
    assert device.enabled

    runtime._local_stop("test", disable=True)
    runtime._on_command(_command(runtime, operation=CommandOperation.HOVER, seq=2))
    assert device.stopped
    acknowledgements = [
        runtime._outbound.get_nowait()  # type: ignore[union-attr]
        for _ in range(runtime._outbound.qsize())  # type: ignore[union-attr]
    ]
    assert any(
        frame["type"] == "acknowledgement"
        and frame["intent_id"] == "intent-2"
        and frame["status"] == "completed"
        for frame in acknowledgements
    )


def test_observations_keep_sensor_evidence_distinct_from_camera_metadata() -> None:
    runtime, device = _runtime_for_local_test()
    scan = device.latest_scan()
    device.latest_scan = lambda: scan  # type: ignore[method-assign]
    runtime._publish_observations()
    runtime._publish_observations()
    frames = [
        runtime._outbound.get_nowait()  # type: ignore[union-attr]
        for _ in range(runtime._outbound.qsize())  # type: ignore[union-attr]
    ]
    observations = [frame for frame in frames if frame["type"] == "observation"]
    payloads = [frame["payload"] for frame in observations]
    assert [payload["kind"] for payload in payloads].count("camera_frame") == 0
    scans = [frame for frame in observations if frame["payload"]["kind"] == "range_scan"]
    assert len(scans) == 1
    assert scans[0]["t_source_receipt"]["unit"] == "ms"
    assert scans[0]["payload"]["sensor_pose"]["x_m"] == pytest.approx(0.1)
    assert scans[0]["payload"]["sensor_pose"]["z_m"] == pytest.approx(0.25)
    assert all(
        frame["confidence"] == pytest.approx(device.status().pos_quality) for frame in observations
    )


def test_drive_io_failure_after_accepted_command_stops_and_reports_failed_terminal_ack() -> None:
    runtime, device = _runtime_for_local_test()
    runtime._on_heartbeat(_heartbeat(runtime, seq=1))

    def fail_drive(*_args: object) -> str:
        raise OSError("serial link lost")

    device.drive_velocity = fail_drive  # type: ignore[method-assign]
    runtime._on_command(_command(runtime, operation=CommandOperation.GROUND_VELOCITY, seq=1))

    frames = [
        runtime._outbound.get_nowait()  # type: ignore[union-attr]
        for _ in range(runtime._outbound.qsize())  # type: ignore[union-attr]
    ]
    acknowledgements = [
        frame
        for frame in frames
        if frame["type"] == "acknowledgement" and frame["intent_id"] == "intent-1"
    ]
    assert [frame["status"] for frame in acknowledgements] == ["accepted", "failed"]
    assert acknowledgements[-1]["reason"] == "local_guard_refused"
    assert not device.enabled
    assert device.stopped


def test_motion_io_failure_after_executing_command_stops_and_reports_failed_terminal_ack() -> None:
    runtime, device = _runtime_for_local_test()
    runtime._on_heartbeat(_heartbeat(runtime, seq=1))

    def fail_completion(_identity: str) -> bool | None:
        raise OSError("serial link lost")

    device.motion_done = fail_completion  # type: ignore[method-assign]
    asyncio.run(runtime._complete_motion(_command_frame(), "motion-1"))

    frames = [
        runtime._outbound.get_nowait()  # type: ignore[union-attr]
        for _ in range(runtime._outbound.qsize())  # type: ignore[union-attr]
    ]
    acknowledgements = [frame for frame in frames if frame["type"] == "acknowledgement"]
    assert acknowledgements[-1]["status"] == "failed"
    assert acknowledgements[-1]["reason"] == "motion_failed"
    assert not device.enabled
    assert device.stopped


def _command_frame():
    from relay.contracts import parse_command

    runtime, _ = _runtime_for_local_test()
    return parse_command(_command(runtime, operation=CommandOperation.GROUND_VELOCITY, seq=1))


def test_lost_pose_confidence_stops_the_ground_runtime_and_withdraws_readiness() -> None:
    runtime, device = _runtime_for_local_test()
    runtime._on_heartbeat(_heartbeat(runtime, seq=1))
    device.lidar_available = False

    runtime._publish_observations()

    assert not device.enabled
    assert device.stopped
    frames = [
        runtime._outbound.get_nowait()  # type: ignore[union-attr]
        for _ in range(runtime._outbound.qsize())  # type: ignore[union-attr]
    ]
    readiness = [frame for frame in frames if frame["type"] == "membership"]
    assert readiness[-1]["drive_authority"] is False
    assert readiness[-1]["heartbeat_ready"] is False
    assert readiness[-1]["pose_identity"]["source_id"] == runtime.config.pose_source_id


def test_sigterm_stops_the_runtime_before_the_process_exits(tmp_path: Path) -> None:
    marker = tmp_path / "stopped"
    program = """import signal
import sys
from pathlib import Path
from adapters.ohmni.runtime import _install_sigterm_stop

class Node:
    stopped = False
    def stop(self):
        self.stopped = True
        Path(sys.argv[1]).write_text("stopped")

node = Node()
restore = _install_sigterm_stop(node)
print("ready", flush=True)
while not node.stopped:
    signal.pause()
restore()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(marker)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        process.terminate()
        assert process.wait(timeout=2) == 0
        assert marker.read_text() == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_local_drive_stops_before_waiting_for_websocket_close(monkeypatch) -> None:
    from . import runtime as module

    runtime, device = _runtime_for_local_test()
    device.enable()
    device.stopped = False
    observed_at_close = []

    class Connection:
        incoming = iter(({"type": "auth.accepted"}, {"type": "state", "roster_version": 1}))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            observed_at_close.append((device.enabled, device.stopped))

        async def send(self, raw):
            if json.loads(raw).get("action") == "join":
                runtime.stop()

        async def recv(self):
            return json.dumps(next(self.incoming))

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Future()

    monkeypatch.setattr(module, "connect", lambda *_args, **_kwargs: Connection())
    asyncio.run(runtime.run())
    assert observed_at_close == [(False, True)]
