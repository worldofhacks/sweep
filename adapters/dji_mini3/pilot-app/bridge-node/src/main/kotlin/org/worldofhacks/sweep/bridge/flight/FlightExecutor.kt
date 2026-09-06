package org.worldofhacks.sweep.bridge.flight

import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.asCoroutineDispatcher
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.yield
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.flight.AircraftFacts
import org.worldofhacks.sweep.bridge.core.flight.AxisMapping
import org.worldofhacks.sweep.bridge.core.flight.FlightCommand
import org.worldofhacks.sweep.bridge.core.flight.FlightConfig
import org.worldofhacks.sweep.bridge.core.flight.FlightController
import org.worldofhacks.sweep.bridge.core.flight.FlightPort
import org.worldofhacks.sweep.bridge.core.flight.FlightReason
import org.worldofhacks.sweep.bridge.core.flight.FlightSettings
import org.worldofhacks.sweep.bridge.core.flight.FlightStatus
import org.worldofhacks.sweep.bridge.core.flight.LinkFacts
import org.worldofhacks.sweep.bridge.core.flight.NavigationConfig
import org.worldofhacks.sweep.bridge.core.flight.NavigationEvidence
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.flight.ReportSink
import org.worldofhacks.sweep.bridge.core.flight.StickFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.node.AircraftSnapshot
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.CommandExecutor
import org.worldofhacks.sweep.bridge.node.CommandReport
import org.worldofhacks.sweep.bridge.node.FlightStates
import org.worldofhacks.sweep.bridge.node.LinkState
import org.worldofhacks.sweep.bridge.node.NodeLog

/**
 * Runs the [FlightController] on the phone: one dedicated single-threaded coroutine
 * dispatcher owns the controller, a ticker drives it at the relay's `virtual_stick_hz`
 * (clamped to 5 to 25 Hz, drift-free), the aircraft snapshot and the relay link state are
 * mirrored into it, and every port callback is posted back onto that thread. It is the
 * node's [CommandExecutor]: flight operations go to the loop, everything else to [fallback]
 * (the flavor's own executor, which owns the camera path).
 *
 * The ticker is the stick stream, so it survives any failure of one tick, and if it is ever
 * stopped ([close]) it sends neutral sticks and disables Virtual Stick before it exits, so
 * the flight controller is never left waiting for frames nobody sends.
 */
