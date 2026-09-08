"""Check a heartbeat emitted by the relay against the real Kotlin phone parser."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from relay.app import RelayRuntime
from relay.auth import Principal, sign_event
from relay.settings import RelaySettings

_KEY = b"heartbeat-interop-adapter-key-0123456789"
_CLASS = "org.worldofhacks.sweep.bridge.core.frames.RelayHeartbeatInteropTest"
_SOURCE = """package org.worldofhacks.sweep.bridge.core.frames

import java.nio.file.Files
import java.nio.file.Path
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

class RelayHeartbeatInteropTest {
    @Test
    fun `the phone accepts the heartbeat emitted by the Python relay`() {
        val path = Path.of(requireNotNull(System.getenv("SWEEP_HEARTBEAT_INTEROP")))
        val wire = Json.parse(Files.readString(path)) as JsonObject
        val heartbeat = ControlHeartbeat.parse(wire)
        assertEquals("heartbeat-interop", heartbeat.session)
        assertEquals(1, heartbeat.droneId)
        assertEquals(1, heartbeat.connectionEpoch)
        assertEquals(1L, heartbeat.seq)
        assertTrue(heartbeat.verifies("heartbeat-interop-adapter-key-0123456789".toByteArray()))
        assertFalse(heartbeat.verifies("incorrect-key".toByteArray()))
    }
}
"""


async def _heartbeat(root: Path) -> dict[str, object]:
    settings = RelaySettings(
        relay_token=b"heartbeat-interop-console-key-0123456789",
        adapter_keys={1: _KEY},
        log_dir=root,
    )
    runtime = RelayRuntime(settings, clock=lambda: 2_500)
    session_id = "heartbeat-interop"
    principal = Principal(source="adapter", drone_id=1, signing_key=_KEY)
    try:
        session = runtime.session(session_id)
        subscription = await runtime.subscribe(session_id, principal)
        membership: dict[str, object] = {
            "v": 1,
            "t": 2_500,
            "type": "membership",
            "event_id": "join-interop",
            "session": session_id,
            "drone_id": 1,
            "action": "join",
            "adapter_id": "phone-interop",
            "capabilities": ["flight"],
        }
        session.process_membership(
            {**membership, "signature": sign_event(membership, _KEY)}, principal
        )
        await runtime._publish_control_heartbeats(session_id, session)
        event = subscription.queue.get_nowait().event
        if event.get("type") != "control_heartbeat":
            raise ValueError("relay did not emit an aircraft control heartbeat")
        return event
    finally:
        await runtime.stop()


def check(project: Path) -> None:
    project = project.resolve()
    source = project / (
        "bridge-core/src/test/kotlin/org/worldofhacks/sweep/bridge/core/frames/"
        "RelayHeartbeatInteropTest.kt"
    )
    if source.exists() or not (project / "gradlew").is_file():
        raise ValueError("Android project is unavailable or the temporary test already exists")
    with tempfile.TemporaryDirectory(prefix="phone-heartbeat-interop-") as temporary:
        root = Path(temporary)
        payload = root / "heartbeat.json"
        payload.write_text(json.dumps(asyncio.run(_heartbeat(root))) + "\n")
        source.write_text(_SOURCE)
        try:
            subprocess.run(
                [
                    str(project / "gradlew"),
                    ":bridge-core:test",
                    "--tests",
                    _CLASS,
                    "--no-daemon",
                    "--rerun-tasks",
                ],
                cwd=project,
                env={**os.environ, "SWEEP_HEARTBEAT_INTEROP": str(payload)},
                check=True,
                timeout=600,
            )
        finally:
            source.unlink(missing_ok=True)
    print("Python relay heartbeat accepted and signature verified by the Kotlin phone parser.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android-project", type=Path, required=True)
    arguments = parser.parse_args()
    check(arguments.android_project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
