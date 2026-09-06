package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class FakeFlightModelTest {
    private fun run(model: FakeFlightModel, fromMs: Long, toMs: Long, stepMs: Long = 100, frame: StickFrame? = null): Long {
        var now = fromMs
        while (now < toMs) {
            now += stepMs
            if (frame != null) model.sendStick(frame)
            model.advance(now)
        }
        return now
    }

    @Test
    fun `takeoff climbs to the hover altitude and landing returns to motors off`() {
        val model = FakeFlightModel()
        model.advance(0)
        assertEquals("landed", model.flightState)
        var ok = false
        model.startTakeoff { ok = it == PortResult.Ok }
        assertTrue(ok)
        assertEquals("taking_off", model.flightState)
        val airborne = run(model, 0, 3_000)
        assertEquals(1.2, model.zUp, 1e-9)
        assertEquals("hovering", model.flightState)
        assertTrue(model.facts.flying)
        model.startLanding { ok = it == PortResult.Ok }
        assertTrue(ok)
        assertEquals("landing", model.flightState)
        run(model, airborne, airborne + 4_000)
        assertEquals(0.0, model.zUp)
        assertEquals("landed", model.flightState)
        assertFalse(model.motorsOn)
    }

    @Test
    fun `stick frames move the fixture by DJI's convention and hover when frames stop`() {
        val model = FakeFlightModel()
        model.place(flying = true)
        model.advance(0)
        model.enableVirtualStick { }
        model.setAdvancedMode(true)
        // roll is the body X velocity: heading north, +roll moves north.
        val forward = StickFrame.NEUTRAL.copy(roll = 0.5)
        val t1 = run(model, 0, 2_000, frame = forward)
        assertTrue(model.yNorth > 0.7, "moved north ${model.yNorth}")
        assertEquals(0.0, model.xEast, 1e-6)
        assertEquals("airborne", model.flightState)
        // Frames stop: the fixture decays to hover like the real flight controller.
        run(model, t1, t1 + 2_000)
        assertTrue(model.speedMS < 0.05, "speed ${model.speedMS}")
        assertEquals("hovering", model.flightState)
        // pitch is the body Y velocity: heading north, +pitch moves east.
        val right = StickFrame.NEUTRAL.copy(pitch = 0.5)
        run(model, t1 + 2_000, t1 + 4_000, frame = right)
        assertTrue(model.xEast > 0.7, "moved east ${model.xEast}")
    }

    @Test
    fun `yaw angle mode turns toward the target and yaw rate integrates`() {
        val model = FakeFlightModel()
        model.place(flying = true)
        model.advance(0)
        model.enableVirtualStick { }
        model.setAdvancedMode(true)
        val t1 = run(model, 0, 2_000, frame = StickFrame(0.0, 0.0, 90.0, 0.0, YawMode.ANGLE))
        assertEquals(90.0, model.yawDeg, 1e-9)
        run(model, t1, t1 + 1_000, frame = StickFrame(0.0, 0.0, -30.0, 0.0, YawMode.ANGULAR_VELOCITY))
        assertEquals(60.0, model.yawDeg, 1e-6)
    }

    @Test
    fun `landing drops virtual stick and a disconnected aircraft refuses everything`() {
        val model = FakeFlightModel()
        model.place(flying = true)
        model.advance(0)
        model.enableVirtualStick { }
        model.startLanding { }
        run(model, 0, 5_000)
        assertFalse(model.virtualStickEnabled)
        model.connected = false
        var result: PortResult? = null
        model.enableVirtualStick { result = it }
        assertTrue(result is PortResult.Failed)
        model.startTakeoff { result = it }
        assertTrue(result is PortResult.Failed)
    }
}
