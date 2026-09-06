"""Hardware bring-up regressions: clock drift, source order, latches, and delayed work."""

from __future__ import annotations

import asyncio

import pytest

from nodekit import protocol
from nodekit.fake import FakeGroundVehicle
from nodekit.node import WATCHDOG_FAILSAFE, WATCHDOG_HOLD, Node, NodeConfig, NodeError


class Clock:
    seconds = 100.0

    def __call__(self):
        return self.seconds


def make_node(clock=None):
    clock = clock or Clock()
    node = Node(
        NodeConfig(
            "ws://localhost",
            "test",
            11,
            "test-key",
            "test-ground",
            clock_ms=lambda: 133_000,
            monotonic=clock,
        ),
        FakeGroundVehicle(),
    )
    node._outbound = asyncio.Queue()
    node._anchor_clock(100_000)
    return node, clock


def join(node, epoch=1):
    node._handle_membership({"action": "join", "connection_epoch": epoch, "roster_version": 1})


def test_relay_anchor_ignores_android_clock_drift_and_timestamps_are_strict():
    node, clock = make_node()
    assert node.now_t() == 100_001
    assert node.now_t() == 100_002
    clock.seconds += 0.5
    assert node.relay_now_ms() == 100_500
    assert node.now_t() == 100_500
    node._observe_clock({"t": 900_000})  # future floor must never poison our source clock
    assert node.now_t() == 100_501


def test_bounded_timestamp_budget_fails_without_duplicate_or_regression():
    node, _ = make_node()
    frames = [node.now_t() for _ in range(500)]
    assert len(set(frames)) == 500
    with pytest.raises(NodeError, match="bounded clock lead"):
        node.now_t()
    assert node._last_t == frames[-1]


def test_reconnect_preserves_the_source_floor_across_a_new_connection_epoch():
    node, _ = make_node()
    before_disconnect = [node.now_t() for _ in range(20)]
    # A re-enable/reconnect may happen before elapsed relay time catches up with the
    # strict source sequence. Epoch changes do not reset the relay's transport ledger.
    node._anchor_clock(100_005)
    assert node.now_t() == before_disconnect[-1] + 1
    assert node.relay_now_ms() == 100_005


def test_join_frames_are_queued_in_their_strict_creation_order():
    node, _ = make_node()
    join(node)
    frames = []
    while not node._outbound.empty():
        frames.append(node._outbound.get_nowait())
    assert [f["type"] for f in frames] == ["telemetry", "membership", "capabilities", "node_status"]
    assert all(a["t"] < b["t"] for a, b in zip(frames, frames[1:], strict=False))


def test_failsafe_survives_rejoin_and_local_stop_disables_without_an_event_loop():
    node, clock = make_node()
    join(node)
    clock.seconds += 11
    node._apply_watchdog(node._watchdog.poll())
    assert node._failsafe_latched and node.watchdog_state == "failsafe"
    join(node, 2)
    assert not node.device.status().control_authority
    assert node._watchdog.state == WATCHDOG_FAILSAFE
    node.local_stop()
    assert not node.device.status().control_authority


def test_link_loss_stops_now_and_disables_at_hold():
    node, clock = make_node()
    join(node)
    node.device.move_to(1, 0, 0, 0.1)
    node._on_link_lost()
    assert node.device.state == "stopped"
    clock.seconds += 2.1
    node._apply_watchdog(node._watchdog.poll())
    assert node._watchdog.state == WATCHDOG_FAILSAFE
    assert not node.device.status().control_authority


def command(node, operation="goto", seq=1):
    args = {"x_mm": 100, "y_mm": 0, "z_mm": 0, "speed_mm_s": 100} if operation == "goto" else {}
    body = {
        "v": 1,
        "type": "command",
        "t": 100_000,
        "event_id": "event",
        "session": "test",
        "drone_id": 11,
        "intent_id": "intent",
        "command_id": f"command-{seq}",
        "connection_epoch": 1,
        "roster_version": 1,
        "issued_at": 100_000,
        "ttl_ms": 2000,
        "seq": seq,
        "operation": operation,
        "args": args,
    }
    body["signature"] = protocol.sign_event(body, "test-key")
    return body


def test_held_watchdog_refuses_motion_and_shutdown_disables():
    node, _ = make_node()
    join(node)
    node._watchdog.state = WATCHDOG_HOLD
    node._handle_command(command(node))
    frames = []
    while not node._outbound.empty():
        frames.append(node._outbound.get_nowait())
    assert frames[-1]["reason"] == "watchdog_hold"
    node._shutdown_device()
    assert not node.device.status().control_authority


def test_stop_io_failure_still_latches_and_attempts_disable():
    node, _ = make_node()
    join(node)
    disabled = []

    def broken_stop():
        raise OSError("bot shell disconnected")

    def disable():
        disabled.append(True)

    node.device.stop = broken_stop
    node.device.disable = disable
    assert node.local_stop() is False
    assert node._failsafe_latched and disabled == [True]
    node._shutdown_device()
    assert disabled == [True, True]


def test_older_queued_reenable_cannot_clear_a_later_stop():
    node, _ = make_node()
    join(node)
    node._watchdog.state = WATCHDOG_FAILSAFE
    node._failsafe_latched = True
    old_generation = node._local_stop_generation
    node.local_stop()
    node._request_reenable(old_generation)
    node._local_stop_update()
    join(node, 2)
    assert node._failsafe_latched
    assert not node.device.status().control_authority


def test_estop_cannot_be_superseded_by_a_subsequent_goto_before_loop_tick():
    async def run():
        node, _ = make_node()
        join(node)
        node._handle_command(command(node, "estop", 1))
        node._handle_command(command(node, "goto", 2))
        await asyncio.sleep(0)
        assert node._failsafe_latched
        assert not node.device.status().control_authority
        assert node.device.state == "stopped"
        events = []
        while not node._outbound.empty():
            events.append(node._outbound.get_nowait())
        estop = [e for e in events if e.get("command_id") == "command-1"]
        goto = [e for e in events if e.get("command_id") == "command-2"]
        assert [e["status"] for e in estop] == ["accepted", "executing", "completed"]
        assert goto[-1]["reason"] == "watchdog_failsafe"

    asyncio.run(run())
