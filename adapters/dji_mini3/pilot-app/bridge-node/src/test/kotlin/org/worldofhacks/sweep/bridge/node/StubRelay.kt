package org.worldofhacks.sweep.bridge.node

import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.worldofhacks.sweep.bridge.core.frames.AuthAccepted
import org.worldofhacks.sweep.bridge.core.frames.AuthFrame
import org.worldofhacks.sweep.bridge.core.frames.AuthRefused
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.NodeSettings
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.signing.Signing

/**
 * A stub relay on OkHttp's MockWebServer speaking just enough of the node protocol for the
 * link tests: auth, the initial state, membership join and readiness answers (with the
 * relay-assigned connection epoch incrementing on every join), signed commands, and
 * relay-side socket drops. Every node frame is recorded in arrival order. With
 * [echoTelemetry] it also sends each telemetry frame straight back on the socket, as the
 * real relay's fan-out does for every node frame, and nothing else unprompted.
 */
class StubRelay(
    private val key: ByteArray,
    private val droneId: Int = 1,
    val session: String = "session-a",
    val nodeSettings: NodeSettings = NodeSettings(2000, 10, 2000, 10000),
    private val clockOffsetMs: Long = 0,
    initialRosterVersion: Int = 3,
    private val refuseAuth: Pair<String, String>? = null,
    private val echoTelemetry: Boolean = false,
) : AutoCloseable {
    private val server = MockWebServer()
    private val sockets = CopyOnWriteArrayList<WebSocket>()
    private val events = AtomicLong()

    val frames = CopyOnWriteArrayList<JsonObject>()
    /** Telemetry frames sent back to the node under [echoTelemetry]. */
    val echoed = AtomicLong()
    val connections = AtomicInteger()
    val epoch = AtomicInteger()
    val rosterVersion = AtomicInteger(initialRosterVersion)
    val seq = AtomicLong()

    init {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = MockResponse().withWebSocketUpgrade(NodeSocket())
        }
        server.start()
    }

    val url: String
        get() = "ws://${server.hostName}:${server.port}"

    fun relayNow(): Long = System.currentTimeMillis() + clockOffsetMs

    fun frames(type: String, predicate: (JsonObject) -> Boolean = { true }): List<JsonObject> =
        frames.filter { (it["type"] as? JsonString)?.value == type && predicate(it) }

    fun awaitFrame(type: String, timeoutMs: Long = 5_000, predicate: (JsonObject) -> Boolean = { true }): JsonObject {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            frames(type, predicate).firstOrNull()?.let { return it }
            Thread.sleep(10)
        }
        throw AssertionError("stub relay never received a $type frame matching the predicate; saw ${frames.map { (it["type"] as? JsonString)?.value }}")
    }

    fun awaitFrames(type: String, count: Int, timeoutMs: Long = 5_000, predicate: (JsonObject) -> Boolean = { true }): List<JsonObject> {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val matching = frames(type, predicate)
            if (matching.size >= count) return matching
            Thread.sleep(10)
        }
        throw AssertionError("stub relay received ${frames(type, predicate).size} $type frames, wanted $count")
    }

    fun issueCommand(
        args: CommandArgs,
        seq: Long = this.seq.incrementAndGet(),
        connectionEpoch: Int = epoch.get(),
        rosterVersion: Int = this.rosterVersion.get(),
        issuedAt: Long = relayNow(),
        ttlMs: Long = nodeSettings.commandTtlMs,
        signingKey: ByteArray = key,
        intentId: String = "intent-1",
        commandId: String = "cmd-${events.incrementAndGet()}",
    ): CommandFrame {
        val frame = CommandFrame(
            t = relayNow(),
            eventId = eventId(),
            session = session,
            commandId = commandId,
            intentId = intentId,
            rosterVersion = rosterVersion,
            droneId = droneId,
            connectionEpoch = connectionEpoch,
            seq = seq,
            issuedAt = issuedAt,
            ttlMs = ttlMs,
            operation = args.operation,
            args = args,
        ).signed(signingKey)
        broadcast(frame.toJson())
        return frame
    }

    fun sendState(rosterVersion: Int = this.rosterVersion.get(), estop: Boolean = false) {
        this.rosterVersion.set(rosterVersion)
        broadcast(stateEvent(estop))
    }

    fun dropConnections() {
        sockets.forEach { it.close(1001, "relay going away") }
    }

    override fun close() {
        // A server-side socket the node already closed has no call behind it; cancelling it throws.
        sockets.forEach { runCatching { it.cancel() } }
        server.shutdown()
    }

    private fun broadcast(json: JsonObject) {
        val text = Json.canonical(json)
        sockets.forEach { it.send(text) }
    }

    private fun eventId(): String = "relay-event-${events.incrementAndGet()}"

    private fun stateEvent(estop: Boolean = false): JsonObject = Json.value(
        mapOf(
            "v" to 1,
            "t" to relayNow(),
            "type" to "state",
            "event_id" to eventId(),
            "session" to session,
            "roster_version" to rosterVersion.get(),
            "armed" to false,
            "estop" to estop,
            "selection" to emptyList<Int>(),
            "formation" to "line",
            "spacing" to 1.0,
            "mode" to "indoor",
            "pending" to null,
            "accepted_plan" to null,
            "drones" to emptyList<Any>(),
        ),
    ) as JsonObject

    private fun membershipEvent(action: String, membership: String, reason: String?, readinessReasons: List<String>): JsonObject =
        Json.value(
            mapOf(
                "v" to 1,
                "t" to relayNow(),
                "type" to "membership",
                "event_id" to eventId(),
                "session" to session,
                "action" to action,
                "drone_id" to droneId,
                "connection_epoch" to epoch.get(),
                "membership" to membership,
                "roster_version" to rosterVersion.get(),
                "reason" to reason,
                "readiness_reasons" to readinessReasons,
                "adapter_id" to "stub",
                "capabilities" to listOf("flight"),
                "provenance" to "adapter_signature",
            ),
        ) as JsonObject

    private fun refusalEvent(reason: String, detail: String): JsonObject = Json.value(
        mapOf(
            "v" to 1,
            "t" to relayNow(),
            "type" to "refusal",
            "event_id" to eventId(),
            "session" to session,
            "intent_id" to null,
            "command_id" to null,
            "status" to "refused",
            "source" to "adapter",
            "drone_id" to droneId,
            "connection_epoch" to epoch.get(),
            "roster_version" to rosterVersion.get(),
            "reason" to reason,
            "detail" to detail,
        ),
    ) as JsonObject

    private inner class NodeSocket : WebSocketListener() {
        private var authenticated = false

        override fun onOpen(webSocket: WebSocket, response: Response) {
            connections.incrementAndGet()
            sockets += webSocket
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val frame = Json.parse(text) as JsonObject
            frames += frame
            val type = (frame["type"] as JsonString).value
            if (!authenticated) {
                if (type != AuthFrame.TYPE) {
                    webSocket.close(1008, "auth first")
                    return
                }
                val auth = AuthFrame.parse(frame)
                val refusal = refuseAuth
                    ?: if (auth.droneId != droneId || auth.token != String(key, Charsets.UTF_8)) {
                        "authentication_failed" to "credential was not accepted"
                    } else {
                        null
                    }
                if (refusal != null) {
                    webSocket.send(Json.canonical(AuthRefused(relayNow(), eventId(), session, refusal.first, refusal.second).toEvent()))
                    webSocket.close(1008, "refused")
                    return
                }
                authenticated = true
                webSocket.send(Json.canonical(AuthAccepted(relayNow(), eventId(), session, "adapter", droneId, nodeSettings).toEvent()))
                webSocket.send(Json.canonical(stateEvent()))
                return
            }
            if (echoTelemetry && type == "telemetry" && webSocket.send(text)) echoed.incrementAndGet()
            if (type != "membership") return
            val signature = (frame["signature"] as JsonString).value
            if (!Signing.verify(frame.without("signature"), signature, key)) {
                webSocket.send(Json.canonical(refusalEvent("invalid_signature", "membership signature did not verify")))
                return
            }
            when ((frame["action"] as JsonString).value) {
                "join" -> {
                    val assigned = epoch.incrementAndGet()
                    rosterVersion.incrementAndGet()
                    webSocket.send(
                        Json.canonical(
                            membershipEvent(
                                "join",
                                "registered",
                                if (assigned == 1) "authenticated_join" else "authenticated_rejoin",
                                listOf("telemetry_missing", "home_pose_missing", "control_authority_missing", "rc_safety_operator_missing"),
                            ),
                        ),
                    )
                }
                "readiness" -> {
                    val gates = listOf(
                        "home_pose_confirmed" to "home_pose_missing",
                        "control_authority" to "control_authority_missing",
                        "rc_safety_operator_present" to "rc_safety_operator_missing",
                    ).filterNot { (field, _) -> (frame[field] as JsonBool).value }.map { it.second }
                    rosterVersion.incrementAndGet()
                    webSocket.send(
                        Json.canonical(
                            membershipEvent(
                                "readiness",
                                if (gates.isEmpty()) "ready" else "degraded",
                                if (gates.isEmpty()) null else "readiness_gate_failed",
                                gates,
                            ),
                        ),
                    )
                }
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            sockets -= webSocket
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            sockets -= webSocket
        }
    }
}
