package org.worldofhacks.sweep.bridge.node

import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.abs
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.frames.AcknowledgementFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NodeSettings
import org.worldofhacks.sweep.bridge.core.frames.NodeStatusFrame
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState
import org.worldofhacks.sweep.bridge.core.frames.TelemetryFrame
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPins
import org.worldofhacks.sweep.bridge.core.flight.NavigationConfig
import org.worldofhacks.sweep.bridge.core.signing.Signing
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogState

/**
 * The relay link against [StubRelay]: frame sequence, thresholds and clock offset, telemetry,
 * command admission and acknowledgement, reconnect with epoch increment, session_closed
 * halting, aircraft disconnect semantics, and the watchdog under relay silence.
 */
class RelayLinkTest {
    private val key = "adapter-key-0123456789abcdef0123456789abcdef".toByteArray(Charsets.UTF_8)
    private val timing = LinkTiming(
        telemetryHz = 10.0,
        watchdogPollMs = 20,
        initialBackoffMs = 50,
        maxBackoffMs = 200,
        authTimeoutMs = 2_000,
        joinFallbackMs = 500,
    )
    private val phone = PhoneStatusSource { PhoneStatus(batteryPercent = 81, thermalState = PhoneThermalState.NONE) }
    private val logs = CopyOnWriteArrayList<String>()

    private fun config(
        stub: StubRelay,
        token: String = String(key, Charsets.UTF_8),
        localizationPins: LocalizationPins? = null,
        navigation: NavigationConfig? = null,
    ) = NodeConfig(
        relayUrl = stub.url,
        session = stub.session,
        droneId = 1,
        token = token,
        adapterId = "test-node-1",
        capabilities = listOf("flight", "pano_360", "reconstruct_8") + if (navigation == null) emptyList() else listOf("localized_navigation"),
        localizationPins = localizationPins,
        navigation = navigation,
    )

