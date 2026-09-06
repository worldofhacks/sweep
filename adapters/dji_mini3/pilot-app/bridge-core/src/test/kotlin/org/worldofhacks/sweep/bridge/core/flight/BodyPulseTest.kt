package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.FakeClock
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs

class BodyPulseTest {
    private class Sink : ReportSink {
        val events = mutableListOf<String>()
        override fun executing(detail: String?) { events += "executing" }
        override fun completed(detail: String?) { events += "completed" }
        override fun failed(reason: FlightReason, detail: String?) { events += reason.wire }
    }

    private class Harness(config: FlightConfig = FlightConfig()) {
        val clock = FakeClock(1_000)
        val pulseClock = FakeClock(10_000)
        val model = FakeFlightModel()
        val controller = FlightController(model, clock, config, pulseClock)
        val frames = mutableListOf<Pair<Long, StickFrame>>()
        var link = LinkFacts(joined = true, lastRelayActivityMs = clock.nowMs(), controlAuthorityGranted = true, settings = FlightSettings(10, 200, 1_000))
        var heartbeat = true

        init {
            model.place(zUp = 1.2, yawDeg = 90.0, flying = true)
            model.advance(clock.nowMs())
            controller.updateAircraft(model.facts)
            controller.updateLink(link)
            controller.onStickSent = { _, frame, _ -> frames += pulseClock.nowMs() to frame }
        }

        fun tick(count: Int = 1) {
            repeat(count) {
                clock.advance(100)
                pulseClock.advance(100)
                if (heartbeat) {
                    link = link.copy(lastRelayActivityMs = clock.nowMs())
                    controller.updateLink(link)
                }
                controller.updateAircraft(model.facts)
                controller.tick(clock.nowMs())
            }
        }

        fun run(args: CommandArgs = CommandArgs.BodyPulse(250, 500)): Sink = Sink().also {
            controller.execute(FlightCommand("command-${args.operation.wire}", args), it)
        }
    }

    @Test
    fun `forward and backward pulses remain in body frame and neutralize at the duration boundary`() {
        for (speed in listOf(-250L, 250L)) {
            for (duration in listOf(100L, 500L)) {
                val h = Harness()
                val sink = h.run(CommandArgs.BodyPulse(speed, duration))
                h.tick((duration / 100).toInt())
                assertEquals(listOf("executing"), sink.events)
                assertTrue(h.frames.all { it.second.roll == speed / 1000.0 && it.second.pitch == 0.0 && it.second.yaw == 0.0 && it.second.verticalThrottle == 0.0 })
                val started = h.frames.first().first
                h.tick()
                assertEquals(started + duration, h.frames.last().first)
                assertTrue(h.frames.last().second.isNeutral, "the boundary tick must not send another moving frame")
                assertEquals("settling", h.controller.status.phase)
                h.tick(5)
                assertEquals(listOf("executing", "completed"), sink.events)
                assertFalse(h.model.virtualStickEnabled)
                assertNull(h.controller.status.activeCommandId)
                assertEquals(0.0, h.model.yNorth, 1e-8, "yaw90 body-forward moves east, not world north")
                assertTrue(h.model.xEast * speed > 0, "forward/back signs must follow the nose")
            }
        }
    }

    @Test
    fun `duration between ticks neutralizes on the first tick at or after its deadline`() {
        val h = Harness()
        h.run(CommandArgs.BodyPulse(250, 101))
        h.tick()
        val started = h.frames.single().first
        h.tick()
        assertEquals(started + 100, h.frames.last().first)
        assertFalse(h.frames.last().second.isNeutral)
        h.tick()
        assertEquals(started + 200, h.frames.last().first)
        assertTrue(h.frames.last().second.isNeutral)
        assertEquals("settling", h.controller.status.phase)
    }

