package org.worldofhacks.sweep.bridge.node

import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.worldofhacks.sweep.bridge.core.admission.AdmissionResult
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.admission.CommandAdmission
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.frames.AcknowledgementFrame
import org.worldofhacks.sweep.bridge.core.frames.AuthAccepted
import org.worldofhacks.sweep.bridge.core.frames.AuthFrame
import org.worldofhacks.sweep.bridge.core.frames.AuthRefused
import org.worldofhacks.sweep.bridge.core.frames.CapabilitiesFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandOperation
import org.worldofhacks.sweep.bridge.core.frames.ContractError
import org.worldofhacks.sweep.bridge.core.frames.LifecycleStatus
import org.worldofhacks.sweep.bridge.core.frames.MembershipEvent
import org.worldofhacks.sweep.bridge.core.frames.MembershipFrame
import org.worldofhacks.sweep.bridge.core.frames.NodeStatusBody
import org.worldofhacks.sweep.bridge.core.frames.NodeStatusFrame
import org.worldofhacks.sweep.bridge.core.frames.RefusalEvent
import org.worldofhacks.sweep.bridge.core.frames.StateEvent
import org.worldofhacks.sweep.bridge.core.frames.TelemetryFrame
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonParseException
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.watchdog.Watchdog
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogConfig
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogState

/**
 * The node's relay link: one OkHttp WebSocket to `<relay>/ws/{session}` speaking the node
 * protocol in `relay/README.md`, in the same frame order as `adapters/dji_mini3/fake_node.py`:
 *
 * 1. `auth` (source `adapter`, drone id, per-node token) as the first frame.
 * 2. `auth.accepted` arrives with the relay-distributed `node` thresholds (command TTL,
 *    Virtual Stick rate, watchdog hold and failsafe) and stamps the relay clock; the offset
 *    against the local clock is measured from that exchange and feeds [CommandAdmission].
 * 3. The relay's initial `state` supplies the roster version; the signed `join` follows.
 * 4. The relay answers with a `membership` join event carrying this node's connection epoch
 *    (1 on first join, incremented by the relay on every rejoin); the node binds admission to
 *    it, arms the watchdog, then sends one telemetry frame (so a confirmed home pose can be
 *    captured), the signed `readiness`, `capabilities`, and `node_status`.
 * 5. Telemetry streams at [LinkTiming.telemetryHz] while an aircraft is connected;
 *    `node_status` is resent whenever its body changes; `readiness` is resent when a pilot
 *    toggle or the aircraft/RC connection changes (aircraft or RC loss reports
 *    `control_authority=false` while the socket stays up).
 * 6. `command` frames are verified and admitted; `accepted` is acknowledged on admission and
 *    the [CommandExecutor] reports `executing` then `completed` or `failed`. Rejections
 *    acknowledge `failed` with `stale_command` or `out_of_order_command`; forged frames are
 *    dropped without a reply.
 *
 * Socket loss schedules a reconnect with bounded exponential backoff. The watchdog keeps its
 * clock running through the outage (the deadman is mandatory: without it the flight
 * controller hovers indefinitely when frames stop) and moves to hold, then failsafe; the
 * flight action for those states is Phase E work, here they are reported in `node_status`
 * and the UI. An `auth.refused` with `session_closed` (the relay restarted; the old session
 * id is replay-only) or a credential failure halts automatic reconnects until the setup
 * changes or [reconnectNow] is called.
 *
 * Everything runs on one single-threaded loop; OkHttp callbacks are posted to it and fenced
 * by a socket generation so a late callback from a dead socket cannot touch a new one.
 */