    private fun link(
        stub: StubRelay,
        aircraft: FakeAircraft,
        readiness: ReadinessInput = ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true),
        token: String = String(key, Charsets.UTF_8),
    ): RelayLink = RelayLink(config(stub, token), aircraft, aircraft, phone, timing = timing, log = { logs += it }).also {
        it.setReadiness(readiness)
        it.start()
    }

    private fun await(what: String, timeoutMs: Long = 5_000, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return
            Thread.sleep(10)
        }
        throw AssertionError("timed out waiting for $what; log:\n" + logs.joinToString("\n"))
    }

    private fun JsonObject.str(key: String): String = (this[key] as JsonString).value

    private fun JsonObject.int(key: String): Long = (this[key] as JsonInt).value

    private fun JsonObject.bool(key: String): Boolean = (this[key] as JsonBool).value

    private fun StubRelay.awaitAck(commandId: String, status: String): JsonObject =
        awaitFrame("acknowledgement") { it.str("command_id") == commandId && it.str("status") == status }

    private fun StubRelay.acks(commandId: String): List<String> =
        frames("acknowledgement") { it.str("command_id") == commandId }.map { it.str("status") }

    /**
     * The link's clock, frozen until the test steps it: the watchdog and admission read it,
     * while the stub relay, the telemetry ticker, and the watchdog poll run on real time.
     */
    private class SteppedClock(start: Long) : Clock {
        @Volatile
        private var now = start

        override fun nowMs(): Long = now

        fun advance(ms: Long) {
            now += ms
        }
    }

    @Test
    fun `frame sequence is auth join telemetry readiness capabilities node_status`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("membership ready") { link.state.value.membership == "ready" }
                // node_status is the last of the six startup frames; wait for it so the order
                // assertion does not race the frames still in flight on a slow runner.
                stub.awaitFrame("node_status")
                val order = stub.frames.map { frame ->
                    frame.str("type") + if (frame.str("type") == "membership") "/" + frame.str("action") else ""
                }
                assertEquals(
                    listOf("auth", "membership/join", "telemetry", "membership/readiness", "capabilities", "node_status"),
                    order.take(6),
                    order.toString(),
                )
                val auth = stub.frames[0]
                assertEquals(setOf("v", "type", "source", "drone_id", "token"), auth.keys)
                assertEquals("adapter", auth.str("source"))
                assertEquals(1L, auth.int("drone_id"))
                val join = stub.frames[1]
                assertEquals("test-node-1", join.str("adapter_id"))
                assertTrue(Signing.verify(join.without("signature"), join.str("signature"), key))
                val telemetry = TelemetryFrame.parse(stub.frames[2])
                assertEquals(1, telemetry.connectionEpoch)
                assertEquals("landed", telemetry.state)
                val readiness = stub.frames[3]
                assertEquals(1L, readiness.int("connection_epoch"))
                assertTrue(readiness.bool("home_pose_confirmed") && readiness.bool("control_authority") && readiness.bool("rc_safety_operator_present"))
                assertTrue(Signing.verify(readiness.without("signature"), readiness.str("signature"), key))
                val capabilities = stub.frames[4]
                assertEquals("fake-mini3", capabilities.str("aircraft_model"))
                assertEquals(1L, capabilities.int("connection_epoch"))
                val status = NodeStatusFrame.parse(stub.frames[5])
                assertTrue(status.body.controlAuthority)
                assertEquals("nominal", status.body.watchdogState.wire)
                assertEquals("stopped", status.body.videoPublishState.wire)
                assertEquals(81, status.body.phoneBatteryPercent)

                val state = link.state.value
                assertEquals(RelayConnection.CONNECTED, state.connection)
                assertEquals(1, state.connectionEpoch)
                assertEquals(0, state.rejoins)
                assertEquals(stub.nodeSettings, state.nodeSettings)
                assertEquals(WatchdogState.ARMED, state.watchdog)
                assertTrue(state.controlAuthority)
                assertNull(state.authorityChangeReason)

                stub.awaitFrames("telemetry", 8, timeoutMs = 3_000)
                assertTrue(link.state.value.telemetryRateHz >= 5.0, "rate ${link.state.value.telemetryRateHz}")
                val stamps = stub.frames.drop(1).map { it.int("t") }
                assertTrue(stamps.zipWithNext().all { (earlier, later) -> earlier <= later }, "timestamps regress: $stamps")
                val ids = stub.frames.drop(1).map { it.str("event_id") }
                assertEquals(ids.size, ids.toSet().size, "event ids repeat")
            }
        }
    }

    @Test
    fun `node_status follows the video publisher's state`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            var publishState = VideoPublishState.STOPPED
            val link = RelayLink(config(stub), aircraft, aircraft, phone, timing = timing, log = { logs += it }, videoPublish = { publishState })
            link.use {
                it.start()
                stub.awaitFrame("node_status") { frame -> frame.str("video_publish_state") == "stopped" }
                publishState = VideoPublishState.CONNECTING
                stub.awaitFrame("node_status") { frame -> frame.str("video_publish_state") == "connecting" }
                publishState = VideoPublishState.PUBLISHING
                stub.awaitFrame("node_status") { frame -> frame.str("video_publish_state") == "publishing" }
                publishState = VideoPublishState.FAILED
                val failed = stub.awaitFrame("node_status") { frame -> frame.str("video_publish_state") == "failed" }
                assertEquals(NodeStatusFrame.parse(failed).body.videoPublishState, VideoPublishState.FAILED)
                assertEquals(VideoPublishState.FAILED, it.state.value.nodeStatus?.videoPublishState)
                assertTrue(logs.any { line -> line.contains("video=publishing") })
            }
        }
    }

    @Test
    fun `default timing matches the deployment note - 3s ping and 500ms to 5s backoff`() {
        val defaults = LinkTiming()
        assertEquals(3_000L, defaults.pingIntervalMs)
        assertEquals(500L, defaults.initialBackoffMs)
        assertEquals(5_000L, defaults.maxBackoffMs)
        assertEquals(10.0, defaults.telemetryHz)
    }

    @Test
    fun `auth exchange records a relay round-trip sample`() {
        StubRelay(key).use { stub ->
            link(stub, FakeAircraft(connected = true)).use { link ->
                await("joined") { link.state.value.joined }
                val rtt = checkNotNull(link.state.value.authRoundTripMs)
                assertTrue(rtt in 0..5_000, "auth round trip $rtt ms")
            }
        }
    }

    @Test
    fun `auth accepted delivers the relay thresholds and the clock offset is measured`() {
        val settings = NodeSettings(commandTtlMs = 1500, virtualStickHz = 12, watchdogHoldMs = 700, watchdogFailsafeMs = 3000)
        StubRelay(key, clockOffsetMs = 5_000, nodeSettings = settings).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("joined") { link.state.value.joined }
                val state = link.state.value
                assertEquals(settings, state.nodeSettings)
                val offset = checkNotNull(state.relayOffsetMs)
                assertTrue(abs(offset - 5_000) < 500, "offset $offset should be about 5000 ms")
                // Outgoing frames are stamped on the relay clock, not the phone clock.
                val telemetry = stub.awaitFrame("telemetry")
                assertTrue(abs(telemetry.int("t") - (System.currentTimeMillis() + 5_000)) < 3_000)
                // A command issued on the relay clock is fresh even though the phone clock trails it.
                val command = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(command.commandId, "completed")
            }
        }
    }

    @Test
    fun `admitted commands acknowledge accepted executing completed and move the fixture`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                val takeoff = stub.issueCommand(CommandArgs.Takeoff(zMm = 1200))
                val acks = stub.awaitFrames("acknowledgement", 3) { it.str("command_id") == takeoff.commandId }
                assertEquals(listOf("accepted", "executing", "completed"), acks.map { it.str("status") })
                for (ack in acks) {
                    val parsed = AcknowledgementFrame.parse(ack)
                    assertEquals(takeoff.intentId, parsed.intentId)
                    assertEquals(1, parsed.connectionEpoch)
                    assertEquals(takeoff.rosterVersion, parsed.rosterVersion)
                    assertEquals(JsonNull, ack["reason"])
                }
                assertEquals(1.2, aircraft.snapshot.value.z)
                assertEquals(FlightStates.HOVERING, aircraft.snapshot.value.state)
                await("record completed") { link.state.value.commands.firstOrNull()?.outcome == "completed" }

                val before = stub.frames("capabilities").size
                stub.issueCommand(CommandArgs.CameraCapabilities)
                stub.awaitFrames("capabilities", before + 1)

                val photo = stub.issueCommand(CommandArgs.CapturePhoto("cap-1"))
                val failed = stub.awaitAck(photo.commandId, "failed")
                assertEquals("unsupported", failed.str("reason"))
            }
        }
    }

    @Test
    fun `stale and out-of-order commands fail with contract reasons and forged ones are dropped`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                val first = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(first.commandId, "completed")

                val duplicate = stub.issueCommand(CommandArgs.Hover, seq = first.seq)
                assertEquals("out_of_order_command", stub.awaitAck(duplicate.commandId, "failed").str("reason"))

                val expired = stub.issueCommand(CommandArgs.Hover, issuedAt = stub.relayNow() - 10_000)
                val expiredAck = stub.awaitAck(expired.commandId, "failed")
                assertEquals("stale_command", expiredAck.str("reason"))
                assertTrue(expiredAck.str("detail").contains("ttl"))

                val staleRoster = stub.issueCommand(CommandArgs.Hover, rosterVersion = stub.rosterVersion.get() + 100)
                assertEquals("stale_command", stub.awaitAck(staleRoster.commandId, "failed").str("reason"))

                val staleEpoch = stub.issueCommand(CommandArgs.Hover, connectionEpoch = 99)
                assertEquals("stale_command", stub.awaitAck(staleEpoch.commandId, "failed").str("reason"))

                val forged = stub.issueCommand(CommandArgs.Hover, signingKey = "impostor".toByteArray())
                await("forged command recorded as dropped") {
                    link.state.value.commands.any { it.commandId == forged.commandId && it.outcome == "dropped" }
                }
                Thread.sleep(200)
                assertTrue(stub.frames("acknowledgement") { it.str("command_id") == forged.commandId }.isEmpty(), "forged frames are never acknowledged")

                // Rejections did not consume the sequence: the next in-order command is admitted.
                val next = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(next.commandId, "completed")
                assertEquals(
                    listOf("completed", "failed", "failed", "failed", "failed", "dropped", "completed"),
                    link.state.value.commands.reversed().map { it.outcome },
                    "oldest first: the first hover, four contract refusals, the dropped forgery, the next hover",
                )
            }
        }
    }

    @Test
    fun `state frames update the roster version commands are admitted against`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                stub.sendState(rosterVersion = 42)
                await("roster 42") { link.state.value.rosterVersion == 42 }
                val stale = stub.issueCommand(CommandArgs.Hover, rosterVersion = 41)
                assertEquals("stale_command", stub.awaitAck(stale.commandId, "failed").str("reason"))
                val fresh = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(fresh.commandId, "completed")
            }
        }
    }

    @Test
    fun `a relay-side drop reconnects with backoff and rejoins with the next epoch`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                val early = stub.issueCommand(CommandArgs.Hover, seq = 5)
                stub.awaitAck(early.commandId, "completed")

                stub.dropConnections()
                await("disconnected") { link.state.value.connection == RelayConnection.DISCONNECTED }
                await("rejoined with epoch 2") { link.state.value.joined && link.state.value.connectionEpoch == 2 }
                val state = link.state.value
                assertEquals(1, state.rejoins)
                assertEquals(2, state.attempts)
                assertEquals(2, stub.connections.get())
                assertEquals(WatchdogState.ARMED, state.watchdog)
                assertTrue(logs.any { it.contains("reconnecting in") }, "backoff was logged")

                // The epoch-2 readiness is sent just after the rejoin; wait for both before asserting.
                stub.awaitFrames("membership", 2) { it.str("action") == "readiness" }
                val readinessEpochs = stub.frames("membership") { it.str("action") == "readiness" }.map { it.int("connection_epoch") }.distinct()
                assertEquals(listOf(1L, 2L), readinessEpochs)

                // The relay assigns the epoch; the admitted sequence restarts with it.
                val restarted = stub.issueCommand(CommandArgs.Hover, seq = 1)
                val acks = stub.awaitFrames("acknowledgement", 3) { it.str("command_id") == restarted.commandId }
                assertEquals(listOf("accepted", "executing", "completed"), acks.map { it.str("status") })
                assertEquals(2L, acks.first().int("connection_epoch"))
            }
        }
    }

    @Test
    fun `an explicit reconnect resolves the current network client instead of reusing the first one`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            val first = RelayClients.build(timing)
            val replacement = RelayClients.build(timing)
            val providerCalls = AtomicInteger()
            try {
                RelayLink(
                    config(stub),
                    aircraft,
                    aircraft,
                    phone,
                    timing = timing,
                    log = { logs += it },
                    clientProvider = {
                        if (providerCalls.incrementAndGet() == 1) first else replacement
                    },
                ).use { link ->
                    link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                    link.start()
                    await("first join") { link.state.value.connectionEpoch == 1 }
                    link.reconnectNow()
                    await("join through replacement client") { link.state.value.connectionEpoch == 2 }
                    assertTrue(providerCalls.get() >= 2)
                }
            } finally {
                for (client in listOf(first, replacement)) {
                    client.dispatcher.cancelAll()
                    client.dispatcher.executorService.shutdown()
                    client.connectionPool.evictAll()
                }
            }
        }
    }

    @Test
    fun `session_closed and bad credentials halt automatic reconnects until asked again`() {
        val detail = "persisted sessions are replay-only after a relay process restart; use a new session ID"
        StubRelay(key, refuseAuth = "session_closed" to detail).use { stub ->
            link(stub, FakeAircraft(connected = true)).use { link ->
                await("halted") { link.state.value.halted }
                assertEquals("session_closed", link.state.value.lastAuthRefusal?.reason)
                Thread.sleep(400)
                assertEquals(1, stub.connections.get(), "no automatic reconnect after session_closed")
                assertNull(link.state.value.nextAttemptAtMs)
                link.reconnectNow()
                await("manual retry") { stub.connections.get() == 2 }
            }
        }
        StubRelay(key).use { stub ->
            link(stub, FakeAircraft(connected = true), token = "not-the-node-key").use { link ->
                await("halted") { link.state.value.halted }
                assertEquals("authentication_failed", link.state.value.lastAuthRefusal?.reason)
            }
        }
    }

    @Test
    fun `aircraft disconnect clears control authority while the socket stays up`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                aircraft.setConnected(false)
                val lost = stub.awaitFrame("membership") { it.str("action") == "readiness" && !it.bool("control_authority") }
                assertEquals(1L, lost.int("connection_epoch"))
                assertTrue(lost.bool("home_pose_confirmed") && lost.bool("rc_safety_operator_present"))
                val status = stub.awaitFrame("node_status") { !it.bool("control_authority") }
                assertEquals("aircraft_disconnected", status.str("authority_change_reason"))
                await("degraded") { link.state.value.membership == "degraded" }
                assertEquals(RelayConnection.CONNECTED, link.state.value.connection)
                assertEquals(1, stub.connections.get())
                val telemetryBefore = stub.frames("telemetry").size
                Thread.sleep(300)
                assertEquals(telemetryBefore, stub.frames("telemetry").size, "no telemetry is invented without an aircraft")

                aircraft.setConnected(true)
                stub.awaitFrames("membership", 2) { it.str("action") == "readiness" && it.bool("control_authority") }
                await("ready again") { link.state.value.membership == "ready" }
                assertEquals(1, link.state.value.connectionEpoch, "recovery does not change the epoch")
                assertNull(link.state.value.authorityChangeReason)
                stub.awaitFrames("telemetry", telemetryBefore + 3)
            }
        }
    }

    @Test
    fun `pilot toggles resend readiness with the authority reason`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = false, rcSafetyOperatorPresent = true))
                stub.awaitFrame("membership") { it.str("action") == "readiness" && !it.bool("control_authority") }
                stub.awaitFrame("node_status") { it["authority_change_reason"] == JsonString("not_granted") }
                await("degraded") { link.state.value.membership == "degraded" }
                assertTrue(link.state.value.readinessReasons.contains("control_authority_missing"))
            }
        }
    }

    @Test
    fun `a relay that only echoes the node's telemetry does not feed the deadman`() {
        val clock = SteppedClock(1_000_000)
        StubRelay(key, echoTelemetry = true, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            RelayLink(config(stub), aircraft, aircraft, phone, clock = clock, timing = timing, log = { logs += it }).use { link ->
                link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                link.start()
                await("ready") { link.state.value.membership == "ready" }
                assertNull(link.state.value.lastRelayActivityMs)
                assertEquals(WatchdogState.ARMED, link.state.value.watchdog)
                val framesAtReady = link.state.value.framesIn
                await("the node's telemetry coming back") { stub.echoed.get() >= 5 && link.state.value.framesIn >= framesAtReady + 5 }

                val hold = stub.nodeSettings.watchdogHoldMs
                val failsafe = stub.nodeSettings.watchdogFailsafeMs
                // Step the clock in slices shorter than the hold window, with real time between
                // slices so echoes keep landing between steps: a deadman refreshed by any frame
                // would never leave ARMED, one fed by relay-authored frames only must.
                var advanced = 0L
                while (advanced + 500 < hold) {
                    clock.advance(500)
                    advanced += 500
                    Thread.sleep(120)
                    assertEquals(WatchdogState.ARMED, link.state.value.watchdog, "still armed $advanced ms in")
                }
                clock.advance(hold - advanced)
                advanced = hold
                stub.awaitFrame("node_status", timeoutMs = 2_000) { it.str("watchdog_state") == "hold" }
                assertEquals(WatchdogState.HOLD, link.state.value.watchdog)
                assertTrue(logs.any { it.contains("ARMED -> HOLD after $hold ms without an authorized control heartbeat") }, logs.joinToString("\n"))
                while (advanced + 500 < failsafe) {
                    clock.advance(500)
                    advanced += 500
                    Thread.sleep(100)
                    assertEquals(WatchdogState.HOLD, link.state.value.watchdog, "still in hold $advanced ms in")
                }
                clock.advance(failsafe - advanced)
                stub.awaitFrame("node_status", timeoutMs = 2_000) { it.str("watchdog_state") == "failsafe" }
                assertEquals(WatchdogState.FAILSAFE, link.state.value.watchdog)
                assertTrue(logs.any { it.contains("HOLD -> FAILSAFE after $failsafe ms without an authorized control heartbeat") }, logs.joinToString("\n"))

                // Echoes flowed the whole time and were seen as frames, never as a control heartbeat.
                val state = link.state.value
                assertTrue(stub.echoed.get() >= 10 && state.framesIn >= framesAtReady + 10, "echoed ${stub.echoed.get()}, frames in ${state.framesIn - framesAtReady}")
                await("an echo stamped the current last-frame time") {
                    (link.state.value.lastRelayFrameAtMs ?: Long.MIN_VALUE) >= clock.nowMs()
                }
                assertNull(state.lastRelayActivityMs, "no echo counted as an authorized control heartbeat")
                assertEquals(RelayConnection.CONNECTED, state.connection, "the socket itself never dropped")
            }
        }
    }

    @Test
    fun `only a fresh current signed increasing control heartbeat refreshes the deadman`() {
        val clock = SteppedClock(2_000_000)
        StubRelay(key, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            RelayLink(config(stub), aircraft, aircraft, phone, clock = clock, timing = timing, log = { logs += it }).use { link ->
                link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                link.start()
                await("ready") { link.state.value.membership == "ready" }
                assertNull(link.state.value.lastRelayActivityMs)

                val first = stub.sendControlHeartbeat(seq = 1)
                await("first heartbeat") { link.state.value.lastRelayActivityMs == clock.nowMs() }
                assertTrue(Signing.verify(first.without("signature"), first.str("signature"), key))

                clock.advance(100)
                val acceptedAt = link.state.value.lastRelayActivityMs
                stub.sendState()
                val command = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(command.commandId, "completed")
                Thread.sleep(100)
                assertEquals(acceptedAt, link.state.value.lastRelayActivityMs, "state and a signed command are not control leases")

                stub.sendControlHeartbeat(seq = 2, rosterVersion = stub.rosterVersion.get() + 1)
                stub.sendControlHeartbeat(seq = 2, signingKey = "forged".toByteArray())
                stub.sendControlHeartbeat(seq = 1)
                Thread.sleep(100)
                assertEquals(acceptedAt, link.state.value.lastRelayActivityMs, "wrong identity, forgery, and replay are ignored")

                stub.sendControlHeartbeat(seq = 2)
                await("next valid heartbeat") { link.state.value.lastRelayActivityMs == clock.nowMs() }
                val nextAcceptedAt = link.state.value.lastRelayActivityMs

                clock.advance(100)
                stub.sendControlHeartbeat(
                    seq = 3,
                    timestamp = stub.relayNow() - stub.nodeSettings.watchdogHoldMs - 1_000,
                )
                Thread.sleep(100)
                assertEquals(nextAcceptedAt, link.state.value.lastRelayActivityMs, "an already-expired lease is ignored")
            }
        }
    }

    @Test
    fun `signed pinned control pose is a short lived diagnostic and never a control lease`() {
        StubRelay(key, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            val pins = LocalizationPins("map-a", "geometry-a", "camera-a", "body-a")
            RelayLink(
                config(stub, localizationPins = pins),
                aircraft,
                aircraft,
                phone,
                timing = timing,
                log = { logs += it },
            ).use { link ->
                link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                link.start()
                await("ready") { link.state.value.membership == "ready" }
                assertNull(link.state.value.lastRelayActivityMs)

                val ready = stub.sendControlPose(xMm = 125, yMm = -250)
                val readyTime = ready.int("fix_time_ms")
                await("ready diagnostic pose") { link.state.value.controlPose?.xMm == 125L }
                assertFalse(checkNotNull(link.state.value.controlPose).flightApproved)
                assertNull(link.state.value.lastRelayActivityMs, "diagnostics cannot feed the flight deadman")
                await("ready diagnostic expiry", timeoutMs = 2_000) { link.state.value.controlPose == null }

                val landTime = stub.relayNow()
                stub.sendControlPose(
                    timestamp = landTime,
                    poseTimeMs = landTime,
                    fixTimeMs = readyTime,
                    status = "land",
                )
                await("current land diagnostic with an old real fix") {
                    link.state.value.controlPose?.status == org.worldofhacks.sweep.bridge.core.frames.ControlPose.Status.LAND
                }
                assertNull(link.state.value.lastRelayActivityMs, "LAND remains diagnostic-only")
            }
        }
    }

    @Test
    fun `signed route authorization and pose enter the separate navigation state`() {
        StubRelay(key, emitControlHeartbeats = false).use { stub ->
            val navigation = NavigationConfig(true, "navigation-a", "map-a", "geometry-a", "camera-a", "body-a")
            RelayLink(config(stub, navigation = navigation), FakeAircraft(connected = true), FakeAircraft(connected = true), phone, timing = timing, log = { logs += it }).use { link ->
                link.start()
                await("joined") { link.state.value.joined }
                stub.sendNavigationAuthorization()
                await("route authorization") { link.state.value.navigationAuthorization?.routeId == "route-1" }
                stub.sendNavigationPose()
                await("navigation pose") { link.state.value.navigationPose?.status?.name == "READY" }
                assertNull(link.state.value.controlPose, "diagnostic control_pose remains separate")
            }
        }
    }

    @Test
    fun `equal evidence time permits conservative status changes but not unsafe recovery`() {
        StubRelay(key, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            val pins = LocalizationPins("map-a", "geometry-a", "camera-a", "body-a")
            RelayLink(
                config(stub, localizationPins = pins),
                aircraft,
                aircraft,
                phone,
                timing = timing,
                log = { logs += it },
            ).use { link ->
                link.start()
                await("joined") { link.state.value.joined }
                val evidenceTime = stub.relayNow()
                stub.sendControlPose(timestamp = evidenceTime, poseTimeMs = evidenceTime, fixTimeMs = evidenceTime)
                await("ready diagnostic") {
                    link.state.value.controlPose?.status == org.worldofhacks.sweep.bridge.core.frames.ControlPose.Status.READY
                }

                stub.sendControlPose(
                    timestamp = evidenceTime,
                    poseTimeMs = evidenceTime,
                    fixTimeMs = evidenceTime,
                    status = "hold",
                )
                await("equal-time conservative transition") {
                    link.state.value.controlPose?.status == org.worldofhacks.sweep.bridge.core.frames.ControlPose.Status.HOLD
                }

                stub.sendControlPose(
                    timestamp = evidenceTime,
                    poseTimeMs = evidenceTime,
                    fixTimeMs = evidenceTime,
                    status = "land",
                )
                await("second equal-time conservative transition") {
                    link.state.value.controlPose?.status == org.worldofhacks.sweep.bridge.core.frames.ControlPose.Status.LAND
                }

                stub.sendControlPose(
                    timestamp = evidenceTime,
                    poseTimeMs = evidenceTime,
                    fixTimeMs = evidenceTime,
                    status = "hold",
                )
                Thread.sleep(100)
                assertEquals(
                    org.worldofhacks.sweep.bridge.core.frames.ControlPose.Status.LAND,
                    link.state.value.controlPose?.status,
                    "the same evidence cannot recover from a conservative status",
                )
                assertTrue(logs.any { it.contains("same-evidence less-conservative control_pose") })
            }
        }
    }

    @Test
    fun `control pose rejects approval stale events wrong pins forgery and out of bounds values`() {
        StubRelay(key, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            val pins = LocalizationPins("map-a", "geometry-a", "camera-a", "body-a")
            RelayLink(
                config(stub, localizationPins = pins),
                aircraft,
                aircraft,
                phone,
                timing = timing,
                log = { logs += it },
            ).use { link ->
                link.start()
                await("joined") { link.state.value.joined }

                stub.sendControlPose(flightApproved = true)
                stub.sendControlPose(timestamp = stub.relayNow() - 2_000)
                val readyCutoff = stub.relayNow()
                stub.sendControlPose(
                    timestamp = readyCutoff,
                    poseTimeMs = readyCutoff - 500,
                    fixTimeMs = readyCutoff - 500,
                )
                stub.sendControlPose(mapId = "wrong-map")
                stub.sendControlPose(connectionEpoch = stub.epoch.get() + 1)
                stub.sendControlPose(signingKey = "forged".toByteArray())
                stub.sendControlPose(xMm = 1_000_001)
                await("all diagnostic rejections") { logs.count { it.contains("dropping") && it.contains("control_pose") } >= 7 }
                assertNull(link.state.value.controlPose)
                assertNull(link.state.value.lastRelayActivityMs)
            }
        }
    }

    @Test
    fun `motion is refused with authority_lost while the pilot's control authority toggle is off`() {
        StubRelay(key).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            val off = ReadinessInput(homePoseConfirmed = true, controlAuthority = false, rcSafetyOperatorPresent = true)
            link(stub, aircraft, readiness = off).use { link ->
                await("degraded") { link.state.value.membership == "degraded" }
                assertTrue(link.state.value.readinessReasons.contains("control_authority_missing"))
                // The relay would refuse these upstream; the node does not count on that.
                for (args in listOf(CommandArgs.Takeoff(zMm = 1200), CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500), CommandArgs.RotateTo(yawMdeg = 90_000, speedMdegS = 30_000))) {
                    val command = stub.issueCommand(args)
                    val refused = stub.awaitAck(command.commandId, "failed")
                    assertEquals("authority_lost", refused.str("reason"), args.operation.wire)
                    assertTrue(refused.str("detail").contains("Control authority toggle"), refused.str("detail"))
                    assertEquals(listOf("failed"), stub.acks(command.commandId), "never accepted")
                }
                assertEquals(FlightStates.LANDED, aircraft.snapshot.value.state)
                assertEquals(0.0, aircraft.snapshot.value.z)
                // hover, land, and estop keep their handling.
                for (args in listOf(CommandArgs.Hover, CommandArgs.Land, CommandArgs.Estop)) {
                    val command = stub.issueCommand(args)
                    stub.awaitAck(command.commandId, "completed")
                    assertEquals(listOf("accepted", "executing", "completed"), stub.acks(command.commandId), args.operation.wire)
                }
                // The stub's dedicated signed heartbeats, not these commands, kept the deadman fed.
                assertEquals(WatchdogState.ARMED, link.state.value.watchdog)
                assertFalse(logs.any { it.contains("-> HOLD") }, logs.joinToString("\n"))

                link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                await("ready") { link.state.value.membership == "ready" }
                val takeoff = stub.issueCommand(CommandArgs.Takeoff(zMm = 1200))
                stub.awaitAck(takeoff.commandId, "completed")
                assertEquals(1.2, aircraft.snapshot.value.z)
                assertEquals(
                    listOf("failed", "failed", "failed", "completed", "completed", "completed", "completed"),
                    link.state.value.commands.reversed().map { it.outcome },
                    "oldest first: three refusals, hover, land, estop, then the takeoff",
                )
            }
        }
    }

    @Test
    fun `relay silence drives the watchdog to hold then failsafe and failsafe refuses commands`() {
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 300, watchdogFailsafeMs = 900)
        StubRelay(key, nodeSettings = settings, emitControlHeartbeats = false).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                // The stub sends no control heartbeat; the node's own telemetry is not one.
                stub.awaitFrame("node_status", timeoutMs = 3_000) { it.str("watchdog_state") == "hold" }
                stub.awaitFrame("node_status", timeoutMs = 3_000) { it.str("watchdog_state") == "failsafe" }
                assertEquals(WatchdogState.FAILSAFE, link.state.value.watchdog)
                assertTrue(logs.any { it.contains("land indoors, never return to home") })
                val command = stub.issueCommand(CommandArgs.Hover)
                assertEquals("watchdog_failsafe", stub.awaitAck(command.commandId, "failed").str("reason"))
            }
        }
    }
}