    @Test
    fun `pulse duration starts after delayed enable at first stick and ignores wall clock rollback`() {
        val h = Harness()
        h.model.deferEnableTicks = 4
        h.run()
        h.tick(3)
        assertTrue(h.frames.isEmpty())
        h.tick()
        val started = h.frames.single().first
        h.clock.advance(-500)
        h.tick(5)
        assertEquals(started + 500, h.frames.last().first)
        assertTrue(h.frames.last().second.isNeutral)
        assertEquals("settling", h.controller.status.phase)
    }

    @Test
    fun `late tick cuts to neutral without replaying missed pulse frames`() {
        val h = Harness()
        h.run()
        h.tick()
        h.pulseClock.advance(1_000)
        h.tick()
        assertEquals(2, h.frames.size)
        assertTrue(h.frames.last().second.isNeutral)
    }

    @Test
    fun `pulse uses measured axis mapping and respects a tighter local speed limit`() {
        val h = Harness()
        h.controller.mapping = AxisMapping(transposed = true)
        h.run()
        h.tick()
        assertEquals(0.25, h.frames.single().second.pitch)
        assertEquals(0.0, h.frames.single().second.roll)
        val tight = Harness(FlightConfig(limits = FlightLimits(maxHorizontalMS = 0.1)))
        assertEquals(listOf("unsupported"), tight.run().events)
        assertTrue(tight.frames.isEmpty())
    }

    @Test
    fun `pulse refuses ungranted authority nonhovering aircraft and overlap`() {
        val ungranted = Harness()
        ungranted.controller.updateLink(ungranted.link.copy(controlAuthorityGranted = false))
        assertEquals(listOf("authority_lost"), ungranted.run().events)
        val landed = Harness()
        landed.model.place()
        landed.controller.updateAircraft(landed.model.facts)
        assertEquals(listOf("not_airborne"), landed.run().events)
        val active = Harness()
        active.run()
        assertEquals(listOf("node_busy"), active.run().events)
    }

    @Test
    fun `hold and estop preempt active and pending enable pulses`() {
        for (stop in listOf(CommandArgs.Hover, CommandArgs.Estop)) {
            for (pending in listOf(false, true)) {
                val h = Harness()
                if (pending) h.model.deferEnableTicks = 3
                val pulse = h.run()
                if (!pending) h.tick()
                val stopSink = h.run(stop)
                val afterStop = h.frames.size
                h.tick(10)
                assertEquals("superseded", pulse.events.last())
                assertFalse(pulse.events.contains("completed"))
                assertTrue(h.frames.drop(afterStop).all { it.second.isNeutral })
                assertEquals("completed", stopSink.events.last())
                assertFalse(h.model.virtualStickEnabled)
            }
        }
    }

    @Test
    fun `RC takeover and watchdog cancel a pulse without resuming it`() {
        val rc = Harness()
        val rcPulse = rc.run()
        rc.tick()
        rc.controller.onTakeover("rc_takeover", "physical stick")
        val sent = rc.frames.size
        rc.tick(10)
        assertEquals("authority_lost", rcPulse.events.last())
        assertEquals(sent, rc.frames.size)
        assertFalse(rc.model.virtualStickEnabled)
        assertEquals(listOf("authority_lost"), rc.run().events)

        val lost = Harness()
        val lostPulse = lost.run()
        lost.tick()
        lost.heartbeat = false
        lost.tick(3)
        assertEquals("watchdog_hold", lostPulse.events.last())
        assertTrue(lost.frames.last().second.isNeutral)
        lost.tick(10)
        assertFalse(lostPulse.events.contains("completed"))
        assertTrue(lost.model.landing || lost.model.facts.onGround)
    }

    @Test
    fun `withdrawing authority cancels active and enabling pulse`() {
        for (pending in listOf(false, true)) {
            val h = Harness()
            if (pending) h.model.deferEnableTicks = 3
            val pulse = h.run()
            if (!pending) h.tick()
            h.link = h.link.copy(controlAuthorityGranted = false)
            h.controller.updateLink(h.link)
            val sent = h.frames.size
            h.tick(10)
            assertEquals("authority_lost", pulse.events.last())
            assertEquals(sent, h.frames.size)
            assertFalse(h.model.virtualStickEnabled)
        }
    }
}
