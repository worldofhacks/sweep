package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.FakeClock
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NavigationPose
import org.worldofhacks.sweep.bridge.core.frames.NavigationRouteAuthorization

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
            navigation = NavigationConfig(
                navigationConfigId = "navigation-a",
                mapId = "map-a",
                geometryId = "geometry-a",
                cameraCalibrationId = "camera-a",
                bodyExtrinsicsId = "body-a",
                poseFreshnessMs = 500,
                authorizationLifetimeMs = 3_000,
                lossLandAfterMs = 300,
                arrivalHorizontalToleranceM = 0.2,
                arrivalVerticalToleranceM = 0.2,
                maxPositionUncertaintyM = 0.1,
            ),
        )
        val controller = FlightController(model, clock, config) { log += it }
        private var link = LinkFacts()

        /** The relay's signed control heartbeat keeps the deadman fed; deadman tests switch it off. */
        var relayAlive = true

        init {
            controller.onStickSent = { _, frame, _ -> frames += frame }
            model.advance(clock.nowMs())
            controller.updateAircraft(model.facts)
        }

        /** Joined with the pilot's Control authority toggle on, as every flight through the relay is. */
        fun join(estop: Boolean = false) {
            link = LinkFacts(joined = true, estop = estop, lastRelayActivityMs = clock.nowMs(), controlAuthorityGranted = true, settings = settings)
            controller.updateLink(link)
        }

        fun leave() {
            link = link.copy(joined = false)
            controller.updateLink(link)
        }

        /** The pilot's Control authority toggle on the Readiness card. */
        fun grantAuthority(granted: Boolean) {
            link = link.copy(controlAuthorityGranted = granted)
            controller.updateLink(link)
        }

        fun controlHeartbeat() {
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
                if (relayAlive) controlHeartbeat()
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

        fun navigation(
            commandId: String = "route-command",
            routeId: String = "route-1",
            poseStatus: NavigationPose.Status = NavigationPose.Status.READY,
            xMm: Long = 0,
            yMm: Long = 0,
            zMm: Long = 1_200,
            expiresAtMs: Long = clock.nowMs() + 2_000,
            freshUntilMs: Long? = clock.nowMs() + 500,
        ) {
            val now = clock.nowMs()
            val authorization = NavigationRouteAuthorization(
                t = now,
                expiresAtMs = expiresAtMs,
                eventId = "route-event",
                session = "session-a",
                droneId = 1,
                connectionEpoch = 1,
                commandId = commandId,
                routeId = routeId,
                seq = 1,
                navigationConfigId = "navigation-a",
                mapId = "map-a",
                geometryId = "geometry-a",
                cameraCalibrationId = "camera-a",
                bodyExtrinsicsId = "body-a",
                startXMm = 0,
                startYMm = 0,
                startZMm = 1_200,
                targetXMm = 0,
                targetYMm = 2_000,
                targetZMm = 1_200,
                maxSpeedMmS = 300,
                horizontalToleranceMm = 200,
                verticalToleranceMm = 200,
                maxPositionUncertaintyMm = 100,
                tubeRadiusMm = 300,
                signature = "0".repeat(64),
            )
            val ready = poseStatus == NavigationPose.Status.READY
            val pose = NavigationPose(
                t = now,
                eventId = "pose-event",
                session = "session-a",
                droneId = 1,
                connectionEpoch = 1,
                commandId = commandId,
                routeId = routeId,
                seq = 2,
                navigationConfigId = "navigation-a",
                mapId = "map-a",
                geometryId = "geometry-a",
                cameraCalibrationId = "camera-a",
                bodyExtrinsicsId = "body-a",
                poseTimeMs = if (ready) now else null,
                fixTimeMs = if (ready) now else null,
                xMm = if (ready) xMm else null,
                yMm = if (ready) yMm else null,
                zMm = if (ready) zMm else null,
                positionUncertaintyMm = if (ready) 50 else null,
                status = poseStatus,
                signature = "0".repeat(64),
            )
            controller.updateNavigation(NavigationEvidence(authorization, pose, freshUntilMs, relayOffsetMs = 0))
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
    fun `signed route streams a bounded velocity and stale evidence holds then lands`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.navigation()
        val route = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        h.tick(2)
        assertEquals("navigating", h.controller.status.phase)
        assertTrue(h.frames.any { !it.isNeutral }, "route should stream a velocity frame")
        assertTrue(h.frames.filter { !it.isNeutral }.all { it.roll in 0.0..0.3 && it.pitch == 0.0 })
        h.tickMs(400)
        assertEquals("navigation_lost", route.terminal?.second, route.events.toString())
        assertEquals("navigation_hold", h.controller.status.phase)
        assertTrue(h.frames.last().isNeutral, "loss holds neutral sticks")
        h.tickMs(300)
        assertEquals("landing", h.controller.status.phase)
        assertEquals("navigation_lost", h.controller.status.landingReason)
        assertFalse(h.model.virtualStickEnabled)
    }

    @Test
    fun `route authorization expiry is enforced on each navigation tick`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.navigation(expiresAtMs = h.clock.nowMs() + 200, freshUntilMs = h.clock.nowMs() + 500)
        val route = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        h.tick(2)
        assertEquals("navigation_lost", route.terminal?.second, route.events.toString())
        assertEquals("navigation_hold", h.controller.status.phase)
        assertTrue(h.frames.last().isNeutral)
    }

    @Test
    fun `signed navigation hold status neutralizes before the bounded loss landing`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.navigation()
        val route = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        h.tick(1)
        h.navigation(poseStatus = NavigationPose.Status.HOLD, freshUntilMs = null)
        h.tick(1)
        assertEquals("navigation_hold", route.terminal?.second, route.events.toString())
        assertEquals("navigation_hold", h.controller.status.phase)
        assertTrue(h.model.virtualStickEnabled)
        assertTrue(h.frames.last().isNeutral)
        h.tickMs(300)
        assertEquals("navigation_lost", h.controller.status.landingReason)
        assertFalse(h.model.virtualStickEnabled)
    }

    @Test
    fun `signed navigation land status releases virtual stick and starts landing`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.navigation()
        val route = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        h.tick(1)
        h.navigation(poseStatus = NavigationPose.Status.LAND)
        h.tick(1)
        assertEquals("navigation_land", route.terminal?.second, route.events.toString())
        assertEquals("landing", h.controller.status.phase)
        assertEquals("navigation_land", h.controller.status.landingReason)
        assertFalse(h.model.virtualStickEnabled)
    }

    @Test
    fun `signed route rejects a different route and pose outside its 3D tube while accepting arrival`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.navigation()
        val unknown = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-other"), "route-command")
        assertEquals("navigation_not_authorized", unknown.terminal?.second, unknown.events.toString())
        h.navigation(xMm = 500)
        val outside = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        assertEquals("navigation_lost", outside.terminal?.second, outside.events.toString())
        h.navigation(yMm = 1_900)
        val arrived = h.run(CommandArgs.Goto(0, 2_000, 1_200, 300, "route-1"), "route-command")
        assertEquals(listOf("executing", "completed"), arrived.statuses, arrived.events.toString())
        assertFalse(h.model.virtualStickEnabled)
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
        // t = 1500: hold threshold reached with no authorized control heartbeat.
        h.tick(1)
        assertEquals("failed", sink.terminal?.first, sink.events.toString())
        assertEquals("watchdog_hold", sink.terminal?.second)
        assertTrue(sink.terminal!!.third!!.contains("[retryable]"))
        assertEquals("watchdog_hold", h.controller.status.phase)
        assertEquals("hold", h.controller.status.watchdog)
        assertTrue(h.model.virtualStickEnabled, "sticks keep flowing during hold")
        h.tick(2)
        assertTrue(h.frames.takeLast(2).all { it.isNeutral }, "neutral sticks during hold")
        // A command is not a control lease: this safety hover completes but cannot recover hold.
        val held = h.run(CommandArgs.Hover)
        h.tick(4)
        assertEquals("completed", held.terminal?.first, held.events.toString())
        assertEquals("hold", h.controller.status.watchdog)
        assertFalse(h.model.virtualStickEnabled)
        h.relayAlive = true
        h.tick(1)
        assertEquals("armed", h.controller.status.watchdog)
        h.relayAlive = false
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
    fun `control heartbeat during hold releases virtual stick and leaves the aircraft under the flight controller`() {
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
    fun `relay motion is refused with authority_lost while the pilot's control authority toggle is off`() {
        val h = Harness()
        h.join()
        h.grantAuthority(false)
        // On the ground: no takeoff action reaches the flight controller.
        val takeoff = h.run(CommandArgs.Takeoff(zMm = 1200))
        assertEquals("authority_lost", takeoff.terminal?.second, takeoff.events.toString())
        assertTrue(takeoff.terminal!!.third!!.contains("Control authority toggle"), takeoff.terminal!!.third!!)
        assertTrue(takeoff.terminal!!.third!!.contains("[terminal]"))
        h.tick(2)
        assertEquals("idle", h.controller.status.phase)
        assertEquals("landed", h.model.flightState)
        // Airborne under the RC operator: goto and rotate_to are refused before any stick frame.
        h.hovering()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        assertEquals("authority_lost", goto.terminal?.second, goto.events.toString())
        val rotate = h.run(CommandArgs.RotateTo(yawMdeg = 90_000, speedMdegS = 30_000))
        assertEquals("authority_lost", rotate.terminal?.second, rotate.events.toString())
        h.tick(2)
        assertTrue(h.frames.isEmpty(), "no stick frame without the pilot's authority")
        assertFalse(h.model.virtualStickEnabled)
        assertEquals(0.0, h.model.yNorth, 1e-9)
        // The bench procedures are the pilot's own: the toggle does not gate them.
        val bench = RecordingSink()
        assertTrue(h.controller.startBench("hold", StickFrame.NEUTRAL, 300, bench))
        h.tickMs(600)
        assertEquals("completed", bench.terminal?.first, bench.events.toString())
        // Hover keeps its airborne fail-safe behaviour: Virtual Stick, neutral sticks, settle.
        val framesBefore = h.frames.size
        val hover = h.run(CommandArgs.Hover)
        h.tick(4)
        assertEquals(listOf("executing", "completed"), hover.statuses, hover.events.toString())
        assertTrue(h.frames.size > framesBefore && h.frames.drop(framesBefore).all { it.isNeutral })
        assertFalse(h.model.virtualStickEnabled)
        // Estop and land keep their handling too.
        val estop = h.run(CommandArgs.Estop)
        h.tick(4)
        assertEquals("completed", estop.terminal?.first, estop.events.toString())
        val land = h.run(CommandArgs.Land)
        h.tickMs(3_500)
        assertEquals("completed", land.terminal?.first, land.events.toString())
        assertEquals("landed", h.model.flightState)
        // Granted again: the next takeoff runs.
        h.grantAuthority(true)
        val allowed = h.run(CommandArgs.Takeoff(zMm = 1200))
        h.tickMs(4_000)
        assertEquals("completed", allowed.terminal?.first, allowed.events.toString())
        assertEquals("hovering", h.model.flightState)
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

    @Test
    fun `a network stop asserted while virtual stick is enabling cuts the motion before its first frame`() {
        val h = Harness()
        h.hovering()
        h.join()
        // The SDK's enable answers two ticks later, as the real one does asynchronously.
        h.model.deferEnableTicks = 2
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        assertEquals("enabling_virtual_stick", h.controller.status.phase)
        h.estop(true)
        // The first tick latches the stop while the loop is still enabling: nothing to cut yet.
        h.tick(1)
        assertTrue(h.controller.status.estopLatched)
        assertEquals("enabling_virtual_stick", h.controller.status.phase)
        assertNull(goto.terminal)
        // The enable answers on the next tick and the loop would start the step; the stop,
        // latched a tick ago, must still cut it before the first frame goes out.
        h.tick(1)
        assertEquals("estop_asserted", goto.terminal?.second, goto.events.toString())
        assertTrue(h.frames.isNotEmpty() && h.frames.all { it.isNeutral }, "frames ${h.frames}")
        h.tick(4)
        assertTrue(h.frames.all { it.isNeutral }, "frames ${h.frames}")
        assertEquals("idle", h.controller.status.phase)
        assertFalse(h.model.virtualStickEnabled)
        assertEquals(0.0, h.model.yNorth, 1e-9)
    }

    @Test
    fun `a motion posted after the stop flag arrives but before the next tick is refused`() {
        val h = Harness()
        h.hovering()
        h.join()
        h.estop(true)
        val refused = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        assertEquals("estop_asserted", refused.terminal?.second, refused.events.toString())
        assertFalse(h.controller.status.estopLatched, "the tick sets the latch; admission reads the live flag")
        h.tick(1)
        assertTrue(h.frames.isEmpty())
        assertFalse(h.model.virtualStickEnabled)
        assertTrue(h.controller.status.estopLatched)
    }

    @Test
    fun `after an RC takeover the deadman never lands underneath the pilot`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.controller.onTakeover("rc_takeover", "left stick 45%")
        assertEquals("authority_lost", goto.terminal?.second)
        h.relayAlive = false
        h.tickMs(2_500)
        assertEquals("failsafe", h.controller.status.watchdog)
        assertFalse(h.model.landing, "no auto-landing underneath the pilot")
        assertEquals("hovering", h.model.flightState)
        assertEquals("idle", h.controller.status.phase)
        assertNull(h.controller.status.landingReason)
        assertEquals("rc_takeover", h.controller.status.authorityLostReason)
        assertTrue(h.log.any { it.contains("the RC has the aircraft (rc_takeover); no landing commanded") }, h.log.joinToString("\n"))
    }

    @Test
    fun `the deadman lands only what the node was flying`() {
        val h = Harness()
        h.hovering() // the RC operator took off by hand; the loop never flew this aircraft
        h.join()
        h.relayAlive = false
        h.tickMs(2_500)
        assertEquals("failsafe", h.controller.status.watchdog)
        assertFalse(h.model.landing, "an idle aircraft under the flight controller stays with the RC operator")
        assertEquals("idle", h.controller.status.phase)
        assertNull(h.controller.status.landingReason)
        assertFalse(h.model.virtualStickEnabled)
        assertTrue(h.log.any { it.contains("the loop was idle with virtual stick off") }, h.log.joinToString("\n"))
        // Silence while the node holds the aircraft under Virtual Stick still lands it.
        h.leave()
        h.relayAlive = true
        h.join()
        assertEquals("armed", h.controller.status.watchdog)
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.relayAlive = false
        h.tickMs(2_500)
        assertEquals("watchdog_hold", goto.terminal?.second, goto.events.toString())
        assertEquals("watchdog_failsafe", h.controller.status.landingReason)
        assertTrue(h.model.landing)
    }

    @Test
    fun `a network stop held after an RC takeover never lands underneath the pilot`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.controller.onTakeover("rc_pause", "pause button pressed")
        assertEquals("authority_lost", goto.terminal?.second)
        h.estop(true)
        h.tickMs(2_500)
        assertTrue(h.controller.status.estopLatched)
        assertFalse(h.model.landing, "no auto-landing underneath the pilot")
        assertEquals("idle", h.controller.status.phase)
        assertNull(h.controller.status.landingReason)
        assertTrue(h.log.any { it.contains("but the RC has the aircraft (rc_pause): no landing commanded") }, h.log.joinToString("\n"))
        // Released, re-armed, and asserted again: the node is authorized and a held stop lands.
        h.estop(false)
        h.tick(1)
        assertFalse(h.controller.status.estopLatched)
        h.controller.rearmAuthority()
        h.estop(true)
        h.tickMs(2_500)
        assertEquals("estop_held", h.controller.status.landingReason)
        assertTrue(h.model.landing)
    }

    @Test
    fun `a link drop releases virtual stick on the aircraft and a stale enable found while idle is disabled`() {
        val h = Harness()
        h.hovering()
        h.join()
        val goto = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        assertTrue(h.model.virtualStickEnabled)
        h.model.connected = false
        h.tick(1)
        assertEquals("authority_lost", goto.terminal?.second)
        assertFalse(h.model.virtualStickEnabled, "the loop disables virtual stick on the aircraft, not only in its own bookkeeping")
        h.model.connected = true
        h.tick(1)
        assertEquals("idle", h.controller.status.phase)
        // The SDK reports Virtual Stick enabled for the node while the loop is idle (as after an
        // app restart mid-command): the loop clears it so the flight controller stops waiting.
        h.model.enableVirtualStick { }
        assertTrue(h.model.virtualStickEnabled)
        h.controller.onVirtualStickState(enabled = true, ownedBySdk = true, owner = "MSDK")
        assertFalse(h.model.virtualStickEnabled)
        assertTrue(h.log.any { it.contains("virtual stick found enabled while the loop is idle (MSDK): disabling") }, h.log.joinToString("\n"))
        assertNull(h.controller.status.authorityLostReason)
        assertEquals("idle", h.controller.status.phase)
        // Reported off while idle: nothing to do and nothing latched.
        h.controller.onVirtualStickState(enabled = false, ownedBySdk = false, owner = "RC")
        assertNull(h.controller.status.authorityLostReason)
    }

    @Test
    fun `every stick event reaches the loop and each takeover after a re-arm cancels again`() {
        val h = Harness()
        h.hovering()
        h.join()
        // The pilot flies by hand: the port forwards every deflection; the loop notes it once.
        repeat(5) { h.controller.onTakeover("rc_takeover", "left stick 60%") }
        assertEquals(1, h.log.count { it.contains("pilot has control") })
        assertNull(h.controller.status.authorityLostReason)
        // Pause during a goto latches.
        val first = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        h.controller.onTakeover("rc_pause", "pause button pressed")
        assertEquals("authority_lost", first.terminal?.second)
        assertEquals("rc_pause", h.controller.status.authorityLostReason)
        // Latched: the pilot's continuing input is not logged again and changes nothing.
        val logged = h.log.size
        repeat(5) { h.controller.onTakeover("rc_takeover", "right stick 50%") }
        assertEquals(logged, h.log.size)
        assertEquals("rc_pause", h.controller.status.authorityLostReason)
        // Re-armed, the next goto runs and a stick past the threshold must latch again.
        h.controller.rearmAuthority()
        val second = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        h.tickMs(300)
        assertTrue(h.model.virtualStickEnabled)
        h.controller.onTakeover("rc_takeover", "right stick 50%")
        assertEquals("authority_lost", second.terminal?.second, second.events.toString())
        assertEquals("rc_takeover", h.controller.status.authorityLostReason)
        assertFalse(h.model.virtualStickEnabled)
        // A stick moved in the window between admission and the first tick is not lost either:
        // the loop sees it in order, while the enable is still pending.
        h.controller.rearmAuthority()
        h.model.deferEnableTicks = 1
        val framesBefore = h.frames.size
        val third = h.run(CommandArgs.Goto(xMm = 0, yMm = 3000, zMm = 1200, speedMmS = 500))
        assertEquals("enabling_virtual_stick", h.controller.status.phase)
        h.controller.onTakeover("rc_takeover", "left stick 40%")
        assertEquals("authority_lost", third.terminal?.second, third.events.toString())
        h.tick(2)
        assertEquals(framesBefore, h.frames.size, "no frame streamed for the cancelled step")
        assertFalse(h.model.virtualStickEnabled, "the late enable answer is released again")
        assertEquals("idle", h.controller.status.phase)
    }

    @Test
    fun `bench holds and virtual stick need the deadman armed and are refused after failsafe`() {
        val h = Harness()
        h.hovering()
        // No relay yet: no thresholds, no deadman, no stick stream.
        val disarmed = RecordingSink()
        assertFalse(h.controller.startBench("axis-pitch", StickFrame.NEUTRAL.copy(pitch = 0.3), 1_000, disarmed))
        assertEquals("watchdog_disarmed", disarmed.terminal?.second, disarmed.events.toString())
        assertTrue(disarmed.terminal!!.third!!.contains("[retryable]"))
        val hover = h.run(CommandArgs.Hover)
        assertEquals("watchdog_disarmed", hover.terminal?.second, hover.events.toString())
        h.tick(2)
        assertTrue(h.frames.isEmpty(), "no frame without the deadman")
        assertFalse(h.model.virtualStickEnabled)
        assertEquals("idle", h.controller.status.phase)
        // Joined: the thresholds are in force and the bench hold runs under the deadman.
        h.join()
        val armed = RecordingSink()
        assertTrue(h.controller.startBench("axis-pitch", StickFrame.NEUTRAL.copy(pitch = 0.3), 500, armed))
        h.tickMs(1_000)
        assertEquals("completed", armed.terminal?.first, armed.events.toString())
        assertFalse(h.model.virtualStickEnabled)
        // Failsafe after relay silence: bench holds are refused exactly like wire commands.
        h.relayAlive = false
        h.tickMs(2_500)
        assertEquals("failsafe", h.controller.status.watchdog)
        val tripped = RecordingSink()
        assertFalse(h.controller.startBench("deadman", StickFrame.NEUTRAL, 1_000, tripped))
        assertEquals("watchdog_failsafe", tripped.terminal?.second, tripped.events.toString())
        assertFalse(h.model.virtualStickEnabled)
    }

    @Test
    fun `a hold that interrupted a node takeoff still lands at failsafe unless the relay comes back`() {
        val h = Harness()
        h.join()
        val takeoff = h.run(CommandArgs.Takeoff(zMm = 1200))
        h.tick(1)
        assertEquals("taking_off", h.controller.status.phase)
        h.relayAlive = false
        h.tickMs(500)
        // Hold: the takeoff command fails and the flight controller finishes the takeoff by itself.
        assertEquals("watchdog_hold", takeoff.terminal?.second, takeoff.events.toString())
        assertEquals("idle", h.controller.status.phase)
        assertFalse(h.model.virtualStickEnabled)
        // An authorized control heartbeat before failsafe re-arms the deadman: nothing is landed.
        h.relayAlive = true
        h.tickMs(3_000)
        assertEquals("armed", h.controller.status.watchdog)
        assertFalse(h.model.landing)
        assertEquals("hovering", h.model.flightState)
        // The same takeoff interrupted again with the relay silent through failsafe: the node
        // lands what it took off, although the hold left the loop idle with virtual stick off.
        h.model.place()
        h.tick(1)
        val again = h.run(CommandArgs.Takeoff(zMm = 1200))
        h.tick(1)
        h.relayAlive = false
        h.tickMs(500)
        assertEquals("watchdog_hold", again.terminal?.second, again.events.toString())
        assertEquals("idle", h.controller.status.phase)
        h.tickMs(1_500)
        assertEquals("failsafe", h.controller.status.watchdog)
        assertEquals("watchdog_failsafe", h.controller.status.landingReason)
        assertTrue(h.model.landing, "auto-landing commanded for the aircraft the node took off")
        assertTrue(h.log.any { it.contains("never return to home") })
        h.tickMs(4_000)
        assertEquals("landed", h.model.flightState)
        assertEquals("idle", h.controller.status.phase)
    }
}
