package org.worldofhacks.sweep.dji

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class BridgeRuntimeStateTest {
    @Test
    fun `feed advisory exposes coverage quality and live readiness`() {
        val monitor = FeedMonitor(staleAfterMs = 500)

        monitor.onSurfaceChanged(true)
        monitor.onCameraChanged(true)
        monitor.onFrame(FeedQuality(width = 1_920, height = 1_080, framesPerSecond = 30), 1_000)

        assertEquals(
            FeedAdvisory(
                coverage = FeedCoverage.PRIMARY_CAMERA,
                quality = FeedQuality(width = 1_920, height = 1_080, framesPerSecond = 30),
                readiness = FeedReadiness.LIVE,
            ),
            monitor.advisory(nowMs = 1_400),
        )
        assertEquals(FeedReadiness.STALE, monitor.advisory(nowMs = 1_501).readiness)
    }

    @Test
    fun `feed readiness reports each missing prerequisite`() {
        val monitor = FeedMonitor(staleAfterMs = 500)

        assertEquals(FeedReadiness.NO_SURFACE, monitor.advisory(0).readiness)
        monitor.onSurfaceChanged(true)
        assertEquals(FeedReadiness.NO_CAMERA, monitor.advisory(0).readiness)
        monitor.onFrame(FeedQuality(640, 480, 30), 0)
        monitor.onCameraChanged(true)
        assertEquals(FeedReadiness.WAITING_FOR_FRAME, monitor.advisory(0).readiness)
        monitor.onCameraChanged(false)
        assertEquals(FeedQuality.UNKNOWN, monitor.advisory(0).quality)
    }

    @Test
    fun `each link loss holds and stops an active dispatch session`() {
        ConnectionSource.entries.forEach { lostSource ->
            val watchdog = BridgeWatchdog()
            ConnectionSource.entries.forEach { watchdog.onConnectionChanged(it, true) }
            assertTrue(watchdog.startDispatch())

            assertEquals(
                WatchdogAction.HoldAndStop(lostSource),
                watchdog.onConnectionChanged(lostSource, false),
            )
            assertFalse(watchdog.canDispatch())
            assertFalse(watchdog.dispatchActive())

            watchdog.onConnectionChanged(lostSource, true)
            assertTrue(watchdog.canDispatch())
            assertFalse(watchdog.dispatchActive())
            assertTrue(watchdog.startDispatch())
        }
    }

    @Test
    fun `dispatch remains refused until product relay and LAN recover`() {
        val watchdog = BridgeWatchdog()

        watchdog.onConnectionChanged(ConnectionSource.PRODUCT, true)
        watchdog.onConnectionChanged(ConnectionSource.RELAY, true)
        assertFalse(watchdog.canDispatch())
        assertFalse(watchdog.startDispatch())

        watchdog.onConnectionChanged(ConnectionSource.LAN, true)
        assertTrue(watchdog.canDispatch())
        assertTrue(watchdog.startDispatch())
    }
}
