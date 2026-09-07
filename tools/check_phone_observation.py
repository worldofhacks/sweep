"""Check a Kotlin relay-link test packet against the authenticated Python relay."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from relay.app import create_app
from relay.auth import sign_event
from relay.observation_ingress import ObservationConfiguration
from relay.observations import FrameDeclaration, FrameRegistry, SourceBinding
from relay.settings import RelaySettings


def receive_until(
    socket, predicate: Callable[[dict[str, object]], bool], description: str
) -> dict[str, object]:
    for _ in range(20):
        event: dict[str, object] = socket.receive_json()
        if predicate(event):
            return event
    raise AssertionError(f"did not receive {description}")


def check(path: Path) -> None:
    encoded = path.read_bytes()
    assert len(encoded) <= 65_536
    packet = json.loads(encoded)
    session = packet["session"]
    assert packet["device_id"] == 1 and packet["connection_epoch"] == 1
    assert packet["source_id"] == "dji-telemetry" and packet["frame"] == "dji_enu"
    assert packet["t_capture"] is None and packet["clock_mapping_id"] is None
    assert packet["t_source_receipt"]["clock_id"] == "phone_snapshot_wall_ms"
    key = b"phone-observation-interop-test-key"
    now = packet["t_source_receipt"]["value"] + 10
    configuration = ObservationConfiguration(
        bindings=(
            SourceBinding(session, 1, 1, "dji-telemetry", "aircraft", ("dji_enu",), ("telemetry",)),
        ),
        frames=FrameRegistry(
            (
                FrameDeclaration(
                    "dji_enu",
                    "legacy_map_enu",
                    "east_north_up",
                    "m",
                    session,
                    1,
                    1,
                    "dji-telemetry",
                ),
            )
        ),
    )
    with TemporaryDirectory() as directory:
        settings = RelaySettings(
            relay_token=b"phone-observation-console-test-key",
            adapter_keys={1: key},
            log_dir=Path(directory),
            observation_configuration=configuration,
        )
        app = create_app(settings, clock=lambda: now)
        with (
            TestClient(app) as client,
            client.websocket_connect(f"/ws/{session}") as console,
            client.websocket_connect(f"/ws/{session}") as adapter,
        ):
            console.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "console",
                    "token": b"phone-observation-console-test-key".decode(),
                }
            )
            receive_until(
                console, lambda event: event["type"] == "auth.accepted", "console auth.accepted"
            )
            receive_until(console, lambda event: event["type"] == "state", "console initial state")
            adapter.send_json(
                {
                    "v": 1,
                    "type": "auth",
                    "source": "adapter",
                    "drone_id": 1,
                    "token": key.decode(),
                }
            )
            receive_until(
                adapter, lambda event: event["type"] == "auth.accepted", "adapter auth.accepted"
            )
            receive_until(adapter, lambda event: event["type"] == "state", "adapter initial state")
            join = {
                "v": 1,
                "t": now,
                "type": "membership",
                "event_id": "interop-join",
                "session": session,
                "drone_id": 1,
                "action": "join",
                "adapter_id": "phone-interop",
                "capabilities": ["flight"],
            }
            adapter.send_json({**join, "signature": sign_event(join, key)})
            receive_until(
                adapter,
                lambda event: event["type"] == "membership" and event["action"] == "join",
                "join membership",
            )
            adapter.send_text(encoded.decode())
            accepted = receive_until(
                console, lambda event: event["type"] == "observation", "accepted observation"
            )
            assert accepted == {**packet, "t_ingest": now}
            adapter.send_text(encoded.decode())
            refusal = receive_until(
                adapter, lambda event: event["type"] == "refusal", "replay refusal"
            )
            assert refusal["reason"] == "replayed_observation"
            audit = app.state.relay_runtime.session(session).audit_log.replay()
            accepted = [row["event"] for row in audit if row["event"]["type"] == "observation"]
            assert accepted == [{**packet, "t_ingest": now}]
    print("Kotlin phone observation accepted unchanged, relay-stamped and replay-protected.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    check(parser.parse_args().packet)
