package org.worldofhacks.sweep.bridge.core.watchdog

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.FakeClock

class WatchdogTest {
    private val clock = FakeClock(nowMs = 1_000)
    private val config = WatchdogConfig(holdMs = 500, failsafeMs = 2_000)

    private fun armed(): Watchdog = Watchdog(config, clock).also { it.arm() }

    @Test
    fun `config requires hold below failsafe`() {
        assertThrows(IllegalArgumentException::class.java) { WatchdogConfig(holdMs = 500, failsafeMs = 500) }
        assertThrows(IllegalArgumentException::class.java) { WatchdogConfig(holdMs = -1, failsafeMs = 500) }
    }

    @Test
    fun `starts disarmed and ignores time until armed`() {
        val watchdog = Watchdog(config, clock)
        assertEquals(WatchdogState.DISARMED, watchdog.state)
        clock.advance(10_000)
        assertNull(watchdog.poll())
        assertEquals(WatchdogState.DISARMED, watchdog.state)
    }

    @Test
    fun `armed stays armed while verified heartbeats keep arriving`() {
        val watchdog = armed()
        repeat(10) {
            clock.advance(400)
            watchdog.heartbeat()
            assertNull(watchdog.poll())
        }
        assertEquals(WatchdogState.ARMED, watchdog.state)
    }

    @Test
    fun `hold after hold_ms without activity`() {
        val watchdog = armed()
        clock.advance(499)
        assertNull(watchdog.poll())
        clock.advance(1)
        val transition = watchdog.poll()
        assertEquals(WatchdogTransition(WatchdogState.ARMED, WatchdogState.HOLD, WatchdogReason.WATCHDOG_HOLD, 500), transition)
        assertEquals("watchdog_hold", transition?.reason?.wire)
        assertEquals("hold", watchdog.state.toNodeStatus().wire)
        assertNull(watchdog.poll(), "no repeated transition while still in hold")
    }

    @Test
    fun `failsafe after failsafe_ms and it is terminal until re-armed`() {
        val watchdog = armed()
        clock.advance(500)
        watchdog.poll()
        clock.advance(1_499)
        assertNull(watchdog.poll())
        clock.advance(1)
        val transition = watchdog.poll()
        assertEquals(WatchdogTransition(WatchdogState.HOLD, WatchdogState.FAILSAFE, WatchdogReason.WATCHDOG_FAILSAFE, 2_000), transition)
        assertEquals("watchdog_failsafe", transition?.reason?.wire)
        watchdog.heartbeat()
        assertNull(watchdog.poll())
        assertEquals(WatchdogState.FAILSAFE, watchdog.state)
        watchdog.arm()
        assertEquals(WatchdogState.ARMED, watchdog.state)
        assertNull(watchdog.poll())
    }

    @Test
    fun `a long silence goes straight to failsafe on one poll`() {
        val watchdog = armed()
        clock.advance(5_000)
        val transition = watchdog.poll()
        assertEquals(WatchdogState.FAILSAFE, transition?.to)
        assertEquals(WatchdogReason.WATCHDOG_FAILSAFE, transition?.reason)
    }

    @Test
    fun `activity during hold recovers to armed`() {
        val watchdog = armed()
        clock.advance(700)
        watchdog.poll()
        watchdog.heartbeat()
        val transition = watchdog.poll()
        assertEquals(WatchdogTransition(WatchdogState.HOLD, WatchdogState.ARMED, null, 0), transition)
        clock.advance(499)
        assertNull(watchdog.poll())
    }

    @Test
    fun `disarm stops the clock and clears state`() {
        val watchdog = armed()
        watchdog.disarm()
        clock.advance(10_000)
        assertNull(watchdog.poll())
        assertEquals(WatchdogState.DISARMED, watchdog.state)
    }

    @Test
    fun `wire names match the node_status contract`() {
        assertEquals(listOf("nominal", "hold", "failsafe"), NodeWatchdogState.entries.map { it.wire })
        assertEquals(NodeWatchdogState.NOMINAL, WatchdogState.DISARMED.toNodeStatus())
        assertEquals(NodeWatchdogState.NOMINAL, WatchdogState.ARMED.toNodeStatus())
        assertEquals(NodeWatchdogState.HOLD, WatchdogState.HOLD.toNodeStatus())
        assertEquals(NodeWatchdogState.FAILSAFE, WatchdogState.FAILSAFE.toNodeStatus())
        assertEquals(NodeWatchdogState.HOLD, NodeWatchdogState.fromWire("hold"))
        assertNull(NodeWatchdogState.fromWire("armed"))
    }
}
