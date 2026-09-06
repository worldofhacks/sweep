package org.worldofhacks.sweep.bridge.flight

import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.flight.FlightConfig
import org.worldofhacks.sweep.bridge.core.flight.FlightReason
import org.worldofhacks.sweep.bridge.core.flight.ReportSink
import org.worldofhacks.sweep.bridge.core.flight.StickFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NodeSettings
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPins
import org.worldofhacks.sweep.bridge.node.FlightStates
import org.worldofhacks.sweep.bridge.node.LinkState
import org.worldofhacks.sweep.bridge.node.LinkTiming
import org.worldofhacks.sweep.bridge.node.NodeConfig
import org.worldofhacks.sweep.bridge.node.PhoneStatus
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource
import org.worldofhacks.sweep.bridge.node.ReadinessInput
import org.worldofhacks.sweep.bridge.node.RelayLink
import org.worldofhacks.sweep.bridge.node.StubRelay

/**
 * The Phase E loop behind the relay link: acknowledgement sequences on the wire, the stick
 * cadence, deadman hold and failsafe under relay silence, and the RC takeover as the relay
 * sees it (readiness `control_authority=false`, `node_status.authority_change_reason`).
 */
class FlightExecutorTest {
    private val key = "adapter-key-0123456789abcdef0123456789abcdef".toByteArray(Charsets.UTF_8)
    private val timing = LinkTiming(telemetryHz = 10.0, watchdogPollMs = 20, initialBackoffMs = 50, maxBackoffMs = 200, authTimeoutMs = 2_000, joinFallbackMs = 500)
    private val phone = PhoneStatusSource { PhoneStatus(batteryPercent = 81, thermalState = PhoneThermalState.NONE) }
    private val logs = CopyOnWriteArrayList<String>()
    private val config = FlightConfig(settleMs = 300, progressIntervalMs = 300, takeoffMinMs = 500, yawSettleMs = 200)

    private class Node(val aircraft: FakeFlightAircraft, val executor: FlightExecutor, val link: RelayLink) : AutoCloseable {
        override fun close() {
            link.close()
            executor.close()
        }
    }

    /** The stub emits the real relay's signed control heartbeat unless a silence test disables it. */
    private fun node(stub: StubRelay, flying: Boolean, localizationPins: LocalizationPins? = null): Node {
        val aircraft = FakeFlightAircraft()
        aircraft.setConnected(true)
        if (flying) aircraft.place(zUp = 1.2, flying = true)
        val executor = FlightExecutor(aircraft, aircraft, aircraft.fake, config = config, log = { logs += it })
        val nodeConfig = NodeConfig(
            stub.url,
            stub.session,
            1,
            String(key, Charsets.UTF_8),
            "test-node-1",
            listOf("flight"),
            localizationPins,
        )
        val link = RelayLink(nodeConfig, aircraft, executor, phone, timing = timing, log = { logs += it })
        // The loop's status feeds the snapshot fields the link reports (the sessions do this on the phone).
        Thread {
            var last = executor.status.value
            while (!Thread.currentThread().isInterrupted) {
                val current = executor.status.value
                if (current != last) {
                    aircraft.applyStatus(current)
                    last = current
                }
                try {
                    Thread.sleep(10)
                } catch (_: InterruptedException) {
                    return@Thread
                }
            }
        }.apply {
            isDaemon = true
            start()
        }
        executor.observe(link.state)
        link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
        link.start()
        return Node(aircraft, executor, link)
    }

