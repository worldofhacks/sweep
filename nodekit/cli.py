"""Run one device as a node against a relay.

    uv run python -m nodekit.cli --device-id 11 --device ground

``--device`` names either a fixture (``ground`` or ``aircraft`` from ``nodekit.fake``, for
wiring checks before hardware is on the bench) or a real integration as
``module:factory``, which is imported and called with no arguments and must return a
``nodekit.device.Device``:

    uv run python -m nodekit.cli --device-id 11 --device adapters.ohmni.device:build

The device key is read from ``SWEEP_NODE_KEY`` or ``--key-file``; it is entered on the
device by a person and is never printed, logged, or echoed by this program.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from nodekit.device import Device
from nodekit.fake import FakeAircraft, FakeGroundVehicle
from nodekit.node import Node, NodeConfig, NodeError

_LOGGER = logging.getLogger(__name__)


def load_device(spec: str, capabilities: Sequence[str] | None) -> Device:
    """Build the device named by ``--device``."""
    if spec in {"ground", "ground_vehicle"}:
        if capabilities:
            return FakeGroundVehicle(capabilities=tuple(capabilities))
        return FakeGroundVehicle()
    if spec == "aircraft":
        if capabilities:
            return FakeAircraft(capabilities=tuple(capabilities))
        return FakeAircraft()
    if ":" not in spec:
        raise SystemExit(
            "--device must be ground, aircraft, or a module:factory path such as "
            "adapters.ohmni.device:build"
        )
    module_name, _, attribute = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise SystemExit(f"cannot import {module_name}: {error}") from None
    factory = getattr(module, attribute, None)
    if factory is None or not callable(factory):
        raise SystemExit(f"{module_name} has no callable {attribute}")
    return factory()


def resolve_key(device_id: int, key_file: str | None) -> str:
    """The device's relay key, from the environment or a file, never from an argument."""
    if key_file:
        key = Path(key_file).read_text(encoding="utf-8").strip()
        if key:
            return key
        raise SystemExit(f"{key_file} is empty")
    key = os.environ.get("SWEEP_NODE_KEY", "").strip()
    if key:
        return key
    try:
        keys = json.loads(os.environ.get("SWEEP_ADAPTER_KEYS_JSON", "{}"))
    except json.JSONDecodeError:
        keys = {}
    if isinstance(keys, dict):
        configured = keys.get(str(device_id))
        if isinstance(configured, str) and configured:
            return configured
    shared = os.environ.get("SWEEP_RELAY_TOKEN", "")
    if shared:
        return shared
    raise SystemExit(
        "no credential: set SWEEP_NODE_KEY on the device, pass --key-file, or set "
        "SWEEP_ADAPTER_KEYS_JSON for this device id"
    )


def parse_args(argv: Sequence[str] | None = None) -> tuple[NodeConfig, Device]:
    parser = argparse.ArgumentParser(
        prog="python -m nodekit.cli",
        description="Run a device as a node on a Sweep relay session.",
    )
    parser.add_argument(
        "--relay",
        default=os.environ.get("SWEEP_RELAY_URL", "ws://127.0.0.1:8000"),
        help="relay WebSocket origin (SWEEP_RELAY_URL)",
    )
    parser.add_argument(
        "--session",
        default=os.environ.get("SWEEP_SESSION_ID", "demo"),
        help="relay session ID (SWEEP_SESSION_ID)",
    )
    parser.add_argument(
        "--device-id",
        type=int,
        default=_int_environment("SWEEP_DEVICE_ID"),
        help="stable positive device ID (SWEEP_DEVICE_ID)",
    )
    parser.add_argument(
        "--device",
        default="ground",
        help="ground, aircraft, or a module:factory path returning a Device",
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=None,
        help="capability the fixture device claims; repeat to build the whole list",
    )
    parser.add_argument("--key-file", default=None, help="file holding the device's relay key")
    parser.add_argument("--adapter-id", default=None, help="adapter_id sent in the signed join")
    parser.add_argument("--telemetry-hz", type=float, default=10.0, help="telemetry rate")
    parser.add_argument("--sensor-hz", type=float, default=5.0, help="scan rate, at most 5")
    parser.add_argument(
        "--no-safety-operator",
        action="store_true",
        help="claim that no spotter or safety pilot is present, which blocks readiness",
    )
    args = parser.parse_args(argv)
    if args.device_id is None:
        parser.error("--device-id is required (or set SWEEP_DEVICE_ID)")
    device = load_device(args.device, args.capability)
    config = NodeConfig(
        relay_url=args.relay.rstrip("/"),
        session=args.session,
        device_id=args.device_id,
        token=resolve_key(args.device_id, args.key_file),
        adapter_id=args.adapter_id or f"nodekit-{args.device_id}",
        telemetry_hz=args.telemetry_hz,
        sensor_hz=args.sensor_hz,
        safety_operator_present=not args.no_safety_operator,
    )
    return config, device


def _int_environment(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config, device = parse_args(argv)
    node = Node(config, device)
    _LOGGER.info(
        "connecting %s %s to %s/ws/%s",
        device.device_class,
        config.device_id,
        config.relay_url,
        config.session,
    )
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        return 0
    except (NodeError, OSError) as error:
        _LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
