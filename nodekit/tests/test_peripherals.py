"""Signed peripheral admission stays independent of wheel motion generations."""

import asyncio

import pytest

from nodekit import protocol
from nodekit.node import WATCHDOG_HOLD
from nodekit.tests.test_runtime_safety import command, join, make_node


def rig():
    node, clock = make_node()
    node.device.capabilities.extend(["robot_peripheral_v1", "neck", "screen"])
    calls = []
    node.device.supported_peripherals = lambda: frozenset({"neck", "screen"})
    node.device.run_peripheral = lambda args: (
        calls.append(dict(args)) or "Submitted; physical output unreported"
    )
    join(node)
    while not node._outbound.empty():
        node._outbound.get_nowait()
    return node, clock, calls


def request(node, *, kind="screen", **changes):
    raw = command(node, "robot_peripheral")
    raw.update(
        args={"kind": "neck", "position": 512}
        if kind == "neck"
        else {"kind": "screen", "text": "hello"},
        **changes,
    )
    raw.pop("signature")
    raw["signature"] = protocol.sign_event(raw, "test-key")
    return raw


def drain(node):
    events = []
    while not node._outbound.empty():
        events.append(node._outbound.get_nowait())
    return events


def test_stationary_peripheral_runs_with_disabled_wheels_and_does_not_supersede_motion():
    asyncio.run(stationary_roundtrip())


async def stationary_roundtrip():
    node, _, calls = rig()
    node.device.disable()
    generation = node._motion_generation
    raw = request(node)
    node._handle_command(raw)
    await asyncio.gather(*node._commands)
    assert calls == [raw["args"]]
    assert node._motion_generation == generation
    assert not node.device.status().control_authority
    acknowledgements = drain(node)
    assert [event["status"] for event in acknowledgements] == ["accepted", "executing", "completed"]
    assert "physical output unreported" in acknowledgements[-1]["detail"]
    # Replay is refused by the normal signed sequence ledger and cannot speak twice.
    node._handle_command(raw)
    assert calls == [raw["args"]]
    assert drain(node)[-1]["status"] == "failed"


@pytest.mark.parametrize(
    "changes", [{"connection_epoch": 2}, {"seq": 0}, {"ttl_ms": 1, "issued_at": 90000}]
)
def test_stationary_operations_still_require_current_signed_admission(changes):
    asyncio.run(admission_roundtrip(changes))


async def admission_roundtrip(changes):
    node, _, calls = rig()
    node._handle_command(request(node, **changes))
    if node._commands:
        await asyncio.gather(*node._commands)
    assert calls == []


@pytest.mark.parametrize("kind", ["neck", "screen"])
def test_peripherals_require_current_watchdog_lease_and_no_local_action(kind):
    node, _, calls = rig()
    node._watchdog.state = WATCHDOG_HOLD
    node._handle_command(request(node, kind=kind))
    assert calls == []
    assert drain(node)[-1]["reason"] == "watchdog_hold"