    private fun await(what: String, timeoutMs: Long = 10_000, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return
            Thread.sleep(10)
        }
        throw AssertionError("timed out waiting for $what; log:\n" + logs.joinToString("\n"))
    }

    private fun JsonObject.str(key: String): String = (this[key] as JsonString).value

    private fun JsonObject.bool(key: String): Boolean = (this[key] as JsonBool).value

    private fun StubRelay.awaitAck(commandId: String, status: String, timeoutMs: Long = 10_000): JsonObject =
        awaitFrame("acknowledgement", timeoutMs) { it.str("command_id") == commandId && it.str("status") == status }

    private fun StubRelay.acks(commandId: String): List<JsonObject> = frames("acknowledgement") { it.str("command_id") == commandId }

    @Test
    fun `diagnostic localization cannot advertise navigation or alter the existing goto path`() {
        StubRelay(key).use { stub ->
            val pins = LocalizationPins("map-a", "geometry-a", "camera-a", "body-a")
            node(stub, flying = false, localizationPins = pins).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                val join = stub.frames("membership") { it.str("action") == "join" }.single()
                assertEquals(Json.value(listOf("flight")), join["capabilities"])
                val takeoff = stub.issueCommand(CommandArgs.Takeoff(zMm = 1200))
                stub.awaitAck(takeoff.commandId, "completed")
                val takeoffAcks = stub.acks(takeoff.commandId).map { it.str("status") }
                assertEquals("accepted", takeoffAcks.first())
                assertEquals("executing", takeoffAcks[1])
                assertEquals("completed", takeoffAcks.last())
                assertEquals(FlightStates.HOVERING, node.aircraft.snapshot.value.state)

                val beforeDiagnostic = node.aircraft.snapshot.value
                stub.sendControlPose(xMm = 900_000, yMm = -900_000, zMm = 900_000)
                await("signed diagnostic pose") { node.link.state.value.controlPose?.xMm == 900_000L }
                Thread.sleep(100)
                val afterDiagnostic = node.aircraft.snapshot.value
                assertEquals(beforeDiagnostic.x, afterDiagnostic.x, 0.001, "diagnostic x cannot move hardware")
                assertEquals(beforeDiagnostic.y, afterDiagnostic.y, 0.001, "diagnostic y cannot move hardware")
                assertEquals(beforeDiagnostic.z, afterDiagnostic.z, 0.001, "diagnostic z cannot move hardware")

                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500))
                stub.awaitFrame("node_status") { it.bool("virtual_stick_enabled") }
                stub.awaitAck(goto.commandId, "completed")
                val gotoAcks = stub.acks(goto.commandId)
                assertEquals("accepted", gotoAcks.first().str("status"))
                val executing = gotoAcks.filter { it.str("status") == "executing" }
                assertTrue(executing.size >= 2, "progress acknowledgements: ${gotoAcks.map { it.str("status") }}")
                assertTrue(executing.first().str("detail").contains("forward 0.50"))
                assertTrue(gotoAcks.last().str("detail").contains("north 1.00"))
                assertTrue(node.aircraft.snapshot.value.y in 0.75..1.05, "moved north ${node.aircraft.snapshot.value.y}")
                await("virtual stick released") { !node.aircraft.model.virtualStickEnabled }
                await("node_status reports virtual stick off again") {
                    stub.frames("node_status").let { it.size >= 3 && !it.last().bool("virtual_stick_enabled") }
                }

                val hover = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(hover.commandId, "completed")
                val land = stub.issueCommand(CommandArgs.Land)
                stub.awaitAck(land.commandId, "completed")
                assertEquals(FlightStates.LANDED, node.aircraft.snapshot.value.state)
                assertTrue(stub.acks(land.commandId).any { it.str("status") == "executing" && it.str("detail").contains("auto-landing started") })

                // Camera commands still reach the fake fixture.
                val photo = stub.issueCommand(CommandArgs.CapturePhoto("cap-1"))
                assertEquals("unsupported", stub.awaitAck(photo.commandId, "failed").str("reason"))
            }
        }
    }

    @Test
    fun `the stick stream runs at the relay's virtual_stick_hz`() {
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 20, watchdogHoldMs = 5000, watchdogFailsafeMs = 20000)
        StubRelay(key, nodeSettings = settings).use { stub ->
            node(stub, flying = true).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                val sends = CopyOnWriteArrayList<Long>()
                node.executor.onStickSent = { _, _, now -> sends += now }
                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(goto.commandId, "completed")
                val span = sends.last() - sends.first()
                val rate = (sends.size - 1) * 1000.0 / span
                assertTrue(rate in 16.0..24.0, "measured stick rate $rate Hz over ${sends.size} sends in $span ms")
                assertTrue(node.executor.status.value.sticksSent >= sends.size.toLong())
            }
        }
    }

    @Test
    fun `relay silence holds the stream then lands and the acknowledgement names watchdog_hold`() {
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 400, watchdogFailsafeMs = 1500)
        StubRelay(key, nodeSettings = settings, emitControlHeartbeats = false).use { stub ->
            node(stub, flying = true).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                // The stub sends no heartbeat after join; the command cannot refresh the lease.
                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 4000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(goto.commandId, "executing")
                val hold = stub.awaitAck(goto.commandId, "failed")
                assertEquals("watchdog_hold", hold.str("reason"))
                assertTrue(hold.str("detail").contains("retryable"))
                await("loop in hold") { node.executor.status.value.phase == "watchdog_hold" }
                assertTrue(node.aircraft.model.virtualStickEnabled, "neutral sticks keep flowing during hold")
                stub.awaitFrame("node_status") { it.str("watchdog_state") == "hold" }
                stub.awaitFrame("node_status") { it.str("watchdog_state") == "failsafe" }
                await("failsafe landing") { node.executor.status.value.landingReason == "watchdog_failsafe" || node.aircraft.snapshot.value.state == FlightStates.LANDED }
                await("landed") { node.aircraft.snapshot.value.state == FlightStates.LANDED }
                assertTrue(logs.any { it.contains("never return to home") })
                assertTrue(node.aircraft.snapshot.value.y < 1.5, "the step was cut at hold, y ${node.aircraft.snapshot.value.y}")
            }
        }
    }

    @Test
    fun `a relay that only echoes the node's telemetry still holds the stream then lands`() {
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 400, watchdogFailsafeMs = 1500)
        StubRelay(key, nodeSettings = settings, echoTelemetry = true, emitControlHeartbeats = false).use { stub ->
            node(stub, flying = true).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                await("the node's telemetry coming back") { stub.echoed.get() >= 3 }
                // The signed command and echoes of the node's 10 Hz telemetry keep arriving;
                // neither is an authorized control heartbeat.
                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 4000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(goto.commandId, "executing")
                val hold = stub.awaitAck(goto.commandId, "failed")
                assertEquals("watchdog_hold", hold.str("reason"))
                await("loop in hold") { node.executor.status.value.phase == "watchdog_hold" }
                stub.awaitFrame("node_status") { it.str("watchdog_state") == "hold" }
                stub.awaitFrame("node_status") { it.str("watchdog_state") == "failsafe" }
                await("failsafe landing") { node.executor.status.value.landingReason == "watchdog_failsafe" || node.aircraft.snapshot.value.state == FlightStates.LANDED }
                await("landed") { node.aircraft.snapshot.value.state == FlightStates.LANDED }
                assertTrue(logs.any { it.contains("never return to home") })
                assertTrue(stub.echoed.get() >= 15, "echoes flowed throughout: ${stub.echoed.get()}")
                assertTrue(node.aircraft.snapshot.value.y < 1.5, "the step was cut at hold, y ${node.aircraft.snapshot.value.y}")
            }
        }
    }

    @Test
    fun `an RC takeover fails the command with authority_lost and drops readiness control authority until re-armed, every time`() {
        StubRelay(key).use { stub ->
            node(stub, flying = true).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                // First takeover: the pause button during a goto.
                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 4000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(goto.commandId, "executing")
                node.executor.onTakeover("rc_pause", "pause button pressed")
                val failed = stub.awaitAck(goto.commandId, "failed")
                assertEquals("authority_lost", failed.str("reason"))
                assertTrue(failed.str("detail").contains("pause button pressed"))
                val readiness = stub.awaitFrame("membership") { it.str("action") == "readiness" && !it.bool("control_authority") }
                assertTrue(readiness.bool("rc_safety_operator_present"))
                val status = stub.awaitFrame("node_status") { it["authority_change_reason"] == JsonString("rc_pause") }
                assertTrue(!status.bool("control_authority"))
                await("degraded") { node.link.state.value.membership == "degraded" }
                val refused = stub.issueCommand(CommandArgs.Hover)
                assertEquals("authority_lost", stub.awaitAck(refused.commandId, "failed").str("reason"))

                node.executor.rearmAuthority()
                stub.awaitFrames("membership", 2) { it.str("action") == "readiness" && it.bool("control_authority") }
                await("ready again") { node.link.state.value.membership == "ready" }
                val hover = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(hover.commandId, "completed")

                // Second takeover after the re-arm: a stick past the threshold during the next
                // goto must cancel it too; nothing between the RC and the loop may latch stale.
                val again = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 4000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(again.commandId, "executing")
                node.executor.onTakeover("rc_takeover", "right stick 60%")
                val failedAgain = stub.awaitAck(again.commandId, "failed")
                assertEquals("authority_lost", failedAgain.str("reason"))
                assertTrue(failedAgain.str("detail").contains("right stick 60%"))
                stub.awaitFrame("node_status") { it["authority_change_reason"] == JsonString("rc_takeover") }
                await("virtual stick released") { !node.aircraft.model.virtualStickEnabled }
                await("degraded again") { node.link.state.value.membership == "degraded" }
                assertTrue(node.aircraft.snapshot.value.y < 2.0, "the second step was cut, y ${node.aircraft.snapshot.value.y}")

                node.executor.rearmAuthority()
                await("ready once more") { node.link.state.value.membership == "ready" }
                val done = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(done.commandId, "completed")
            }
        }
    }

    @Test
    fun `with the pilot's control authority toggle off the link refuses motion and an airborne hover still holds`() {
        StubRelay(key).use { stub ->
            node(stub, flying = true).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                node.link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = false, rcSafetyOperatorPresent = true))
                await("degraded") { node.link.state.value.membership == "degraded" }
                val goto = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 4000, zMm = 1200, speedMmS = 500))
                val refused = stub.awaitAck(goto.commandId, "failed")
                assertEquals("authority_lost", refused.str("reason"))
                assertTrue(refused.str("detail").contains("Control authority toggle"), refused.str("detail"))
                assertEquals(listOf("failed"), stub.acks(goto.commandId).map { it.str("status") }, "never accepted")
                assertEquals(0.0, node.aircraft.snapshot.value.y, 1e-9)
                assertTrue(!node.aircraft.model.virtualStickEnabled, "no virtual stick for a refused motion")
                // The airborne hover keeps its fail-safe behaviour: virtual stick, neutral sticks, settle.
                val hover = stub.issueCommand(CommandArgs.Hover)
                stub.awaitAck(hover.commandId, "completed")
                assertTrue(stub.acks(hover.commandId).any { it.str("status") == "executing" && it.str("detail").contains("neutral sticks") })
                await("virtual stick released") { !node.aircraft.model.virtualStickEnabled }

                node.link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
                await("ready again") { node.link.state.value.membership == "ready" }
                val allowed = stub.issueCommand(CommandArgs.Goto(xMm = 0, yMm = 1000, zMm = 1200, speedMmS = 500))
                stub.awaitAck(allowed.commandId, "completed")
                assertTrue(node.aircraft.snapshot.value.y in 0.75..1.05, "moved north ${node.aircraft.snapshot.value.y}")
            }
        }
    }

    @Test
    fun `closing the executor mid-hold releases virtual stick instead of leaving the flight controller waiting`() {
        val aircraft = FakeFlightAircraft()
        aircraft.setConnected(true)
        aircraft.place(zUp = 1.2, flying = true)
        val executor = FlightExecutor(aircraft, aircraft, aircraft.fake, config = config, log = { logs += it })
        val settings = NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 5000, watchdogFailsafeMs = 20000)
        executor.observe(MutableStateFlow(LinkState(joined = true, nodeSettings = settings, lastRelayActivityMs = System.currentTimeMillis())))
        await("deadman armed") { executor.status.value.watchdog == "armed" }
        val sink = object : ReportSink {
            override fun executing(detail: String?) = Unit

            override fun completed(detail: String?) = Unit

            override fun failed(reason: FlightReason, detail: String?) = Unit
        }
        executor.startBench("hold", StickFrame.NEUTRAL, 60_000, sink)
        await("virtual stick enabled") { aircraft.model.virtualStickEnabled }
        executor.close()
        assertTrue(!aircraft.model.virtualStickEnabled, "virtual stick released when the ticker stopped; log:\n" + logs.joinToString("\n"))
        assertTrue(logs.any { it.contains("flight loop stopped with virtual stick enabled") }, logs.joinToString("\n"))
    }
}