class RelayLink(
    val config: NodeConfig,
    private val aircraft: AircraftSource,
    private val executor: CommandExecutor,
    private val phone: PhoneStatusSource,
    private val clock: Clock = SystemClock,
    private val timing: LinkTiming = LinkTiming(),
    private val log: NodeLog = NodeLog { },
    client: OkHttpClient? = null,
    private val videoPublish: VideoPublishSource = VideoPublishSource { VideoPublishState.STOPPED },
) : AutoCloseable {
    private val ownsClient = client == null
    private val client: OkHttpClient = client ?: RelayClients.build(timing)
    private val loop: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "relay-link").apply { isDaemon = true }
    }

    private val _state = MutableStateFlow(LinkState())
    val state: StateFlow<LinkState> = _state.asStateFlow()

    private val admission = CommandAdmission(config.key, config.droneId, clock)
    private var watchdog: Watchdog? = null
    private var socket: WebSocket? = null
    private var generation = 0
    private var running = false
    private var halted = false
    private var joinPending = false
    private var authSentAtMs: Long? = null
    private var lastT = 0L
    private var failures = 0
    private var reconnect: ScheduledFuture<*>? = null
    private var tickers: List<ScheduledFuture<*>> = emptyList()
    private var readiness = ReadinessInput()
    private var previousEpoch: Int? = null
    private var lastAircraftConnected: Boolean? = null
    private var lastRcConnected: Boolean? = null
    private var lastAuthorityLost: String? = null
    private var lastNodeStatus: NodeStatusBody? = null
    private val telemetryTimes = ArrayDeque<Long>()
    private val commands = LinkedHashMap<String, CommandRecord>()

    fun start() = post {
        if (running) return@post
        running = true
        halted = false
        failures = 0
        update { it.copy(halted = false) }
        startTickers()
        connect()
    }

    fun stop() = post {
        running = false
        reconnect?.cancel(false)
        reconnect = null
        tickers.forEach { it.cancel(false) }
        tickers = emptyList()
        dropSocket("node stopping")
        watchdog?.disarm()
        update {
            it.copy(
                connection = RelayConnection.DISCONNECTED,
                authenticated = false,
                joined = false,
                watchdog = WatchdogState.DISARMED,
                nextAttemptAtMs = null,
                backoffMs = null,
            )
        }
        log.log("relay link stopped")
    }

    /** Drops the current socket (if any), clears a halt, and connects immediately. */
    fun reconnectNow() = post {
        halted = false
        failures = 0
        reconnect?.cancel(false)
        reconnect = null
        update { it.copy(halted = false, nextAttemptAtMs = null, backoffMs = null) }
        if (!running) {
            running = true
            startTickers()
        }
        dropSocket("reconnect requested")
        connect()
    }

    /** The pilot's three toggles; a change while joined resends the signed readiness. */
    fun setReadiness(input: ReadinessInput) = post {
        readiness = input
        update { it.copy(readiness = input) }
        if (_state.value.joined) {
            sendReadiness()
            sendNodeStatusIfChanged(force = true)
        }
    }

    override fun close() {
        stop()
        loop.shutdown()
        loop.awaitTermination(2, TimeUnit.SECONDS)
        if (ownsClient) {
            client.dispatcher.executorService.shutdown()
            client.connectionPool.evictAll()
        }
    }

    // ---- connection lifecycle (loop thread) ----

    private fun connect() {
        if (!running || halted || socket != null) return
        val mine = ++generation
        joinPending = false
        authSentAtMs = null
        update {
            it.copy(
                connection = RelayConnection.CONNECTING,
                authenticated = false,
                joined = false,
                attempts = it.attempts + 1,
                nextAttemptAtMs = null,
            )
        }
        log.log("connecting to ${config.socketUrl} (attempt ${_state.value.attempts})")
        val request = Request.Builder().url(config.socketUrl).build()
        socket = client.newWebSocket(
            request,
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) = fenced(mine) { onOpen(webSocket) }

                override fun onMessage(webSocket: WebSocket, text: String) = fenced(mine) { onFrame(text) }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(NORMAL_CLOSURE, null)
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
                    fenced(mine) { onDown("socket closed ($code ${reason.ifEmpty { "no reason" }})") }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                    fenced(mine) { onDown("socket failure: ${t.javaClass.simpleName}${t.message?.let { ": $it" } ?: ""}") }
            },
        )
        schedule(timing.authTimeoutMs) {
            if (mine == generation && !_state.value.authenticated) {
                log.log("auth.accepted not received within ${timing.authTimeoutMs} ms")
                socket?.cancel()
            }
        }
    }

    private fun onOpen(webSocket: WebSocket) {
        authSentAtMs = clock.nowMs()
        val sent = webSocket.send(Json.canonical(AuthFrame(config.droneId, config.token).toJson()))
        if (sent) {
            update { it.copy(framesOut = it.framesOut + 1) }
            log.log("auth frame sent for drone ${config.droneId}")
        }
    }

    private fun onFrame(text: String) {
        val now = clock.nowMs()
        val json = try {
            Json.parse(text) as? JsonObject
        } catch (error: JsonParseException) {
            log.log("unparseable relay frame dropped: ${error.message}")
            null
        }
        if (json == null) {
            update { it.copy(connection = RelayConnection.DEGRADED, framesIn = it.framesIn + 1, lastError = "unparseable relay frame dropped") }
            return
        }
        update { it.copy(framesIn = it.framesIn + 1, lastRelayFrameAtMs = now) }
        watchdog?.heartbeat()
        when ((json["type"] as? JsonString)?.value) {
            AuthAccepted.TYPE -> onAuthAccepted(json, now)
            AuthRefused.TYPE -> onAuthRefused(json)
            StateEvent.TYPE -> onState(json)
            MembershipEvent.TYPE -> onMembership(json)
            CommandFrame.TYPE -> onCommand(json)
            RefusalEvent.TYPE -> onRefusal(json)
            else -> Unit // fan-out of telemetry, acknowledgements, and node-authored frames
        }
    }

    private fun onAuthAccepted(json: JsonObject, receivedAtMs: Long) {
        val accepted = parseOrLog("auth.accepted") { AuthAccepted.parse(json) } ?: return
        val settings = accepted.node
        if (settings == null) {
            log.log("auth.accepted carried no node thresholds; the watchdog cannot be configured")
            update { it.copy(lastError = "auth.accepted carried no node thresholds") }
            dropSocket("no node thresholds")
            scheduleReconnect()
            return
        }
        val sentAtMs = authSentAtMs ?: receivedAtMs
        val roundTripMs = receivedAtMs - sentAtMs
        // Single-sample offset: the relay stamped `t` somewhere inside the auth round trip.
        val offset = accepted.t - (sentAtMs + receivedAtMs) / 2
        admission.relayOffsetMs = offset
        watchdog = Watchdog(WatchdogConfig(settings.watchdogHoldMs, settings.watchdogFailsafeMs), clock)
        failures = 0
        update {
            it.copy(
                connection = RelayConnection.CONNECTED,
                authenticated = true,
                nodeSettings = settings,
                relayOffsetMs = offset,
                authRoundTripMs = roundTripMs,
                backoffMs = null,
                nextAttemptAtMs = null,
                lastAuthRefusal = null,
                lastError = null,
                watchdog = WatchdogState.DISARMED,
            )
        }
        log.log(
            "authenticated; relay clock offset $offset ms (auth round trip ${receivedAtMs - sentAtMs} ms); " +
                "thresholds ttl=${settings.commandTtlMs} ms stick=${settings.virtualStickHz} Hz " +
                "hold=${settings.watchdogHoldMs} ms failsafe=${settings.watchdogFailsafeMs} ms",
        )
        joinPending = true
        schedule(timing.joinFallbackMs) {
            if (joinPending && _state.value.authenticated) {
                log.log("no initial state within ${timing.joinFallbackMs} ms; sending join anyway")
                joinPending = false
                sendJoin()
            }
        }
    }

    private fun onAuthRefused(json: JsonObject) {
        val refused = parseOrLog("auth.refused") { AuthRefused.parse(json) } ?: return
        val halt = refused.reason in HALT_REASONS
        halted = halted || halt
        update { it.copy(lastAuthRefusal = refused, halted = halted) }
        log.log(
            "relay refused authentication: ${refused.reason} (${refused.detail})" +
                if (halt) "; automatic reconnect stopped until the setup changes or Reconnect is pressed" else "",
        )
    }

    private fun onState(json: JsonObject) {
        val event = parseOrLog("state") { StateEvent.parse(json) } ?: return
        admission.updateRosterVersion(event.rosterVersion)
        val mine = event.drone(config.droneId)
        update {
            it.copy(
                rosterVersion = event.rosterVersion,
                estop = event.estop,
                membership = mine?.membership ?: it.membership,
                readinessReasons = mine?.readinessReasons ?: it.readinessReasons,
            )
        }
        if (joinPending) {
            joinPending = false
            sendJoin()
        }
    }

    private fun onMembership(json: JsonObject) {
        val event = parseOrLog("membership") { MembershipEvent.parse(json) } ?: return
        if (event.droneId != config.droneId) return
        admission.updateRosterVersion(event.rosterVersion)
        update {
            it.copy(
                rosterVersion = event.rosterVersion,
                membership = event.membership,
                membershipReason = event.reason,
                readinessReasons = event.readinessReasons,
            )
        }
        log.log(
            "membership ${event.action}: ${event.membership} (epoch ${event.connectionEpoch}, roster ${event.rosterVersion}" +
                (event.reason?.let { ", reason $it" } ?: "") +
                (if (event.readinessReasons.isEmpty()) ")" else ", gates ${event.readinessReasons})"),
        )
        if (event.action == MembershipFrame.Join::class.simpleName?.lowercase()) onJoined(event.connectionEpoch, event.rosterVersion)
    }

    private fun onJoined(epoch: Int, rosterVersion: Int) {
        val rejoin = previousEpoch != null && previousEpoch != epoch
        previousEpoch = epoch
        admission.bind(epoch, rosterVersion)
        val dog = watchdog
        dog?.arm()
        lastNodeStatus = null
        val snapshot = aircraft.snapshot.value
        lastAircraftConnected = snapshot.aircraftConnected
        lastRcConnected = snapshot.rcConnected
        lastAuthorityLost = snapshot.authorityLostReason
        update {
            it.copy(
                joined = true,
                connectionEpoch = epoch,
                rejoins = if (rejoin) it.rejoins + 1 else it.rejoins,
                watchdog = dog?.state ?: WatchdogState.DISARMED,
            )
        }
        log.log((if (rejoin) "rejoined" else "joined") + " as drone ${config.droneId}, connection epoch $epoch; watchdog armed")
        if (snapshot.aircraftConnected) sendTelemetry(snapshot)
        sendReadiness()
        sendCapabilities(snapshot)
        sendNodeStatusIfChanged(force = true)
    }

    private fun onRefusal(json: JsonObject) {
        val refusal = parseOrLog("refusal") { RefusalEvent.parse(json) } ?: return
        if (refusal.droneId != null && refusal.droneId != config.droneId) return
        update { it.copy(lastRefusal = refusal) }
        log.log("relay refused a node frame: ${refusal.reason} (${refusal.detail})")
    }

    private fun onDown(reason: String) {
        socket = null
        joinPending = false
        authSentAtMs = null
        update { it.copy(connection = RelayConnection.DISCONNECTED, authenticated = false, joined = false, lastError = reason) }
        val dog = watchdog
        log.log(
            "relay link down: $reason" +
                if (dog != null && dog.state != WatchdogState.DISARMED) "; watchdog clock keeps running (${dog.state})" else "",
        )
        scheduleReconnect()
    }

    private fun dropSocket(reason: String) {
        generation++
        socket?.close(NORMAL_CLOSURE, reason)
        socket = null
        joinPending = false
        if (_state.value.connection != RelayConnection.DISCONNECTED) {
            update { it.copy(connection = RelayConnection.DISCONNECTED, authenticated = false, joined = false) }
        }
    }

    private fun scheduleReconnect() {
        if (!running || halted) {
            update { it.copy(nextAttemptAtMs = null, backoffMs = null) }
            return
        }
        if (reconnect != null) return
        failures += 1
        val exponent = min(failures - 1, MAX_BACKOFF_EXPONENT)
        val base = min(timing.initialBackoffMs shl exponent, timing.maxBackoffMs)
        val jitter = if (base >= 4) Random.nextLong(base / 4) else 0L
        val delay = base + jitter
        val at = clock.nowMs() + delay
        update { it.copy(nextAttemptAtMs = at, backoffMs = delay) }
        log.log("reconnecting in $delay ms")
        reconnect = schedule(delay) {
            reconnect = null
            connect()
        }
    }

    // ---- outbound frames (loop thread) ----

    private fun sendJoin() {
        val join = MembershipFrame.Join(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            droneId = config.droneId,
            adapterId = config.adapterId,
            capabilities = config.capabilities,
        )
        if (send(join.signed(config.key))) log.log("join sent: adapter ${config.adapterId}, capabilities ${config.capabilities}")
    }

    private fun sendReadiness() {
        val current = _state.value
        val epoch = current.connectionEpoch ?: return
        if (!current.joined) return
        val snapshot = aircraft.snapshot.value
        val authority = effectiveAuthority(snapshot)
        val frame = MembershipFrame.Readiness(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            droneId = config.droneId,
            connectionEpoch = epoch,
            homePoseConfirmed = readiness.homePoseConfirmed,
            controlAuthority = authority,
            rcSafetyOperatorPresent = readiness.rcSafetyOperatorPresent,
        )
        if (send(frame.signed(config.key))) {
            update { it.copy(controlAuthority = authority, authorityChangeReason = authorityReason(snapshot)) }
            log.log(
                "readiness sent: epoch $epoch home_pose_confirmed=${readiness.homePoseConfirmed} " +
                    "control_authority=$authority rc_safety_operator_present=${readiness.rcSafetyOperatorPresent}",
            )
        }
    }

    private fun sendCapabilities(snapshot: AircraftSnapshot) {
        val epoch = _state.value.connectionEpoch ?: return
        val frame = CapabilitiesFrame(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            droneId = config.droneId,
            connectionEpoch = epoch,
            camera = snapshot.camera,
            hardware = snapshot.hardware,
        )
        if (send(frame.toEvent())) {
            log.log("capabilities sent: ${snapshot.hardware.aircraftModel}, sdk ${snapshot.hardware.sdkVersion}, panorama ${snapshot.camera.nativePanoramaModes}")
        }
    }

    private fun sendTelemetry(snapshot: AircraftSnapshot) {
        val epoch = _state.value.connectionEpoch ?: return
        val frame = TelemetryFrame(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            drone = config.droneId,
            connectionEpoch = epoch,
            x = finite(snapshot.x),
            y = finite(snapshot.y),
            z = finite(snapshot.z),
            vx = finite(snapshot.vx),
            vy = finite(snapshot.vy),
            vz = finite(snapshot.vz),
            battery = unit(snapshot.battery),
            state = snapshot.state.ifBlank { "unreported" },
            link = unit(snapshot.link),
            posQuality = unit(snapshot.posQuality),
        )
        if (send(frame.toEvent())) {
            val now = clock.nowMs()
            telemetryTimes.addLast(now)
            while (telemetryTimes.isNotEmpty() && telemetryTimes.first() < now - RATE_WINDOW_MS) telemetryTimes.removeFirst()
            val span = now - telemetryTimes.first()
            val rate = if (telemetryTimes.size >= 2 && span > 0) (telemetryTimes.size - 1) * 1000.0 / span else 0.0
            update { it.copy(telemetrySent = it.telemetrySent + 1, telemetryRateHz = rate) }
        }
    }

    private fun nodeStatusBody(): NodeStatusBody {
        val snapshot = aircraft.snapshot.value
        val phoneStatus = phone.current()
        return NodeStatusBody(
            virtualStickEnabled = snapshot.virtualStickEnabled, // set by the Phase E flight loop
            controlAuthority = effectiveAuthority(snapshot),
            authorityChangeReason = authorityReason(snapshot),
            watchdogState = (watchdog?.state ?: WatchdogState.DISARMED).toNodeStatus(),
            videoPublishState = videoPublish.current(),
            phoneBatteryPercent = phoneStatus.batteryPercent.coerceIn(0, 100),
            phoneThermalState = phoneStatus.thermalState,
        )
    }

    private fun sendNodeStatusIfChanged(force: Boolean = false) {
        val current = _state.value
        if (!current.joined) return
        val epoch = current.connectionEpoch ?: return
        val body = nodeStatusBody()
        if (!force && body == lastNodeStatus) return
        val frame = NodeStatusFrame(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            droneId = config.droneId,
            connectionEpoch = epoch,
            body = body,
        )
        if (send(frame.toEvent())) {
            lastNodeStatus = body
            update { it.copy(nodeStatus = body) }
            log.log(
                "node_status sent: control_authority=${body.controlAuthority} reason=${body.authorityChangeReason} " +
                    "watchdog=${body.watchdogState.wire} video=${body.videoPublishState.wire} phone=${body.phoneBatteryPercent}% ${body.phoneThermalState.wire}",
            )
        }
    }

    private fun sendAck(command: CommandFrame, status: LifecycleStatus, reason: String? = null, detail: String? = null) {
        val frame = AcknowledgementFrame(
            t = nextT(),
            eventId = eventId(),
            session = config.session,
            intentId = command.intentId,
            commandId = command.commandId,
            status = status,
            droneId = config.droneId,
            connectionEpoch = command.connectionEpoch,
            rosterVersion = command.rosterVersion,
            reason = reason,
            detail = detail?.take(MAX_DETAIL)?.ifBlank { null },
        )
        if (send(frame.toEvent())) {
            log.log("ack ${status.wire} for ${command.operation.wire} ${command.commandId} (seq ${command.seq})" + (reason?.let { ": $it" } ?: ""))
        }
    }

    // ---- commands (loop thread) ----

    private fun onCommand(json: JsonObject) {
        val command = try {
            CommandFrame.parse(json)
        } catch (error: ContractError) {
            log.log("dropping a malformed command: ${error.detail}")
            return
        }
        if (command.session != config.session) {
            log.log("dropping command ${command.commandId} for session ${command.session}")
            return
        }
        when (val result = admission.admit(command)) {
            is AdmissionResult.Rejected -> {
                if (result.reason.acknowledged) {
                    sendAck(command, LifecycleStatus.FAILED, result.reason.wire, result.detail)
                    record(command, "failed", result.reason.wire, result.detail)
                } else {
                    log.log("dropped command ${command.commandId} without acknowledgement: ${result.reason.wire}: ${result.detail}")
                    record(command, "dropped", result.reason.wire, result.detail)
                }
            }
            is AdmissionResult.Admitted -> {
                val dog = watchdog
                if (dog != null && dog.state == WatchdogState.FAILSAFE) {
                    val detail = "watchdog is in failsafe after relay silence; it re-arms on the next join"
                    sendAck(command, LifecycleStatus.FAILED, WATCHDOG_FAILSAFE, detail)
                    record(command, "failed", WATCHDOG_FAILSAFE, detail)
                    return
                }
                dog?.command()
                sendAck(command, LifecycleStatus.ACCEPTED)
                record(command, "accepted")
                if (command.operation == CommandOperation.CAMERA_CAPABILITIES) sendCapabilities(aircraft.snapshot.value)
                val report = Report(command)
                try {
                    executor.execute(command, report)
                } catch (error: RuntimeException) {
                    report.failed("adapter_failure", error.message ?: error.javaClass.simpleName)
                }
            }
        }
    }

    private inner class Report(private val command: CommandFrame) : CommandReport {
        private var terminal = false

        override fun executing(detail: String?) = post {
            if (live()) {
                sendAck(command, LifecycleStatus.EXECUTING, detail = detail)
                record(command, "executing", detail = detail)
            }
        }

        override fun completed(detail: String?) = post {
            if (live()) {
                terminal = true
                sendAck(command, LifecycleStatus.COMPLETED, detail = detail)
                record(command, "completed", detail = detail)
            }
        }

        override fun failed(reason: String, detail: String?) = post {
            if (live()) {
                terminal = true
                val code = if (reason.isMachineCode()) reason else "adapter_failure"
                sendAck(command, LifecycleStatus.FAILED, code, detail)
                record(command, "failed", code, detail)
            }
        }

        private fun live(): Boolean {
            if (terminal) return false
            val current = _state.value
            if (!current.joined || current.connectionEpoch != command.connectionEpoch) {
                log.log("dropping a late report for ${command.commandId}: connection epoch changed")
                terminal = true
                return false
            }
            return true
        }
    }

    private fun record(command: CommandFrame, outcome: String, reason: String? = null, detail: String? = null) {
        val now = clock.nowMs()
        val existing = commands.remove(command.commandId)
        commands[command.commandId] = CommandRecord(
            commandId = command.commandId,
            intentId = command.intentId,
            operation = command.operation.wire,
            seq = command.seq,
            rosterVersion = command.rosterVersion,
            connectionEpoch = command.connectionEpoch,
            receivedAtMs = existing?.receivedAtMs ?: now,
            updatedAtMs = now,
            outcome = outcome,
            reason = reason,
            detail = detail,
        )
        while (commands.size > MAX_COMMANDS) commands.remove(commands.keys.first())
        update { it.copy(commands = commands.values.reversed()) }
    }

    // ---- tickers (loop thread) ----

    private fun startTickers() {
        if (tickers.isNotEmpty()) return
        val period = (1000.0 / timing.telemetryHz).toLong().coerceAtLeast(1)
        tickers = listOf(
            loop.scheduleAtFixedRate({ safely { telemetryTick() } }, period, period, TimeUnit.MILLISECONDS),
            loop.scheduleAtFixedRate({ safely { watchdogTick() } }, timing.watchdogPollMs, timing.watchdogPollMs, TimeUnit.MILLISECONDS),
        )
    }

    private fun telemetryTick() {
        if (!_state.value.joined) return
        val snapshot = aircraft.snapshot.value
        if (!snapshot.aircraftConnected) return
        sendTelemetry(snapshot)
    }

    private fun watchdogTick() {
        watchdog?.poll()?.let { transition ->
            val action = when (transition.to) {
                WatchdogState.HOLD -> " (flight loop action at hold: neutral sticks and hover)"
                WatchdogState.FAILSAFE -> " (flight loop action at failsafe: land indoors, never return to home)"
                else -> ""
            }
            log.log("watchdog ${transition.from} -> ${transition.to} after ${transition.elapsedMs} ms without relay activity$action")
            update { it.copy(watchdog = transition.to) }
        }
        checkAircraft()
        sendNodeStatusIfChanged()
    }

    /**
     * Aircraft or RC loss reports `control_authority=false` and a `node_status`; the socket
     * stays up. A Phase E authority latch (RC takeover) or its re-arm does the same.
     */
    private fun checkAircraft() {
        val snapshot = aircraft.snapshot.value
        val previousAircraft = lastAircraftConnected
        val previousRc = lastRcConnected
        if (previousAircraft == null || previousRc == null) {
            lastAircraftConnected = snapshot.aircraftConnected
            lastRcConnected = snapshot.rcConnected
            lastAuthorityLost = snapshot.authorityLostReason
            return
        }
        if (snapshot.aircraftConnected == previousAircraft && snapshot.rcConnected == previousRc && snapshot.authorityLostReason == lastAuthorityLost) return
        lastAircraftConnected = snapshot.aircraftConnected
        lastRcConnected = snapshot.rcConnected
        lastAuthorityLost = snapshot.authorityLostReason
        log.log(
            "aircraft ${if (snapshot.aircraftConnected) "connected" else "disconnected"}, " +
                "rc ${if (snapshot.rcConnected) "connected" else "disconnected"}" +
                (snapshot.authorityLostReason?.let { ", authority latched: $it" } ?: "") +
                "; control authority ${effectiveAuthority(snapshot)}; relay socket stays up",
        )
        if (_state.value.joined) {
            if (snapshot.aircraftConnected && snapshot.rcConnected) sendTelemetry(snapshot)
            sendReadiness()
            sendNodeStatusIfChanged(force = true)
        }
    }

    // ---- helpers ----

    private fun effectiveAuthority(snapshot: AircraftSnapshot): Boolean =
        readiness.controlAuthority && snapshot.aircraftConnected && snapshot.rcConnected && snapshot.authorityLostReason == null

    /** Why control authority is what it is; snake_case for `node_status.authority_change_reason`. */
    private fun authorityReason(snapshot: AircraftSnapshot): String? = when {
        !readiness.controlAuthority -> "not_granted"
        !snapshot.aircraftConnected -> "aircraft_disconnected"
        !snapshot.rcConnected -> "rc_disconnected"
        snapshot.authorityLostReason != null -> snapshot.authorityLostReason // Phase E: RC takeover, latched until the pilot re-arms
        else -> null
    }

    private fun send(frame: JsonObject): Boolean {
        val webSocket = socket ?: return false
        val ok = webSocket.send(Json.canonical(frame))
        if (ok) update { it.copy(framesOut = it.framesOut + 1) } else log.log("send failed; the socket is closing")
        return ok
    }

    /** Relay-clock stamp that never regresses (the relay refuses a `t` below the previous one). */
    private fun nextT(): Long {
        lastT = maxOf(lastT, admission.relayNowMs())
        return lastT
    }

    private fun eventId(): String = UUID.randomUUID().toString()

    private inline fun update(transform: (LinkState) -> LinkState) {
        _state.value = transform(_state.value)
    }

    private inline fun <T> parseOrLog(what: String, parse: () -> T): T? = try {
        parse()
    } catch (error: ContractError) {
        log.log("dropping an unparseable $what frame: ${error.detail}")
        null
    }

    private fun fenced(mine: Int, block: () -> Unit) = post {
        if (mine == generation) block()
    }

    private fun post(block: () -> Unit) {
        try {
            loop.execute { safely(block) }
        } catch (_: RejectedExecutionException) {
            // the link is closed
        }
    }

    private fun schedule(delayMs: Long, block: () -> Unit): ScheduledFuture<*> =
        loop.schedule({ safely(block) }, delayMs, TimeUnit.MILLISECONDS)

    private inline fun safely(block: () -> Unit) {
        try {
            block()
        } catch (error: RuntimeException) {
            log.log("relay link task failed: $error")
        }
    }

    private fun finite(value: Double): Double = if (value.isFinite()) value else 0.0

    private fun unit(value: Double): Double = if (value.isFinite()) value.coerceIn(0.0, 1.0) else 0.0

    private fun String.isMachineCode(): Boolean = isNotEmpty() && all { it in 'a'..'z' || it in '0'..'9' || it == '_' }

    private companion object {
        const val NORMAL_CLOSURE = 1000
        const val MAX_BACKOFF_EXPONENT = 16
        const val MAX_COMMANDS = 50
        const val MAX_DETAIL = 512
        const val RATE_WINDOW_MS = 2_000L
        const val WATCHDOG_FAILSAFE = "watchdog_failsafe"
        val HALT_REASONS = setOf("session_closed", "authentication_failed", "invalid_auth", "unknown_source")
    }
}