class FlightExecutor(
    port: FlightPort,
    private val aircraft: AircraftSource,
    private val fallback: CommandExecutor? = null,
    private val clock: Clock = SystemClock,
    config: FlightConfig = FlightConfig(),
    private val log: NodeLog = NodeLog { },
) : CommandExecutor, AutoCloseable {
    private val rawPort: FlightPort = port
    private val loop = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "flight-loop").apply { isDaemon = true } }
    private val dispatcher = loop.asCoroutineDispatcher()
    private val scope = CoroutineScope(SupervisorJob() + dispatcher)

    /** The pure loop; only touch it from [post]ed blocks (tests included). */
    val controller = FlightController(PostingPort(port), clock, config) { line -> log.log("flight: $line") }

    private val _status = MutableStateFlow(controller.status)
    val status: StateFlow<FlightStatus> = _status.asStateFlow()

    /** Called on the loop thread for every stick frame; the bench recorder hangs here. */
    @Volatile
    var onStickSent: ((seq: Long, frame: StickFrame, nowMs: Long) -> Unit)? = null

    init {
        controller.onStatus = { next -> _status.value = next }
        controller.onStickSent = { seq, frame, now -> onStickSent?.invoke(seq, frame, now) }
        scope.launch { aircraft.snapshot.collect { snapshot -> controller.updateAircraft(facts(snapshot)) } }
        scope.launch { ticker() }
    }

    private suspend fun ticker() {
        var deadline = clock.nowMs()
        try {
            while (true) {
                val now = clock.nowMs()
                try {
                    controller.tick(now)
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Throwable) {
                    // One failed tick must not end the stream; the next tick runs on schedule.
                    log.log("flight loop tick failed: $error")
                }
                deadline = controller.cadence.nextDeadline(deadline, now)
                val wait = deadline - clock.nowMs()
                if (wait > 0) delay(wait) else yield()
            }
        } finally {
            // Only cancellation gets here; never leave Virtual Stick on with nobody streaming.
            if (controller.virtualStickEnabled) {
                rawPort.sendStick(StickFrame.NEUTRAL)
                rawPort.disableVirtualStick { }
                log.log("flight loop stopped with virtual stick enabled: neutral sticks sent and virtual stick disabled")
            }
        }
    }

    /** Mirrors relay state and admitted navigation evidence into the loop. */
    fun observe(link: StateFlow<LinkState>): Job = scope.launch {
        link.collect { state ->
            controller.updateLink(linkFacts(state))
            controller.updateNavigation(
                NavigationEvidence(
                    authorization = state.navigationAuthorization,
                    pose = state.navigationPose,
                    poseFreshUntilMs = state.navigationPoseFreshUntilMs,
                    relayOffsetMs = state.relayOffsetMs,
                ),
            )
        }
    }

    override fun execute(command: CommandFrame, report: CommandReport) {
        if (FlightCommand.isFlight(command.args)) {
            post {
                try {
                    controller.execute(FlightCommand(command.commandId, command.args), ReportBridge(report))
                } catch (error: RuntimeException) {
                    // The relay must not wait out its timeout on a command the loop could not plan.
                    log.log("flight command ${command.commandId} failed inside the loop: $error")
                    report.failed("node_error", "the flight loop could not run the command: $error [terminal]")
                }
            }
        } else {
            val other = fallback
            if (other == null) {
                report.failed("unsupported", "${command.operation.wire} is not a flight operation and this node has no other executor")
            } else {
                other.execute(command, report)
            }
        }
    }

    /** Physical RC input, the pause or RTH button, or a mode switch while the loop was active. */
    fun onTakeover(reason: String, detail: String? = null) = post { controller.onTakeover(reason, detail) }

    fun onVirtualStickState(enabled: Boolean, ownedBySdk: Boolean, owner: String) = post { controller.onVirtualStickState(enabled, ownedBySdk, owner) }

    /** The pilot's re-arm after a takeover; readiness reports control authority again. */
    fun rearmAuthority() = post { controller.rearmAuthority() }

    fun setMapping(mapping: AxisMapping) = post { controller.mapping = mapping }

    fun configureNavigation(config: NavigationConfig?): Job = post { controller.configureNavigation(config) }

    fun reportFailsafeSetting(value: String) = post { controller.reportFailsafeSetting(value) }

    fun startBench(label: String, frame: StickFrame, durationMs: Long, sink: ReportSink) = post { controller.startBench(label, frame, durationMs, sink) }

    fun stopBench() = post { controller.stopBench() }

    fun benchTakeoff(zMm: Long, sink: ReportSink) = post { controller.benchTakeoff(zMm, sink) }

    fun benchLand(sink: ReportSink) = post { controller.benchLand(sink) }

    /** Runs [block] on the loop thread. */
    fun post(block: () -> Unit): Job = scope.launch { block() }

    override fun close() {
        scope.cancel()
        loop.shutdown()
        try {
            // Lets the ticker's release of Virtual Stick run before the caller moves on.
            loop.awaitTermination(2, TimeUnit.SECONDS)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    /** Marshals every asynchronous port result back onto the loop thread. */
    private inner class PostingPort(private val inner: FlightPort) : FlightPort {
        override fun enableVirtualStick(onResult: (PortResult) -> Unit) = inner.enableVirtualStick { result -> post { onResult(result) } }

        override fun disableVirtualStick(onResult: (PortResult) -> Unit) = inner.disableVirtualStick { result -> post { onResult(result) } }

        override fun setAdvancedMode(enabled: Boolean) = inner.setAdvancedMode(enabled)

        override fun sendStick(frame: StickFrame) = inner.sendStick(frame)

        override fun startTakeoff(onResult: (PortResult) -> Unit) = inner.startTakeoff { result -> post { onResult(result) } }

        override fun startLanding(onResult: (PortResult) -> Unit) = inner.startLanding { result -> post { onResult(result) } }

        override fun advance(nowMs: Long) = inner.advance(nowMs)
    }

    private class ReportBridge(private val report: CommandReport) : ReportSink {
        override fun executing(detail: String?) = report.executing(detail)

        override fun completed(detail: String?) = report.completed(detail)

        override fun failed(reason: FlightReason, detail: String?) = report.failed(reason.wire, detail)
    }

    companion object {
        val FLYING_STATES = setOf(FlightStates.TAKING_OFF, FlightStates.AIRBORNE, FlightStates.HOVERING, FlightStates.LANDING, FlightStates.EMERGENCY)

        fun facts(snapshot: AircraftSnapshot): AircraftFacts {
            val flying = snapshot.state in FLYING_STATES
            return AircraftFacts(
                aircraftConnected = snapshot.aircraftConnected,
                rcConnected = snapshot.rcConnected,
                flightState = snapshot.state,
                flying = flying,
                onGround = !flying,
                xEast = snapshot.x,
                yNorth = snapshot.y,
                zUp = snapshot.z,
                vxEast = snapshot.vx,
                vyNorth = snapshot.vy,
                vzUp = snapshot.vz,
                yawDeg = snapshot.yawDeg,
            )
        }

        fun linkFacts(state: LinkState): LinkFacts = LinkFacts(
            joined = state.joined,
            estop = state.estop,
            // Only RelayLink's verified control-heartbeat timestamp feeds the flight deadman.
            lastRelayActivityMs = state.lastRelayActivityMs,
            controlAuthorityGranted = state.readiness.controlAuthority,
            settings = state.nodeSettings?.let { FlightSettings(it.virtualStickHz, it.watchdogHoldMs, it.watchdogFailsafeMs) },
        )
    }
}
