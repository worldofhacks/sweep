package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.FakeClock
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs

/**
 * The control loop against the kinematic fixture and a stepped clock: acknowledgement
 * sequences, the stick stream, deadman timing (hold then failsafe), estop sequencing, and
 * the RC takeover transitions. Every safety behaviour the Phase E brief names has a test here.
 */
class FlightControllerTest {
    private class RecordingSink : ReportSink {
        val events = mutableListOf<Triple<String, String?, String?>>()

        override fun executing(detail: String?) {
            events += Triple("executing", null, detail)
        }

        override fun completed(detail: String?) {
            events += Triple("completed", null, detail)
        }

        override fun failed(reason: FlightReason, detail: String?) {
            events += Triple("failed", reason.wire, detail)
        }

        val statuses: List<String>
            get() = events.map { it.first }

        val terminal: Triple<String, String?, String?>?
            get() = events.lastOrNull { it.first == "completed" || it.first == "failed" }
    }

    private class Harness {
        val clock = FakeClock(1_000)
        val model = FakeFlightModel()
        val log = mutableListOf<String>()
        val frames = mutableListOf<StickFrame>()
        val settings = FlightSettings(stickHz = 10, holdMs = 500, failsafeMs = 2_000)
        val config = FlightConfig(
            settleMs = 300,
            progressIntervalMs = 1_000,
            takeoffMinMs = 1_000,
            takeoffTimeoutMs = 10_000,
            landingTimeoutMs = 10_000,
            estopLandAfterMs = 2_000,
            yawSettleMs = 200,
            yawMarginMs = 1_000,
        )
        val controller = FlightController(model, clock, config) { log += it }
        private var link = LinkFacts()

        /** The relay's 10 Hz state fan-out keeps the deadman fed; the deadman tests switch it off. */
        var relayAlive = true

        init {
            controller.onStickSent = { _, frame, _ -> frames += frame }
            model.advance(clock.nowMs())
            controller.updateAircraft(model.facts)
        }

        fun join(estop: Boolean = false) {
            link = LinkFacts(joined = true, estop = estop, lastRelayActivityMs = clock.nowMs(), settings = settings)
            controller.updateLink(link)
        }

        fun leave() {
            link = link.copy(joined = false)
            controller.updateLink(link)
        }

        fun relayActivity() {
            link = link.copy(lastRelayActivityMs = clock.nowMs())
            controller.updateLink(link)
        }

        fun estop(asserted: Boolean) {
            link = link.copy(estop = asserted)
            controller.updateLink(link)
        }

        /** Advances 100 ms per tick; the facts the loop sees lag one tick, as they do on the phone. */
        fun tick(count: Int = 1) {
            repeat(count) {
                clock.advance(100)
                if (relayAlive) relayActivity()
                controller.updateAircraft(model.facts)
                controller.tick(clock.nowMs())
            }
        }

        fun tickMs(ms: Long) = tick((ms / 100).toInt())

        fun run(args: CommandArgs, id: String = "cmd-${args.operation.wire}"): RecordingSink {
            val sink = RecordingSink()
            controller.execute(FlightCommand(id, args), sink)
            return sink
        }

        fun hovering(z: Double = 1.2) {
            model.place(zUp = z, flying = true)
            model.advance(clock.nowMs())
            controller.updateAircraft(model.facts)
        }
    }

    @Test
    fun `hover enables virtual stick streams neutral frames completes and disables`() {
        val h = Harness()
        h.hovering()
        h.join()
        val sink = h.run(CommandArgs.Hover)
        assertTrue(h.controller.status.phase == "settling" || h.controller.status.phase == "enabling_virtual_stick", h.controller.status.phase)
        h.tick(4)
        assertEquals(listOf("executing", "completed"), sink.statuses, sink.events.toString())
        assertTrue(sink.events[0].third!!.contains("neutral sticks"))
        assertTrue(sink.events[1].third!!.contains("hover held"))
        assertTrue(h.frames.size >= 2, "frames ${h.frames.size}")
        assertTrue(h.frames.all { it.isNeutral })
        assertFalse(h.model.virtualStickEnabled, "virtual stick is disabled when idle")
        assertEquals("idle", h.controller.status.phase)
        assertTrue(h.log.any { it.contains("virtual stick enabled") } && h.log.any { it.contains("virtual stick disabled") })
    }

