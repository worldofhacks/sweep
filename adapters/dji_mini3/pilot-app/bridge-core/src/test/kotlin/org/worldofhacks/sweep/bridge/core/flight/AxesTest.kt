package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * The BODY-frame sign conventions, written down because issue #85 warns of a pitch and roll
 * swap: DJI documents `roll` as the body X (nose) velocity and `pitch` as the body Y
 * (starboard) velocity in velocity mode, so forward goes into `roll` and right into `pitch`.
 */
class AxesTest {
    private val mapping = AxisMapping()

    @Test
    fun `forward goes into roll and right goes into pitch by DJI's documented convention`() {
        val forward = mapping.toFrame(BodyVelocity(forwardMS = 0.3))
        assertEquals(StickFrame(pitch = 0.0, roll = 0.3, yaw = 0.0, verticalThrottle = 0.0, yawMode = YawMode.ANGULAR_VELOCITY), forward)
        val right = mapping.toFrame(BodyVelocity(rightMS = 0.3))
        assertEquals(0.3, right.pitch)
        assertEquals(0.0, right.roll)
        val backLeft = mapping.toFrame(BodyVelocity(forwardMS = -0.2, rightMS = -0.1))
        assertEquals(-0.2, backLeft.roll)
        assertEquals(-0.1, backLeft.pitch)
    }

    @Test
    fun `up is positive vertical throttle and yaw rate is clockwise positive in angular velocity mode`() {
        val frame = mapping.toFrame(BodyVelocity(upMS = 0.25, yawRateDegS = 15.0))
        assertEquals(0.25, frame.verticalThrottle)
        assertEquals(15.0, frame.yaw)
        assertEquals(YawMode.ANGULAR_VELOCITY, frame.yawMode)
        assertFalse(frame.isNeutral)
        assertTrue(StickFrame.NEUTRAL.isNeutral)
    }

    @Test
    fun `a yaw target switches to angle mode with the heading wrapped into DJI's range`() {
        val frame = mapping.toFrame(BodyVelocity(yawTargetDeg = 270.0))
        assertEquals(YawMode.ANGLE, frame.yawMode)
        assertEquals(-90.0, frame.yaw)
        assertEquals(180.0, AxisMapping.normalizeYaw(180.0))
        assertEquals(180.0, AxisMapping.normalizeYaw(-180.0))
        assertEquals(-170.0, AxisMapping.normalizeYaw(190.0))
    }

    @Test
    fun `the transposed mapping swaps the two fields and the inverse round-trips`() {
        val transposed = AxisMapping(transposed = true)
        val frame = transposed.toFrame(BodyVelocity(forwardMS = 0.3, rightMS = -0.1))
        assertEquals(0.3, frame.pitch)
        assertEquals(-0.1, frame.roll)
        for (candidate in listOf(mapping, transposed)) {
            val body = BodyVelocity(forwardMS = 0.3, rightMS = -0.1, upMS = 0.05, yawRateDegS = 5.0)
            assertEquals(body, candidate.toBody(candidate.toFrame(body)))
            val angle = BodyVelocity(forwardMS = 0.1, yawTargetDeg = 45.0)
            assertEquals(angle, candidate.toBody(candidate.toFrame(angle)))
        }
    }

    @Test
    fun `yaw delta takes the shortest way around`() {
        assertEquals(20.0, AxisMapping.yawDelta(350.0, 10.0))
        assertEquals(-20.0, AxisMapping.yawDelta(10.0, 350.0))
        assertEquals(180.0, AxisMapping.yawDelta(0.0, 180.0))
        assertEquals(180.0, AxisMapping.yawDelta(90.0, -90.0))
        assertEquals(0.0, AxisMapping.yawDelta(-30.0, 330.0))
    }

    @Test
    fun `ground to body rotation uses compass yaw - north is forward at zero and east is forward at ninety`() {
        val (f0, r0) = GroundFrame.toBody(east = 0.0, north = 1.0, yawDeg = 0.0)
        assertEquals(1.0, f0, 1e-9)
        assertEquals(0.0, r0, 1e-9)
        val (f0e, r0e) = GroundFrame.toBody(east = 1.0, north = 0.0, yawDeg = 0.0)
        assertEquals(0.0, f0e, 1e-9)
        assertEquals(1.0, r0e, 1e-9)
        val (f90, r90) = GroundFrame.toBody(east = 1.0, north = 0.0, yawDeg = 90.0)
        assertEquals(1.0, f90, 1e-9)
        assertEquals(0.0, r90, 1e-9)
        val (f90n, r90n) = GroundFrame.toBody(east = 0.0, north = 1.0, yawDeg = 90.0)
        assertEquals(0.0, f90n, 1e-9)
        assertEquals(-1.0, r90n, 1e-9, "at heading east, north is to the left")
        val (east, north) = GroundFrame.toGround(forward = 1.0, right = 0.0, yawDeg = 90.0)
        assertEquals(1.0, east, 1e-9)
        assertEquals(0.0, north, 1e-9)
    }
}
