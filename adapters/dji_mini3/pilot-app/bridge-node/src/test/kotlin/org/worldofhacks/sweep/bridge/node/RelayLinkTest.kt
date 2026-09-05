package org.worldofhacks.sweep.bridge.node

import java.util.concurrent.CopyOnWriteArrayList
import kotlin.math.abs
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.AcknowledgementFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NodeSettings
import org.worldofhacks.sweep.bridge.core.frames.NodeStatusFrame
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState
import org.worldofhacks.sweep.bridge.core.frames.TelemetryFrame
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
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

    private fun config(stub: StubRelay, token: String = String(key, Charsets.UTF_8)) = NodeConfig(
        relayUrl = stub.url,
        session = stub.session,
        droneId = 1,
        token = token,
        adapterId = "test-node-1",
        capabilities = listOf("flight", "pano_360", "reconstruct_8"),
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
    fun `relay silence drives the watchdog to hold then failsafe and failsafe refuses commands`() {
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 300, watchdogFailsafeMs = 900)
        StubRelay(key, nodeSettings = settings).use { stub ->
            val aircraft = FakeAircraft(connected = true)
            link(stub, aircraft).use { link ->
                await("ready") { link.state.value.membership == "ready" }
                // The stub sends nothing unprompted; the node's own telemetry is not relay activity.
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