    @Test
    fun `goto streams the planned body velocity with progress and completes after the time box`() {
        val h = Harness()
        h.hovering()
        h.join()
        val sink = h.run(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500))
        h.tickMs(2_600)
        assertEquals("completed", sink.terminal?.first, sink.events.toString())
        val executing = sink.events.filter { it.first == "executing" }
        assertTrue(executing.size >= 2, "progress acknowledgements: ${executing.size}")
        assertTrue(executing.first().third!!.contains("forward 0.50"))
        assertTrue(executing[1].third!!.contains(" of 2000 ms"))
        assertTrue(sink.terminal!!.third!!.contains("north 1.00"))
        val moving = h.frames.filter { !it.isNeutral }
        assertTrue(moving.size in 18..22, "moving frames ${moving.size}")
        assertTrue(moving.all { it.roll == 0.5 && it.pitch == 0.0 && it.yawMode == YawMode.ANGULAR_VELOCITY })
        assertTrue(h.frames.last().isNeutral, "settling sends neutral sticks")
        assertTrue(h.model.yNorth in 0.8..1.05, "moved north ${h.model.yNorth}")
        assertEquals(0.0, h.model.xEast, 1e-6)
        assertFalse(h.model.virtualStickEnabled)
        assertTrue(h.controller.status.stickRateHz > 9.0, "stick rate ${h.controller.status.stickRateHz}")
    }

    @Test
    fun `deadman decays to neutral at hold and lands at failsafe reporting the contract reasons`() {
        val h = Harness()
        h.hovering()
        h.join()
        val sink = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.relayAlive = false
        h.tickMs(400)
        assertEquals("velocity_step", h.controller.status.phase)
        // t = 1500: hold threshold reached with no relay activity since the command.
        h.tick(1)
        assertEquals("failed", sink.terminal?.first, sink.events.toString())
        assertEquals("watchdog_hold", sink.terminal?.second)
        assertTrue(sink.terminal!!.third!!.contains("[retryable]"))
        assertEquals("watchdog_hold", h.controller.status.phase)
        assertEquals("hold", h.controller.status.watchdog)
        assertTrue(h.model.virtualStickEnabled, "sticks keep flowing during hold")
        h.tick(2)
        assertTrue(h.frames.takeLast(2).all { it.isNeutral }, "neutral sticks during hold")
        // A command during hold is admitted (activity) and the sticks stay live.
        val held = h.run(CommandArgs.Hover)
        h.tick(4)
        assertEquals("completed", held.terminal?.first, held.events.toString())
        assertEquals("armed", h.controller.status.watchdog)
        assertFalse(h.model.virtualStickEnabled)
        // Silence again: hold at +500 ms, failsafe at +2000 ms lands.
        val again = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(500)
        assertEquals("watchdog_hold", again.terminal?.second)
        h.tickMs(1_500)
        assertEquals("failsafe", h.controller.status.watchdog)
        assertEquals("landing", h.controller.status.phase)
        assertEquals("watchdog_failsafe", h.controller.status.landingReason)
        assertTrue(h.model.landing, "auto-landing commanded")
        assertFalse(h.model.virtualStickEnabled, "virtual stick released before landing")
        assertTrue(h.log.any { it.contains("never return to home") })
        val refused = h.run(CommandArgs.Hover)
        assertEquals("watchdog_failsafe", refused.terminal?.second)
        h.tickMs(4_000)
        assertEquals("landed", h.model.flightState)
        assertEquals("idle", h.controller.status.phase)
        // A rejoin re-arms the deadman.
        h.leave()
        h.join()
        assertEquals("armed", h.controller.status.watchdog)
    }

    @Test
    fun `a landing in progress is never interrupted by the deadman`() {
        val h = Harness()
        h.hovering()
        h.join()
        val land = h.run(CommandArgs.Land)
        h.relayAlive = false
        h.tickMs(2_200)
        assertEquals("failsafe", h.controller.status.watchdog)
        assertEquals("landing", h.controller.status.phase)
        assertEquals("land_command", h.controller.status.landingReason)
        h.tickMs(1_500)
        assertEquals("completed", land.terminal?.first, land.events.toString())
        assertEquals("landed", h.model.flightState)
    }

    @Test
    fun `relay activity during hold releases virtual stick and leaves the aircraft under the flight controller`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.relayAlive = false
        h.tickMs(500)
        assertEquals("watchdog_hold", h.controller.status.phase)
        h.relayAlive = true
        h.tick(1)
        assertEquals("idle", h.controller.status.phase)
        assertEquals("armed", h.controller.status.watchdog)
        assertFalse(h.model.virtualStickEnabled)
    }

    @Test
    fun `estop is neutral sticks and hover at once then land if the relay keeps the stop asserted`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(500)
        // The relay's authoritative flag cuts the motion before any command arrives.
        h.estop(true)
        h.tick(1)
        assertEquals("estop_asserted", goto.terminal?.second, goto.events.toString())
        assertTrue(h.frames.last().isNeutral)
        assertTrue(h.controller.status.estopLatched)
        // The estop command itself hovers and completes.
        val estop = h.run(CommandArgs.Estop)
        h.tick(4)
        assertEquals(listOf("executing", "completed"), estop.statuses, estop.events.toString())
        assertFalse(h.model.virtualStickEnabled)
        // Motion is refused while the stop is asserted; land stays available.
        val refused = h.run(CommandArgs.Goto(xMm = 0, yMm = 500, zMm = 1200, speedMmS = 500))
        assertEquals("estop_asserted", refused.terminal?.second)
        // Held past the window (2000 ms from the tick that latched it): the node lands on its own
        // (PRD 5.5 hold, then land if held).
        h.tickMs(1_700)
        assertEquals("landing", h.controller.status.phase)
        assertEquals("estop_held", h.controller.status.landingReason)
        assertTrue(h.model.landing)
        h.tickMs(4_000)
        assertEquals("landed", h.model.flightState)
        h.estop(false)
        h.tick(1)
        assertFalse(h.controller.status.estopLatched)
    }

    @Test
    fun `a released stop never lands`() {
        val h = Harness()
        h.hovering()
        h.join(estop = true)
        val estop = h.run(CommandArgs.Estop)
        h.tick(4)
        assertEquals("completed", estop.terminal?.first)
        h.estop(false)
        h.tickMs(3_000)
        assertEquals("idle", h.controller.status.phase)
        assertFalse(h.model.landing)
        assertEquals("hovering", h.model.flightState)
    }

    @Test
    fun `RC takeover cancels with authority_lost and latches until the pilot re-arms`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.controller.onTakeover("rc_takeover", "left stick 45%")
        assertEquals("authority_lost", goto.terminal?.second, goto.events.toString())
        assertTrue(goto.terminal!!.third!!.contains("[terminal]"))
        assertEquals("rc_takeover", h.controller.status.authorityLostReason)
        assertFalse(h.model.virtualStickEnabled)
        assertEquals("idle", h.controller.status.phase)
        h.tick(2)
        val refused = h.run(CommandArgs.Hover)
        assertEquals("authority_lost", refused.terminal?.second)
        h.controller.rearmAuthority()
        assertNull(h.controller.status.authorityLostReason)
        val hover = h.run(CommandArgs.Hover)
        h.tick(4)
        assertEquals("completed", hover.terminal?.first)
    }

    @Test
    fun `stick input while idle is the pilot flying and does not latch`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.controller.onTakeover("rc_takeover", "right stick 60%")
        assertNull(h.controller.status.authorityLostReason)
        assertTrue(h.log.last().contains("pilot has control"))
    }

    @Test
    fun `the flight controller dropping virtual stick counts as a takeover`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.controller.onVirtualStickState(enabled = true, ownedBySdk = true, owner = "MSDK")
        assertNull(goto.terminal)
        h.controller.onVirtualStickState(enabled = true, ownedBySdk = false, owner = "RC")
        assertEquals("authority_lost", goto.terminal?.second)
        assertEquals("virtual_stick_dropped", h.controller.status.authorityLostReason)
    }

    @Test
    fun `losing the aircraft or RC link cancels with authority_lost without latching`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.model.connected = false
        h.tick(1)
        assertEquals("authority_lost", goto.terminal?.second)
        assertTrue(goto.terminal!!.third!!.contains("aircraft_disconnected"))
        assertNull(h.controller.status.authorityLostReason, "no pilot re-arm needed after a link drop")
        assertEquals("idle", h.controller.status.phase)
        val refused = h.run(CommandArgs.Hover)
        assertEquals("aircraft_unavailable", refused.terminal?.second)
        h.model.connected = true
        h.tick(1)
        val hover = h.run(CommandArgs.Hover)
        h.tick(4)
        assertEquals("completed", hover.terminal?.first)
    }

    @Test
    fun `takeoff completes on the reported flight state and climbs to the requested altitude`() {
        val h = Harness()
        h.join()
        val landed = h.run(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500))
        assertEquals("not_airborne", landed.terminal?.second)
        val takeoff = h.run(CommandArgs.Takeoff(zMm = 1500))
        h.tick(1)
        assertEquals("executing", takeoff.statuses.first())
        assertTrue(takeoff.events.first().third!!.contains("auto takeoff started"))
        assertEquals("taking_off", h.controller.status.phase)
        h.tickMs(3_500)
        assertEquals("velocity_step", h.controller.status.phase, h.log.joinToString("\n"))
        assertTrue(h.frames.any { it.verticalThrottle > 0.29 && it.roll == 0.0 && it.pitch == 0.0 })
        h.tickMs(2_500)
        assertEquals("completed", takeoff.terminal?.first, takeoff.events.toString())
        assertTrue(h.model.zUp in 1.4..1.6, "z ${h.model.zUp}")
        assertFalse(h.model.virtualStickEnabled)
        val again = h.run(CommandArgs.Takeoff(zMm = 1500))
        assertEquals("already_airborne", again.terminal?.second)
    }

    @Test
    fun `takeoff at the hover altitude needs no climb and a refused action fails as retryable`() {
        val h = Harness()
        h.join()
        val takeoff = h.run(CommandArgs.Takeoff(zMm = 1200))
        h.tickMs(4_000)
        assertEquals("completed", takeoff.terminal?.first, takeoff.events.toString())
        assertTrue(h.frames.isEmpty(), "no virtual stick needed")
        h.model.place()
        h.model.takeoffResult = PortResult.Failed("motors blocked")
        h.tick(1)
        val refused = h.run(CommandArgs.Takeoff(zMm = 1200))
        assertEquals("takeoff_failed", refused.terminal?.second)
        assertTrue(refused.terminal!!.third!!.contains("[retryable]"))
    }

    @Test
    fun `land completes when the aircraft reports landed and is a no-op on the ground`() {
        val h = Harness()
        h.hovering()
        h.join()
        val land = h.run(CommandArgs.Land)
        h.tick(1)
        assertTrue(land.events.first().third!!.contains("auto-landing started"))
        h.tickMs(3_500)
        assertEquals("completed", land.terminal?.first, land.events.toString())
        assertTrue(land.terminal!!.third!!.contains("landed after"))
        assertEquals("landed", h.model.flightState)
        val again = h.run(CommandArgs.Land)
        assertEquals(listOf("executing", "completed"), again.statuses)
    }

    @Test
    fun `rotate_to uses yaw angle mode and completes within tolerance`() {
        val h = Harness()
        h.hovering()
        h.join()
        val rotate = h.run(CommandArgs.RotateTo(yawMdeg = 90_000, speedMdegS = 30_000))
        h.tickMs(2_500)
        assertEquals("completed", rotate.terminal?.first, rotate.events.toString())
        val yawFrames = h.frames.filter { it.yawMode == YawMode.ANGLE }
        assertTrue(yawFrames.isNotEmpty())
        assertTrue(yawFrames.all { it.yaw == 90.0 && it.roll == 0.0 && it.pitch == 0.0 })
        assertEquals(90.0, h.model.yawDeg, 1e-6)
        val same = h.run(CommandArgs.RotateTo(yawMdeg = 92_000, speedMdegS = 30_000))
        assertEquals(listOf("executing", "completed"), same.statuses, "within tolerance needs no rotation")
    }

    @Test
    fun `hover preempts a running motion and a second motion is refused as busy`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        val busy = h.run(CommandArgs.RotateTo(yawMdeg = 90_000, speedMdegS = 30_000))
        assertEquals("node_busy", busy.terminal?.second)
        assertNull(goto.terminal)
        val hover = h.run(CommandArgs.Hover)
        assertEquals("superseded", goto.terminal?.second)
        h.tick(4)
        assertEquals("completed", hover.terminal?.first)
    }

    @Test
    fun `a refused virtual stick enable fails the command as retryable`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.model.enableResult = PortResult.Failed("not in P mode")
        val hover = h.run(CommandArgs.Hover)
        assertEquals("virtual_stick_unavailable", hover.terminal?.second)
        assertTrue(hover.terminal!!.third!!.contains("[retryable]"))
        assertEquals("idle", h.controller.status.phase)
    }

    @Test
    fun `bench holds stream the raw frame under the same protections and stop on request`() {
        val h = Harness()
        h.hovering()
        h.join()
        val sink = RecordingSink()
        val frame = StickFrame.NEUTRAL.copy(pitch = 0.3)
        assertTrue(h.controller.startBench("axis-pitch", frame, 5_000, sink))
        h.tickMs(1_000)
        assertEquals("bench_axis-pitch", h.controller.status.phase)
        assertTrue(h.frames.takeLast(5).all { it == frame })
        assertTrue(h.model.xEast > 0.2, "the fixture moved east (pitch is the body Y velocity) ${h.model.xEast}")
        h.controller.stopBench()
        assertEquals("completed", sink.terminal?.first)
        assertFalse(h.model.virtualStickEnabled)
        val tooFast = RecordingSink()
        assertFalse(h.controller.startBench("fast", StickFrame.NEUTRAL.copy(roll = 5.0), 1_000, tooFast))
        assertEquals("unsupported", tooFast.terminal?.second)
    }
}
