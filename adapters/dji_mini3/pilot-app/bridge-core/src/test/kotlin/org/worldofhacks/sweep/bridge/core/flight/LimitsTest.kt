package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.hypot
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class LimitsTest {
    @Test
    fun `stick cadence clamps the relay rate into DJI's 5 to 25 Hz and derives the period`() {
        assertEquals(10, StickCadence(10).hz)
        assertEquals(100L, StickCadence(10).periodMs)
        assertEquals(5, StickCadence(3).hz)
        assertEquals(200L, StickCadence(3).periodMs)
        assertEquals(25, StickCadence(40).hz)
        assertEquals(40L, StickCadence(40).periodMs)
        assertEquals(50L, StickCadence(20).periodMs)
        assertThrows(IllegalArgumentException::class.java) { FlightSettings(stickHz = 10, holdMs = 500, failsafeMs = 500) }
        assertEquals(25, FlightSettings(stickHz = 30, holdMs = 1, failsafeMs = 2).clampedStickHz)
    }

    @Test
    fun `the next deadline is drift free and resyncs after a long stall`() {
        val cadence = StickCadence(10)
        assertEquals(1_100L, cadence.nextDeadline(previousDeadlineMs = 1_000, nowMs = 1_050))
        assertEquals(1_100L, cadence.nextDeadline(previousDeadlineMs = 1_000, nowMs = 1_150), "one late tick catches up")
        assertEquals(5_100L, cadence.nextDeadline(previousDeadlineMs = 1_000, nowMs = 5_000), "a stall longer than a period resyncs")
    }

    @Test
    fun `rate meter measures the achieved cadence over its window`() {
        val meter = RateMeter(windowMs = 2_000)
        assertEquals(0.0, meter.rateHz(0))
        for (i in 0 until 21) meter.record(i * 100L)
        assertEquals(10.0, meter.rateHz(2_000), 1e-9)
        assertEquals(21, meter.count)
        assertEquals(0.0, meter.rateHz(10_000), "old sends fall out of the window")
    }

    @Test
    fun `limits scale the horizontal vector and clamp the rest`() {
        val limits = FlightLimits()
        val clamped = limits.clamp(BodyVelocity(forwardMS = 3.0, rightMS = 4.0, upMS = 2.0, yawRateDegS = 90.0))
        assertEquals(0.5, hypot(clamped.forwardMS, clamped.rightMS), 1e-9)
        assertEquals(0.3, clamped.forwardMS, 1e-9)
        assertEquals(0.4, clamped.rightMS, 1e-9)
        assertEquals(0.3, clamped.upMS)
        assertEquals(30.0, clamped.yawRateDegS)
        assertTrue(limits.within(clamped))
        assertThrows(IllegalArgumentException::class.java) { FlightLimits(maxHorizontalMS = 30.0) }
    }
}
