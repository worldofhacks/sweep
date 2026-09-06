package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.hypot
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs

class MotionPlannerTest {
    private val limits = FlightLimits()
    private val hovering = AircraftFacts(aircraftConnected = true, rcConnected = true, flightState = "hovering", flying = true, onGround = false, zUp = 1.0)

    @Test
    fun `goto from millimetre arguments is a time-boxed body step at the requested speed`() {
        // 1.2 m north, 0.4 m east, level, at 0.5 m/s, heading north: 1.265 m takes 2530 ms.
        val step = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 400, yMm = 1200, zMm = 1000, speedMmS = 500), hovering, limits, 0.05))
        assertEquals(2_530L, step.durationMs)
        assertEquals(1.2 / 2.53, step.body.forwardMS, 1e-9)
        assertEquals(0.4 / 2.53, step.body.rightMS, 1e-9)
        assertEquals(0.0, step.body.upMS, 1e-9)
        assertEquals(Triple(0.4, 1.2, 0.0), step.displacementM)
        assertFalse(step.slowed)
        assertTrue(limits.within(step.body))
        // The velocity times the duration reproduces the displacement.
        assertEquals(1.2649, hypot(step.body.forwardMS, step.body.rightMS) * step.durationMs / 1000.0, 1e-3)
    }

    @Test
    fun `the step is rotated into the body frame at the current heading`() {
        val facingEast = hovering.copy(yawDeg = 90.0)
        val step = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 1000, yMm = 0, zMm = 1000, speedMmS = 500), facingEast, limits, 0.05))
        assertEquals(2_000L, step.durationMs)
        assertEquals(0.5, step.body.forwardMS, 1e-9, "1 m east is straight ahead when heading east")
        assertEquals(0.0, step.body.rightMS, 1e-9)
        val north = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1000, speedMmS = 500), facingEast, limits, 0.05))
        assertEquals(-0.5, north.body.rightMS, 1e-9, "1 m north is to the left when heading east")
    }

    @Test
    fun `speed above the node limit slows the step and the acknowledgement says so`() {
        val step = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 0, yMm = 2000, zMm = 1000, speedMmS = 2000), hovering, limits, 0.05))
        assertTrue(step.slowed)
        assertEquals(4_000L, step.durationMs, "2 m at the 0.5 m/s limit")
        assertEquals(0.5, step.body.forwardMS, 1e-9)
    }

    @Test
    fun `vertical displacement is bounded by the vertical limit and lengthens the step`() {
        val step = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 0, yMm = 300, zMm = 1900, speedMmS = 500), hovering, limits, 0.05))
        assertEquals(3_000L, step.durationMs, "0.9 m up at 0.3 m/s dominates")
        assertEquals(0.3, step.body.upMS, 1e-9)
        assertEquals(0.1, step.body.forwardMS, 1e-9)
        assertTrue(step.slowed)
        val descent = requireNotNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 0, yMm = 0, zMm = 400, speedMmS = 500), hovering, limits, 0.05))
        assertEquals(-0.3, descent.body.upMS, 1e-9)
        assertEquals(2_000L, descent.durationMs)
    }

    @Test
    fun `a target within the minimum displacement needs no step`() {
        assertNull(MotionPlanner.goto(CommandArgs.Goto(xMm = 20, yMm = -20, zMm = 1000, speedMmS = 500), hovering, limits, 0.05))
        assertNull(MotionPlanner.climb(1.1, hovering, limits, 0.2))
        val climb = requireNotNull(MotionPlanner.climb(1.5, hovering, limits, 0.2))
        assertEquals(0.3, climb.body.upMS, 1e-3, "0.5 m up at the vertical limit; millisecond rounding lengthens the step")
        assertEquals(1_667L, climb.durationMs)
    }

    @Test
    fun `rotate_to takes the shortest way at the requested rate with a deadline margin`() {
        val facts = hovering.copy(yawDeg = 350.0)
        val step = MotionPlanner.rotateTo(CommandArgs.RotateTo(yawMdeg = 10_000, speedMdegS = 10_000), facts, limits, marginMs = 2_000)
        assertEquals(10.0, step.targetDeg)
        assertEquals(20.0, step.deltaDeg)
        assertEquals(10.0, step.speedDegS)
        assertEquals(4_000L, step.durationMs, "2 s of travel plus the 2 s margin")
        val fast = MotionPlanner.rotateTo(CommandArgs.RotateTo(yawMdeg = 270_000, speedMdegS = 90_000), hovering, limits, marginMs = 0)
        assertEquals(-90.0, fast.targetDeg, "270 wraps to -90 in DJI's range")
        assertEquals(-90.0, fast.deltaDeg)
        assertEquals(30.0, fast.speedDegS, "yaw rate is clamped to the node limit")
        assertEquals(3_000L, fast.durationMs)
    }
}
