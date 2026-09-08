package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.sqrt
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NavigationPose
import org.worldofhacks.sweep.bridge.core.frames.NavigationRouteAuthorization
import org.worldofhacks.sweep.bridge.core.frames.NavigationSegment
import org.worldofhacks.sweep.bridge.core.watchdog.Watchdog
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogConfig
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogState

/**
 * The Virtual Stick control loop of Phase E (issue #43), as a pure, tick-driven state
 * machine: every input arrives through a method call and every output leaves through the
 * [FlightPort] and the per-command [ReportSink], so the whole thing runs against an injected
 * clock in JVM tests. The caller confines it to one thread and calls [tick] at the stick
 * cadence; the bridge-node `FlightExecutor` is that caller on the phone.
 *
 * Safety behaviour, in the order it is checked on every tick:
 * 1. Deadman. The loop keeps its own [Watchdog] on the relay-distributed thresholds, fed
 *    only by the verified control-heartbeat timestamp it sees through [updateLink]. It runs
 *    independently of the relay link object, so tearing the link down cannot stop the
 *    protection. Hold decays the stream to neutral sticks and fails the active command with
 *    `watchdog_hold`; failsafe commands auto-landing (indoors: land, never return to home)
 *    and fails with `watchdog_failsafe`. Neutral sticks keep flowing while Virtual Stick is
 *    enabled, so the stream never stops silently. Nothing streams without it: a Virtual Stick
 *    enable or a bench hold is refused while the deadman is disarmed, holding, or in failsafe,
 *    and a hold that interrupted a node takeoff is remembered so the failsafe still lands it.
 *    A new command is never activity: only the caller's verified-heartbeat input can move
 *    the watchdog from hold back to armed and reopen motion admission.
 *    The deadman lands only what the node was flying: with the loop idle and Virtual Stick
 *    off the aircraft is already under the flight controller and the RC operator, and after
 *    an RC takeover the node never commands a landing underneath the pilot.
 * 2. Authority. Losing the aircraft or the RC cancels everything with `authority_lost` and
 *    releases Virtual Stick on the aircraft. An RC takeover ([onTakeover]: stick deflection,
 *    pause, mode switch, or the flight controller dropping Virtual Stick) cancels with
 *    `authority_lost` and latches until the pilot calls [rearmAuthority] on screen; the latch
 *    is what the node reports as `control_authority`. The port forwards every stick
 *    deflection past its threshold; this loop is the only judge of whether there is anything
 *    to cancel (idle input is the pilot flying, latched input is already the pilot's).
 *    The pilot's Control authority toggle ([LinkFacts.controlAuthorityGranted], the
 *    readiness `control_authority` as the pilot set it) is enforced here as well as by the
 *    relay: while it is off, `takeoff`, `goto`, and `rotate_to` from the wire are refused
 *    with `authority_lost`, so a command that reaches the phone anyway never becomes a
 *    takeoff or a stick frame; `hover`, `land`, and `estop` keep their fail-safe handling
 *    and the bench procedures are the pilot's own.
 * 3. Network stop. The relay's authoritative `estop` flag is level-triggered: on every tick
 *    while it is asserted any running motion is cut to neutral sticks (`estop_asserted`),
 *    including motion admitted before the flag arrived or whose Virtual Stick enable answered
 *    after it, and new motion is refused. If the flag stays asserted for
 *    [FlightConfig.estopLandAfterMs] while airborne the node lands (PRD 5.5: hold, then land
 *    if held) unless the RC has the aircraft after a takeover. The `estop` command does the
 *    same hover and shares the latch.
 * 4. The command in flight, then one stick frame if Virtual Stick is enabled.
 *
 * Virtual Stick is enabled only while a command that needs it is active and disabled as soon
 * as it completes, so an idle aircraft is always under the flight controller and the RC; if
 * the SDK reports it enabled for the node while the loop is idle, the loop disables it.
 */
class FlightController(
    private val port: FlightPort,
    private val clock: Clock,
    val config: FlightConfig = FlightConfig(),
    private val monotonicNowMs: () -> Long = clock::nowMs,
    private val log: (String) -> Unit = {},
) {
    private sealed interface Phase {
        data object Idle : Phase

        data class Enabling(val sinceMs: Long) : Phase

        data class Releasing(val sinceMs: Long) : Phase

        data class Running(val steps: List<MotionStep>, val index: Int, val startedMs: Long, val yawSettledSinceMs: Long?) : Phase

        data class Settling(val untilMs: Long, val detail: String) : Phase

        data class TakingOff(val startedMs: Long, val targetZM: Double, val hoverSinceMs: Long?) : Phase

        data class SupervisedClimb(val targetZM: Double, val settledSinceMs: Long?) : Phase

        data class Landing(val startedMs: Long, val reason: String, val attempts: Int, val awaitingResult: Boolean, val retryAtMs: Long?) : Phase

        data class Holding(val sinceMs: Long) : Phase

        data class Bench(val frame: StickFrame, val untilMs: Long, val label: String) : Phase

        data object Navigating : Phase

        data class NavigationHolding(val sinceMs: Long, val detail: String) : Phase
    }

    private sealed interface NavigationCheck {
        data class Ready(val arrived: Boolean, val velocity: BodyVelocity) : NavigationCheck

        data class Invalid(val reason: FlightReason, val detail: String) : NavigationCheck
    }

    private class Active(val command: FlightCommand, val sink: ReportSink, val startedMs: Long, val startDetail: String) {
        var executingSent = false
        var lastProgressMs = 0L

        fun executingNow(detail: String) {
            if (executingSent) return
            executingSent = true
            sink.executing(detail)
        }
    }

    var mapping: AxisMapping = config.mapping

    /** Called on the loop thread whenever the observable status changes. */
    var onStatus: ((FlightStatus) -> Unit)? = null

    /** Called on the loop thread for every stick frame sent; the bench recorder hangs here. */
    var onStickSent: ((seq: Long, frame: StickFrame, nowMs: Long) -> Unit)? = null

    private var facts = AircraftFacts()
    private var link = LinkFacts()
    private var navigation = NavigationEvidence()
    private var lastNavigationVelocity = BodyVelocity.ZERO
    private var lastNavigationVelocityAtMs: Long? = null
    private var settings: FlightSettings? = null
    private var watchdog: Watchdog? = null
    private var lastRelayActivityMs: Long? = null
    private var phase: Phase = Phase.Idle
    private var generation = 0L
    private var active: Active? = null
    private var vsEnabled = false
    private var observedVirtualStickEnabled: Boolean? = null
    private var observedVirtualStickOwnedBySdk = false
    private var observedVirtualStickOwner = "UNKNOWN"
    private var authorityLost: String? = null
    private var groundedAuthorityQualified = false
    private var pilotInputNoted = false
    private var estopLatched = false
    private var estopSinceMs = 0L
    private var estopLandStarted = false
    /** Set when a hold interrupted a command the node was flying, so the failsafe still lands what the node flew. */
    private var flownIntoHold = false
    private var landingReason: String? = null
    private var supervisedFlightTargetZM: Double? = null
    private var supervisedFlightAirborne = false
    private var supervisedTakeoffStopIssued = false
    private var stickSeq = 0L
    private val rate = RateMeter()
    private var lastFrame: StickFrame? = null
    private var lastEvent: String? = null
    private var failsafeSetting: String? = null
    private var published = FlightStatus()

    val status: FlightStatus
        get() = snapshot(clock.nowMs())

    val cadence: StickCadence
        get() = StickCadence(settings?.stickHz ?: config.defaultStickHz)

    val virtualStickEnabled: Boolean
        get() = vsEnabled

    val virtualStickMayBeEnabled: Boolean
        get() = vsEnabled || phase is Phase.Enabling

    val watchdogState: WatchdogState
        get() = watchdog?.state ?: WatchdogState.DISARMED

    /** The flight-controller failsafe setting the port read (documented, never changed by the node). */
    fun reportFailsafeSetting(value: String) {
        failsafeSetting = value
        event("flight controller failsafe setting read: $value (the node never changes it; indoors the node lands on its own deadman)")
    }

    // ---- inputs ----

    fun updateAircraft(next: AircraftFacts) {
        facts = next
    }

    fun updateLink(next: LinkFacts) {
        val previous = link
        link = next
        next.settings?.let { applySettings(it) }
        val activity = next.lastRelayActivityMs
        if (activity != null && activity != lastRelayActivityMs) {
            lastRelayActivityMs = activity
            watchdog?.heartbeat()
        }
        val dog = watchdog
        if (dog != null && next.joined && (!previous.joined || dog.state == WatchdogState.DISARMED)) {
            dog.arm()
            flownIntoHold = false
            event("joined the relay; deadman armed (hold ${dog.config.holdMs} ms, failsafe ${dog.config.failsafeMs} ms)")
        }
    }

    fun updateNavigation(next: NavigationEvidence) {
        navigation = next
    }

    private fun applySettings(next: FlightSettings) {
        if (next == settings) return
        settings = next
        val previous = watchdog
        val dog = Watchdog(WatchdogConfig(next.holdMs, next.failsafeMs), clock)
        if (previous != null && previous.state != WatchdogState.DISARMED) dog.arm()
        watchdog = dog
        event("relay thresholds: stick ${next.clampedStickHz} Hz (requested ${next.stickHz}), hold ${next.holdMs} ms, failsafe ${next.failsafeMs} ms")
    }

    fun onTakeover(reason: String, detail: String?) {
        if (phase == Phase.Idle && active == null && !vsEnabled) {
            // The port forwards every deflection past its threshold, so idle input arrives at
            // the stick update rate while the pilot flies: note it once per idle stretch, and
            // not at all once the latch already says the pilot has the aircraft.
            if (authorityLost == null && !pilotInputNoted) {
                pilotInputNoted = true
                event("rc input while idle ($reason): the pilot has control; nothing to cancel")
            }
            return
        }
        event("RC takeover ($reason${detail?.let { ": $it" } ?: ""}): loop cancelled, virtual stick released")
        failActive(FlightReason.AUTHORITY_LOST, "$reason${detail?.let { ": $it" } ?: ""}; re-arm control authority on the flight card")
        authorityLost = reason
        groundedAuthorityQualified = false
        flownIntoHold = false
        supervisedFlightTargetZM = null
        supervisedFlightAirborne = false
        supervisedTakeoffStopIssued = false
        releaseVirtualStick()
        landingReason = null
    }

    /**
     * The SDK's own view of Virtual Stick: losing it while the loop believes it is on is a
     * takeover; finding it enabled for the node while the loop is idle (a link blip or an app
     * restart mid-command left the flight controller waiting for frames nobody sends) is
     * cleared at once so the aircraft goes back to the flight controller and the RC.
     */
    fun onVirtualStickState(enabled: Boolean, ownedBySdk: Boolean, owner: String) {
        observedVirtualStickEnabled = enabled
        observedVirtualStickOwnedBySdk = ownedBySdk
        observedVirtualStickOwner = owner
        if (vsEnabled && phase !is Phase.Enabling && (!enabled || !ownedBySdk)) {
            onTakeover("virtual_stick_dropped", if (enabled) "flight control authority is $owner" else "flight controller disabled virtual stick")
            return
        }
        if (!vsEnabled && enabled && ownedBySdk && phase == Phase.Idle) {
            event("virtual stick found enabled while the loop is idle ($owner): disabling")
            port.disableVirtualStick { result -> if (result is PortResult.Failed) log("virtual stick disable while idle failed: ${result.detail}") }
        }
    }

    fun rearmAuthority() {
        if (authorityLost == null) return
        event("control authority re-armed by the pilot (was ${authorityLost})")
        authorityLost = null
    }

    // ---- commands ----

    fun execute(command: FlightCommand, sink: ReportSink) {
        val now = clock.nowMs()
        authorityLost?.let {
            fail(sink, FlightReason.AUTHORITY_LOST, "control authority not re-armed after $it; re-arm on the flight card")
            return
        }
        // The relay refuses motion while readiness says control_authority=false; the node
        // refuses it too, so nothing that slips past the relay (or races the toggle) flies.
        if (command.relayMotion && !link.controlAuthorityGranted) {
            fail(sink, FlightReason.AUTHORITY_LOST, "control authority not granted: the pilot's Control authority toggle on the Readiness card is off; ${command.operation} refused")
            return
        }
        if (!facts.linked) {
            fail(sink, FlightReason.AIRCRAFT_UNAVAILABLE, if (!facts.aircraftConnected) "aircraft is not connected" else "remote controller is not connected")
            return
        }
        if (watchdogState == WatchdogState.FAILSAFE) {
            fail(sink, FlightReason.WATCHDOG_FAILSAFE, "deadman is in failsafe after relay silence; it re-arms on the next join")
            return
        }
        when (val args = command.args) {
            CommandArgs.Estop -> estop(command, sink, now)
            CommandArgs.Hover -> hover(command, sink, now)
            CommandArgs.Land -> land(command, sink, now)
            is CommandArgs.Takeoff -> takeoff(command, args, sink, now)
            is CommandArgs.Goto -> goto(command, args, sink, now)
            is CommandArgs.RotateTo -> rotateTo(command, args, sink, now)
            else -> fail(sink, FlightReason.UNSUPPORTED, "${command.operation} is not a flight operation")
        }
    }

    private fun estop(command: FlightCommand, sink: ReportSink, now: Long) {
        if (!estopLatched) {
            estopLatched = true
            estopSinceMs = now
            estopLandStarted = false
        }
        event("estop: neutral sticks and hover at once; landing if the relay keeps the stop asserted for ${config.estopLandAfterMs} ms")
        holdNow(command, sink, now, "estop")
    }

    private fun hover(command: FlightCommand, sink: ReportSink, now: Long) = holdNow(command, sink, now, "hover")

    private fun holdNow(command: FlightCommand, sink: ReportSink, now: Long, word: String) {
        when (val current = phase) {
            // A landing already underway is the safest thing the aircraft can be doing; keep its command.
            is Phase.Landing -> {
                sink.executing("landing already in progress (${current.reason}); $word holds")
                sink.completed("landing continues under the flight controller")
                return
            }
            // Auto takeoff ends in a hover by itself and enabling Virtual Stick mid-takeoff is
            // what the flight controller interrupts (prior-art notes on #43): let it finish.
            is Phase.TakingOff -> {
                preempt(word)
                transition(Phase.Idle)
                sink.executing("auto takeoff in progress; the flight controller hovers when it completes")
                sink.completed("takeoff continues under the flight controller; no sticks sent")
                return
            }
            else -> preempt(word)
        }
        if (facts.onGround) {
            sink.executing("on the ground (${facts.flightState}): nothing to $word")
            sink.completed("on the ground; no sticks sent")
            if (virtualStickMayBeEnabled) releaseVirtualStick()
            return
        }
        // HOLD is already the safety action. If Virtual Stick was not active when the
        // deadman tripped, leave control with the flight controller and the RC instead of
        // enabling a new stick stream without a fresh verified control heartbeat.
        if (watchdogState == WatchdogState.HOLD && !vsEnabled) {
            sink.executing("deadman hold is active; $word keeps the aircraft under the flight controller and the RC")
            sink.completed("watchdog hold maintained; no virtual stick enabled")
            return
        }
        active = Active(command, sink, now, "neutral sticks: hovering (${facts.flightState})")
        beginVirtualStick(now) {
            transition(Phase.Settling(clock.nowMs() + config.settleMs, "hover held for ${config.settleMs} ms"))
        }
    }

    private fun land(command: FlightCommand, sink: ReportSink, now: Long) {
        preempt("land")
        if (facts.onGround) {
            sink.executing("already on the ground (${facts.flightState})")
            sink.completed("on the ground; no landing needed")
            if (virtualStickMayBeEnabled) releaseVirtualStick()
            return
        }
        val current = phase
        if (current is Phase.Landing) {
            val joined = Active(command, sink, now, "landing")
            active = joined
            joined.executingNow("joining the landing already in progress (${current.reason})")
            return
        }
        active = Active(command, sink, now, "auto-landing")
        startLanding(now, "land_command")
    }

    private fun takeoff(command: FlightCommand, args: CommandArgs.Takeoff, sink: ReportSink, now: Long) {
        if (!motionAllowed(sink)) return
        if (facts.flying) {
            fail(sink, FlightReason.ALREADY_AIRBORNE, "aircraft is ${facts.flightState}; no takeoff while airborne")
            return
        }
        val targetZ = args.zMm / 1000.0
        config.supervisedVertical?.let { supervised ->
            val softCeiling = minOf(supervised.softCeilingM, supervised.hardCeilingM)
            if (targetZ > softCeiling || targetZ >= supervised.hardCeilingM) {
                fail(
                    sink,
                    FlightReason.VERTICAL_CEILING_EXCEEDED,
                    "takeoff target ${format(targetZ)} m exceeds the local soft ceiling ${format(softCeiling)} m",
                )
                return
            }
            val height = freshLocalHeight(supervised)
            if (height == null) {
                fail(
                    sink,
                    FlightReason.LOCAL_HEIGHT_UNAVAILABLE,
                    "takeoff requires a finite KeyAltitude callback no older than ${supervised.maximumHeightAgeMs} ms",
                )
                return
            }
            if (height >= supervised.hardCeilingM) {
                fail(
                    sink,
                    FlightReason.VERTICAL_CEILING_EXCEEDED,
                    "local height ${format(height)} m is already at the hard ceiling ${format(supervised.hardCeilingM)} m",
                )
                return
            }
            if (height >= targetZ) {
                fail(
                    sink,
                    FlightReason.VERTICAL_CEILING_EXCEEDED,
                    "local height ${format(height)} m is not below the signed takeoff target ${format(targetZ)} m",
                )
                return
            }
        }
        active = Active(command, sink, now, "auto takeoff")
        if (config.supervisedVertical != null) {
            supervisedFlightTargetZM = targetZ
            supervisedFlightAirborne = false
            supervisedTakeoffStopIssued = false
        }
        transition(Phase.TakingOff(now, targetZ, null))
        val gen = generation
        port.startTakeoff { result ->
            if (gen != generation) return@startTakeoff
            when (result) {
                PortResult.Ok -> active?.executingNow("auto takeoff started; target z ${format(targetZ)} m, completes on the reported flight state")
                is PortResult.Failed -> {
                    supervisedFlightTargetZM = null
                    supervisedFlightAirborne = false
                    supervisedTakeoffStopIssued = false
                    failActive(FlightReason.TAKEOFF_FAILED, "takeoff action refused: ${result.detail}")
                    transition(Phase.Idle)
                }
            }
        }
    }

    private fun goto(command: FlightCommand, args: CommandArgs.Goto, sink: ReportSink, now: Long) {
        if (config.supervisedVertical != null) {
            fail(sink, FlightReason.UNSUPPORTED, "goto is disabled by the supervised vertical profile")
            return
        }
        if (!motionAllowed(sink)) return
        if (!facts.flying) {
            fail(sink, FlightReason.NOT_AIRBORNE, "aircraft is ${facts.flightState}; goto needs a hovering aircraft")
            return
        }
        if (args.navigationRouteId != null) {
            when (val route = navigationCheck(command, args, now)) {
                is NavigationCheck.Invalid -> {
                    fail(sink, route.reason, route.detail)
                    return
                }
                is NavigationCheck.Ready -> {
                    if (route.arrived) {
                        sink.executing("within the signed route arrival tolerance")
                        sink.completed("route arrival confirmed by signed pose")
                        return
                    }
                    active = Active(command, sink, now, "signed route ${args.navigationRouteId}: tracking mapped pose")
                    beginVirtualStick(now) { transition(Phase.Navigating) }
                    return
                }
            }
        }
        val step = MotionPlanner.goto(args, facts, config.limits, config.minDisplacementM)
        if (step == null) {
            sink.executing("already within ${format(config.minDisplacementM)} m of the target")
            sink.completed("no displacement needed")
            return
        }
        active = Active(command, sink, now, describe(step, 1, 1))
        beginVirtualStick(now) { transition(Phase.Running(listOf(step), 0, clock.nowMs(), null)) }
    }

    private fun rotateTo(command: FlightCommand, args: CommandArgs.RotateTo, sink: ReportSink, now: Long) {
        if (config.supervisedVertical != null) {
            fail(sink, FlightReason.UNSUPPORTED, "rotate_to is disabled by the supervised vertical profile")
            return
        }
        if (!motionAllowed(sink)) return
        if (!facts.flying) {
            fail(sink, FlightReason.NOT_AIRBORNE, "aircraft is ${facts.flightState}; rotate_to needs a hovering aircraft")
            return
        }
        val step = MotionPlanner.rotateTo(args, facts, config.limits, config.yawMarginMs)
        if (abs(step.deltaDeg) <= config.yawToleranceDeg) {
            sink.executing("heading ${format(facts.yawDeg)} is within ${format(config.yawToleranceDeg)} deg of ${format(step.targetDeg)}")
            sink.completed("no rotation needed")
            return
        }
        active = Active(command, sink, now, describe(step, 1, 1))
        beginVirtualStick(now) { transition(Phase.Running(listOf(step), 0, clock.nowMs(), null)) }
    }

    private fun motionAllowed(sink: ReportSink): Boolean {
        if (watchdogState == WatchdogState.HOLD) {
            fail(sink, FlightReason.WATCHDOG_HOLD, "deadman is holding after relay silence; a fresh verified control heartbeat must re-arm it before motion")
            return false
        }
        val current = active
        if (current != null) {
            fail(sink, FlightReason.NODE_BUSY, "${current.command.operation} ${current.command.commandId} is still active")
            return false
        }
        if (phase is Phase.Landing) {
            fail(sink, FlightReason.LANDING_IN_PROGRESS, "landing in progress (${landingReason ?: "unknown reason"})")
            return false
        }
        // The relay's live flag, not the latch the tick sets: a motion posted between the flag
        // arriving and the next tick must be refused too.
        if (link.estop) {
            fail(sink, FlightReason.ESTOP_ASSERTED, "relay network stop is asserted; only hover, land, and estop are accepted")
            return false
        }
        return true
    }

    // ---- bench procedures (issue #85), run from the screen with the RC operator present ----

    /**
     * Holds one raw SDK-field frame for [durationMs] under the same deadman, authority, and
     * estop protections as a command. The axis probe sends a pure `pitch` or `roll` frame;
     * the deadman and takeover drills hold neutral sticks while the operator kills the link
     * or moves a stick.
     */
    fun startBench(label: String, frame: StickFrame, durationMs: Long, sink: ReportSink): Boolean {
        val supervised = config.supervisedVertical
        val now = clock.nowMs()
        val command = FlightCommand("bench-$label-$now", CommandArgs.Hover, label)
        authorityLost?.let {
            fail(sink, FlightReason.AUTHORITY_LOST, "control authority not re-armed after $it")
            return false
        }
        if (!facts.linked) {
            fail(sink, FlightReason.AIRCRAFT_UNAVAILABLE, "aircraft and RC must both be connected")
            return false
        }
        if (supervised != null && (!groundedAuthorityQualified || !link.controlAuthorityGranted)) {
            fail(sink, FlightReason.AUTHORITY_LOST, "supervised bench motion requires grounded authority qualification and the pilot's Control authority toggle")
            return false
        }
        when (watchdogState) {
            WatchdogState.DISARMED -> {
                fail(sink, FlightReason.WATCHDOG_DISARMED, "deadman not armed: bench holds stream sticks only under the relay's watchdog thresholds; connect and join the relay first")
                return false
            }
            WatchdogState.HOLD -> {
                fail(sink, FlightReason.WATCHDOG_HOLD, "deadman is holding after relay silence; a fresh verified control heartbeat must re-arm it before bench motion")
                return false
            }
            WatchdogState.FAILSAFE -> {
                fail(sink, FlightReason.WATCHDOG_FAILSAFE, "deadman is in failsafe after relay silence; it re-arms on the next join")
                return false
            }
            else -> Unit
        }
        if (!motionAllowed(sink)) return false
        if (!facts.flying) {
            fail(sink, FlightReason.NOT_AIRBORNE, "bench procedures start from a guarded hover")
            return false
        }
        if (supervised != null && facts.flightState != HOVERING) {
            fail(sink, FlightReason.NOT_AIRBORNE, "supervised bench motion requires a guarded hover, not ${facts.flightState}")
            return false
        }
        if (supervised != null && !supervisedDirectionalBenchAllowed(frame, durationMs, supervised, sink)) return false
        val body = mapping.toBody(frame)
        if (!config.limits.within(body)) {
            fail(sink, FlightReason.UNSUPPORTED, "bench frame exceeds the flight limits")
            return false
        }
        active = Active(command, sink, now, "bench $label: holding ${frameWord(frame)} for $durationMs ms")
        beginVirtualStick(now) { transition(Phase.Bench(frame, clock.nowMs() + durationMs, label)) }
        return true
    }

    private fun supervisedDirectionalBenchAllowed(
        frame: StickFrame,
        durationMs: Long,
        supervised: SupervisedVerticalConfig,
        sink: ReportSink,
    ): Boolean {
        if (durationMs !in 1..SUPERVISED_BENCH_MAX_DURATION_MS ||
            listOf(frame.pitch, frame.roll, frame.yaw, frame.verticalThrottle).any { !it.isFinite() } ||
            frame.yawMode != YawMode.ANGULAR_VELOCITY || frame.yaw != 0.0 || frame.verticalThrottle != 0.0
        ) {
            fail(sink, FlightReason.UNSUPPORTED, "supervised bench motion requires a finite, horizontal body-axis hold no longer than $SUPERVISED_BENCH_MAX_DURATION_MS ms")
            return false
        }
        val body = mapping.toBody(frame)
        val singleAxis = (body.forwardMS == 0.0) != (body.rightMS == 0.0)
        if (!singleAxis || maxOf(kotlin.math.abs(body.forwardMS), kotlin.math.abs(body.rightMS)) > SUPERVISED_BENCH_MAX_SPEED_MS) {
            fail(sink, FlightReason.UNSUPPORTED, "supervised bench motion permits one signed body axis at no more than $SUPERVISED_BENCH_MAX_SPEED_MS m/s")
            return false
        }
        val height = freshLocalHeight(supervised)
        if (height == null) {
            fail(sink, FlightReason.LOCAL_HEIGHT_UNAVAILABLE, "supervised bench motion requires a fresh finite KeyAltitude callback")
            return false
        }
        if (height < SUPERVISED_BENCH_MIN_HEIGHT_M) {
            fail(sink, FlightReason.NOT_AIRBORNE, "supervised bench motion requires local height at least ${format(SUPERVISED_BENCH_MIN_HEIGHT_M)} m")
            return false
        }
        if (height >= supervised.hardCeilingM) {
            fail(sink, FlightReason.VERTICAL_CEILING_EXCEEDED, "local height ${format(height)} m is at the hard ceiling ${format(supervised.hardCeilingM)} m")
            return false
        }
        return true
    }

    fun qualifyGroundedAuthority(sink: ReportSink) {
        groundedAuthorityQualified = false
        val now = clock.nowMs()
        authorityLost?.let {
            fail(sink, FlightReason.AUTHORITY_LOST, "control authority not re-armed after $it")
            return
        }
        if (!facts.linked) {
            fail(sink, FlightReason.AIRCRAFT_UNAVAILABLE, "aircraft and RC must both be connected")
            return
        }
        if (!facts.onGround) {
            fail(sink, FlightReason.ALREADY_AIRBORNE, "grounded authority qualification is unavailable while ${facts.flightState}")
            return
        }
        if (active != null || phase != Phase.Idle || vsEnabled) {
            fail(sink, FlightReason.NODE_BUSY, "the flight loop is ${phaseName(phase)}; wait for it to become idle")
            return
        }
        active = Active(
            FlightCommand("grounded-authority-$now", CommandArgs.Hover, "grounded authority qualification"),
            sink,
            now,
            "grounded virtual stick mode contract qualification",
        )
        beginVirtualStick(now) {
            val qualification = active
            qualification?.executingNow("virtual stick mode contract verified while grounded; releasing Virtual Stick")
            releaseVirtualStick(awaitResult = true) { result ->
                if (active !== qualification || phase !is Phase.Releasing) return@releaseVirtualStick
                when (result) {
                    PortResult.Ok -> {
                        groundedAuthorityQualified = true
                        completeActive("virtual stick mode contract verified while grounded; Virtual Stick disable completed")
                        event("grounded authority qualification completed: virtual stick mode contract verified, Virtual Stick disable completed")
                        transition(Phase.Idle)
                    }
                    is PortResult.Failed -> {
                        authorityLost = "virtual_stick_cleanup_failed"
                        failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick mode contract was verified but Virtual Stick cleanup failed: ${result.detail}")
                        event("grounded authority qualification cleanup failed: ${result.detail}")
                        transition(Phase.Idle)
                    }
                }
            }
        }
    }

    /** Ends a bench hold early; the command completes as "stopped by the operator". */
    fun stopBench() {
        val current = phase
        if (current !is Phase.Bench) return
        completeActive("bench ${current.label} stopped by the operator")
        releaseVirtualStick()
    }

    /** Takeoff and land from the bench card use the command path with a logging sink. */
    fun benchTakeoff(zMm: Long, sink: ReportSink) {
        if (config.supervisedVertical != null) {
            fail(sink, FlightReason.UNSUPPORTED, "bench takeoff is disabled by the supervised vertical profile")
            return
        }
        execute(FlightCommand("bench-takeoff-${clock.nowMs()}", CommandArgs.Takeoff(zMm), "bench takeoff"), sink)
    }

    fun benchLand(sink: ReportSink) = execute(FlightCommand("bench-land-${clock.nowMs()}", CommandArgs.Land, "bench land"), sink)

    // ---- the tick ----

    fun tick(nowMs: Long) {
        port.advance(nowMs)
        pollDeadman(nowMs)
        checkLink()
        checkEstop(nowMs)
        checkSupervisedFlight(nowMs)
        checkNavigation(nowMs)
        advancePhase(nowMs)
        streamSticks(nowMs)
        publish(nowMs)
    }

    private fun pollDeadman(now: Long) {
        val transition = watchdog?.poll() ?: return
        when (transition.to) {
            WatchdogState.HOLD -> enterHold(now, transition.elapsedMs)
            WatchdogState.FAILSAFE -> enterFailsafe(now, transition.elapsedMs)
            WatchdogState.ARMED -> {
                flownIntoHold = false
                if (phase is Phase.Holding) {
                    event("authorized control heartbeat resumed after hold; virtual stick released")
                    releaseVirtualStick()
                }
            }
            WatchdogState.DISARMED -> Unit
        }
    }

    private fun enterHold(now: Long, elapsedMs: Long) {
        val detail = "no authorized control heartbeat for $elapsedMs ms (hold threshold ${settings?.holdMs} ms): sticks neutral until the link recovers"
        event("watchdog hold: $detail")
        if (phase is Phase.Landing) return
        // A takeoff, or a Virtual Stick enable still pending, ends here with the loop idle and
        // Virtual Stick off: remember that the node was flying so the failsafe still lands it.
        if (active != null || phase !is Phase.Idle) flownIntoHold = true
        failActive(FlightReason.WATCHDOG_HOLD, detail)
        if (phase is Phase.Enabling || vsEnabled) {
            releaseVirtualStick()
            transition(Phase.Holding(now))
        } else {
            transition(Phase.Idle)
        }
    }

    private fun enterFailsafe(now: Long, elapsedMs: Long) {
        val detail = "no authorized control heartbeat for $elapsedMs ms (failsafe threshold ${settings?.failsafeMs} ms): auto-landing now, never return to home"
        event("watchdog failsafe: $detail")
        if (phase is Phase.Landing) return
        // Decided before failActive clears the command: the node lands only what it was flying.
        val flownByNode = vsEnabled || active != null || phase !is Phase.Idle || flownIntoHold
        flownIntoHold = false
        failActive(FlightReason.WATCHDOG_FAILSAFE, detail)
        val lost = authorityLost
        when {
            lost != null -> {
                event("failsafe: the RC has the aircraft ($lost); no landing commanded")
                transition(Phase.Idle)
            }
            facts.onGround -> if (virtualStickMayBeEnabled) releaseVirtualStick() else transition(Phase.Idle)
            !flownByNode -> {
                event("failsafe: the loop was idle with virtual stick off; the aircraft stays with the flight controller and the RC operator, no landing commanded")
                transition(Phase.Idle)
            }
            else -> startLanding(now, "watchdog_failsafe")
        }
    }

    private fun checkLink() {
        if (facts.linked) return
        groundedAuthorityQualified = false
        val reason = if (!facts.aircraftConnected) "aircraft_disconnected" else "rc_disconnected"
        if (supervisedFlightTargetZM != null && !facts.onGround && authorityLost == null) {
            if (phase !is Phase.Landing) {
                stopSupervisedFlight(
                    FlightReason.AUTHORITY_LOST,
                    "$reason during supervised vertical flight; landing",
                    clock.nowMs(),
                )
            }
            return
        }
        if (phase == Phase.Idle && active == null && !vsEnabled) return
        event("authority lost: $reason; loop cancelled, physical RC remains primary")
        failActive(FlightReason.AUTHORITY_LOST, "$reason: the loop cancelled; physical RC remains primary")
        releaseVirtualStick()
        landingReason = null
    }

    private fun checkEstop(now: Long) {
        if (link.estop) {
            if (!estopLatched) {
                estopLatched = true
                estopSinceMs = now
                estopLandStarted = false
                event("network stop asserted by the relay: motion cut to neutral sticks")
            }
            // Level-triggered, not edge-triggered: a motion admitted before the flag arrived, or
            // whose Virtual Stick enable answered after it, reaches Running under an asserted
            // stop and is cut on the first tick that sees it, before this tick's frame goes out.
            when (phase) {
                is Phase.Enabling -> {
                    failActive(FlightReason.ESTOP_ASSERTED, "relay network stop asserted while virtual stick was enabling")
                    releaseVirtualStick()
                }
                is Phase.Running, is Phase.SupervisedClimb, is Phase.Navigating, is Phase.NavigationHolding, is Phase.Bench -> {
                    failActive(FlightReason.ESTOP_ASSERTED, "relay network stop asserted: sticks neutral, hovering")
                    transition(Phase.Settling(now + config.settleMs, "network stop hover"))
                }
                is Phase.TakingOff -> {
                    if (supervisedFlightTargetZM != null) {
                        stopSupervisedFlight(
                            FlightReason.ESTOP_ASSERTED,
                            "relay network stop asserted during supervised takeoff",
                            now,
                        )
                    } else {
                        failActive(FlightReason.ESTOP_ASSERTED, "relay network stop asserted during takeoff; the aircraft finishes the takeoff under the flight controller")
                        transition(Phase.Idle)
                    }
                }
                else -> Unit
            }
            if (!estopLandStarted && !facts.onGround && phase !is Phase.Landing && now - estopSinceMs >= config.estopLandAfterMs) {
                estopLandStarted = true
                val lost = authorityLost
                if (lost != null) {
                    event("network stop held for ${now - estopSinceMs} ms but the RC has the aircraft ($lost): no landing commanded")
                } else {
                    event("network stop held for ${now - estopSinceMs} ms: landing (PRD 5.5 hold, then land if held)")
                    failActive(FlightReason.ESTOP_ASSERTED, "network stop held; landing")
                    startLanding(now, "estop_held")
                }
            }
        } else if (estopLatched) {
            estopLatched = false
            estopLandStarted = false
            event("network stop released by the relay")
        }
    }

    private fun checkSupervisedFlight(now: Long) {
        if (supervisedFlightTargetZM == null) return
        val supervised = config.supervisedVertical ?: return
        if (facts.flying) supervisedFlightAirborne = true
        if (supervisedTakeoffStopIssued && facts.flying && phase !is Phase.Landing) {
            startLanding(now, "supervised_takeoff_cancelled")
            return
        }
        if (supervisedFlightAirborne && facts.onGround) {
            supervisedFlightTargetZM = null
            supervisedFlightAirborne = false
            supervisedTakeoffStopIssued = false
            return
        }
        if (authorityLost != null || phase is Phase.Landing) return
        guardVerticalHeight(supervised, now)
    }

    private fun advancePhase(now: Long) {
        when (val current = phase) {
            Phase.Idle, is Phase.Holding -> Unit
            is Phase.Enabling -> if (now - current.sinceMs > config.enableTimeoutMs) {
                failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick mode contract did not answer within ${config.enableTimeoutMs} ms")
                releaseVirtualStick()
            }
            is Phase.Releasing -> if (now - current.sinceMs > config.enableTimeoutMs) {
                authorityLost = "virtual_stick_cleanup_timeout"
                failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "Virtual Stick cleanup did not answer within ${config.enableTimeoutMs} ms")
                event("grounded authority qualification cleanup timed out")
                transition(Phase.Idle)
            }
            is Phase.Running -> advanceRunning(current, now)
            Phase.Navigating -> advanceNavigation(now)
            is Phase.NavigationHolding -> if (now - current.sinceMs >= navigationLossLandAfterMs()) {
                event("navigation evidence remained unavailable for ${now - current.sinceMs} ms: landing")
                releaseVirtualStick()
                if (facts.flying) startLanding(now, "navigation_lost")
            }
            is Phase.Settling -> if (now >= current.untilMs) {
                completeActive("${current.detail}; measured speed ${format(facts.speedMS)} m/s")
                releaseVirtualStick()
            }
            is Phase.TakingOff -> advanceTakeoff(current, now)
            is Phase.SupervisedClimb -> advanceSupervisedClimb(current, now)
            is Phase.Landing -> advanceLanding(current, now)
            is Phase.Bench -> if (supervisedBenchHeightIsSafe(now)) {
                if (now >= current.untilMs) {
                    completeActive("bench ${current.label} held ${frameWord(current.frame)} for ${now - (active?.startedMs ?: now)} ms")
                    releaseVirtualStick()
                }
            }
        }
    }

    private fun checkNavigation(now: Long) {
        if (phase !is Phase.Navigating) return
        val current = active ?: return
        val args = current.command.args as? CommandArgs.Goto ?: return
        when (val check = navigationCheck(current.command, args, now)) {
            is NavigationCheck.Ready -> Unit
            is NavigationCheck.Invalid -> when (check.reason) {
                FlightReason.NAVIGATION_LAND -> {
                    failActive(check.reason, check.detail)
                    releaseVirtualStick()
                    if (facts.flying) startLanding(now, "navigation_land")
                }
                else -> {
                    failActive(check.reason, check.detail)
                    event("navigation hold: ${check.detail}")
                    transition(Phase.NavigationHolding(now, check.detail))
                }
            }
        }
    }

    private fun navigationCheck(command: FlightCommand, args: CommandArgs.Goto, now: Long): NavigationCheck {
        val local = config.navigation ?: return navigationInvalid("navigation is not configured on this node")
        val authorization = navigation.authorization ?: return navigationInvalid("signed route authorization is unavailable")
        val pose = navigation.pose ?: return navigationInvalid("signed navigation pose is unavailable")
        val relayOffset = navigation.relayOffsetMs ?: return navigationInvalid("relay clock offset is unavailable")
        val relayNow = now + relayOffset
        val routeId = args.navigationRouteId ?: return navigationInvalid("goto has no navigation route id")
        if (!routePinsMatch(authorization, local)) return navigationInvalid("signed route provenance does not match this node")
        if (authorization.commandId != command.commandId || authorization.routeId != routeId) {
            return navigationInvalid("signed route does not bind command ${command.commandId} and route $routeId")
        }
        if (listOf(args.xMm, args.yMm, args.zMm) != authorization.target() || args.speedMmS > authorization.maxSpeedMmS) {
            return navigationInvalid("goto target or speed does not match the signed route")
        }
        if (authorization.clockLeaseId != local.clockLeaseId || authorization.expiresAtMs > local.clockLeaseExpiresAtMs ||
            relayNow >= local.clockLeaseExpiresAtMs || authorization.expiresAtMs - authorization.t > local.authorizationLifetimeMs || authorization.expiresAtMs <= relayNow
        ) {
            return navigationLost("signed route authorization expired or exceeds the local lease bound")
        }
        if (!poseMatchesRoute(pose, authorization)) return navigationLost("signed navigation pose does not bind the active route")
        when (pose.status) {
            NavigationPose.Status.HOLD -> return NavigationCheck.Invalid(FlightReason.NAVIGATION_HOLD, "signed navigation pose requested hold")
            NavigationPose.Status.LAND -> return NavigationCheck.Invalid(FlightReason.NAVIGATION_LAND, "signed navigation pose requested landing")
            NavigationPose.Status.READY -> Unit
        }
        val poseTime = pose.poseTimeMs ?: return navigationLost("ready navigation pose omitted pose time")
        val fixTime = pose.fixTimeMs ?: return navigationLost("ready navigation pose omitted fix time")
        val freshUntil = navigation.poseFreshUntilMs ?: return navigationLost("navigation pose has no local freshness deadline")
        if (now >= freshUntil || !withinClockBudget(poseTime, relayNow, authorization) || !withinClockBudget(fixTime, relayNow, authorization)) {
            return navigationLost("signed navigation pose or fix is stale")
        }
        val current = active
        if (current != null && current.command == command && now - current.startedMs >= authorization.trackingTimeoutMs) {
            return navigationLost("signed route tracking deadline elapsed")
        }
        val x = pose.xMm ?: return navigationLost("ready navigation pose omitted x")
        val y = pose.yMm ?: return navigationLost("ready navigation pose omitted y")
        val z = pose.zMm ?: return navigationLost("ready navigation pose omitted z")
        val uncertaintyMm = pose.positionUncertaintyMm ?: return navigationLost("ready navigation pose omitted uncertainty")
        if (uncertaintyMm > authorization.maxPositionUncertaintyMm || uncertaintyMm / 1000.0 > local.maxPositionUncertaintyM) {
            return navigationLost("navigation position uncertainty exceeds the route bound")
        }
        if (facts.speedMS > authorization.maxSpeedMmS / 1000.0 + SPEED_EPSILON_MS) {
            return navigationLost("measured aircraft speed exceeds the signed route bound")
        }
        val uncertaintyM = uncertaintyMm / 1000.0
        val point = Triple(x / 1000.0, y / 1000.0, z / 1000.0)
        val segmentDistance = authorization.segments.minOf { distanceToSegment(point, it) }
        val allowedCrossTrack = minOf(authorization.maxCrossTrackMm, authorization.segments.maxOf { it.tubeRadiusMm }) / 1000.0
        if (segmentDistance + uncertaintyM > allowedCrossTrack) return navigationLost("navigation pose is outside the signed route corridor")
        val targetValues = authorization.target()
        val target = Triple(targetValues[0] / 1000.0, targetValues[1] / 1000.0, targetValues[2] / 1000.0)
        val east = target.first - point.first
        val north = target.second - point.second
        val up = target.third - point.third
        val horizontalDistance = hypot(east, north)
        val verticalDistance = abs(up)
        val arrived = local.isWithinArrival(horizontalDistance, verticalDistance, uncertaintyM) &&
            horizontalDistance + uncertaintyM <= authorization.arrivalHorizontalToleranceMm / 1000.0 &&
            verticalDistance + uncertaintyM <= authorization.arrivalVerticalToleranceMm / 1000.0
        if (arrived) return NavigationCheck.Ready(arrived = true, velocity = BodyVelocity.ZERO)
        val distance = sqrt(east * east + north * north + up * up)
        val stoppingDistance = facts.speedMS * facts.speedMS / (2.0 * (authorization.maxDecelerationMmS2 / 1000.0))
        val stoppingLimitedSpeed = sqrt(2.0 * (authorization.maxDecelerationMmS2 / 1000.0) * (distance - stoppingDistance).coerceAtLeast(0.0))
        val speed = minOf(args.speedMmS, authorization.maxSpeedMmS).toDouble() / 1000.0
        val scale = minOf(speed, stoppingLimitedSpeed) / distance
        val (forward, right) = GroundFrame.toBody(east * scale, north * scale, facts.yawDeg)
        val body = config.limits.clamp(BodyVelocity(forwardMS = forward, rightMS = right, upMS = up * scale))
        return NavigationCheck.Ready(arrived = false, velocity = body)
    }

    private fun routePinsMatch(route: NavigationRouteAuthorization, local: NavigationConfig): Boolean =
        route.navigationConfigId == local.navigationConfigId && route.navigationConfigSha256 == local.navigationConfigSha256 &&
            route.mapVersion == local.mapVersion && route.mapSha256 == local.mapSha256 && route.geometrySha256 == local.geometrySha256 &&
            route.cameraCalibrationSha256 == local.cameraCalibrationSha256 && route.bodyExtrinsicsSha256 == local.bodyExtrinsicsSha256 &&
            route.worldTransformSha256 == local.worldTransformSha256 && route.controlSourceIds == local.controlSourceIds

    private fun poseMatchesRoute(pose: NavigationPose, route: NavigationRouteAuthorization): Boolean =
        pose.commandId == route.commandId && pose.routeId == route.routeId && pose.connectionEpoch == route.connectionEpoch &&
            pose.clockLeaseId == route.clockLeaseId && pose.navigationConfigId == route.navigationConfigId &&
            pose.navigationConfigSha256 == route.navigationConfigSha256 && pose.mapVersion == route.mapVersion && pose.mapSha256 == route.mapSha256 &&
            pose.geometrySha256 == route.geometrySha256 && pose.cameraCalibrationSha256 == route.cameraCalibrationSha256 &&
            pose.bodyExtrinsicsSha256 == route.bodyExtrinsicsSha256 && pose.worldTransformSha256 == route.worldTransformSha256 &&
            pose.controlSourceIds == route.controlSourceIds

    private fun withinClockBudget(timeMs: Long, relayNowMs: Long, route: NavigationRouteAuthorization): Boolean =
        timeMs <= relayNowMs + route.maxClockErrorMs && relayNowMs - timeMs <= route.poseFreshnessMs + route.maxClockErrorMs

    private fun navigationFrame(now: Long): StickFrame {
        val command = active?.command ?: return StickFrame.NEUTRAL
        val args = command.args as? CommandArgs.Goto ?: return StickFrame.NEUTRAL
        val check = navigationCheck(command, args, now) as? NavigationCheck.Ready ?: return StickFrame.NEUTRAL
        if (check.arrived) {
            rememberNavigationVelocity(BodyVelocity.ZERO)
            return StickFrame.NEUTRAL
        }
        val route = navigation.authorization ?: return StickFrame.NEUTRAL
        return mapping.toFrame(limitNavigationAcceleration(check.velocity, route, now))
    }

    private fun limitNavigationAcceleration(target: BodyVelocity, route: NavigationRouteAuthorization, now: Long): BodyVelocity {
        val previousAt = lastNavigationVelocityAtMs
        val maximumChange = route.maxAccelerationMmS2 / 1000.0 * if (previousAt == null) 0.0 else (now - previousAt).coerceAtLeast(0) / 1000.0
        val previous = lastNavigationVelocity
        val horizontalDelta = hypot(target.forwardMS - previous.forwardMS, target.rightMS - previous.rightMS)
        val horizontalScale = if (horizontalDelta > maximumChange && horizontalDelta > 0.0) maximumChange / horizontalDelta else 1.0
        val next = config.limits.clamp(BodyVelocity(
            forwardMS = previous.forwardMS + (target.forwardMS - previous.forwardMS) * horizontalScale,
            rightMS = previous.rightMS + (target.rightMS - previous.rightMS) * horizontalScale,
            upMS = previous.upMS + (target.upMS - previous.upMS).coerceIn(-maximumChange, maximumChange),
        ))
        rememberNavigationVelocity(next)
        return next
    }

    private fun rememberNavigationVelocity(value: BodyVelocity) {
        lastNavigationVelocity = value
        lastNavigationVelocityAtMs = clock.nowMs()
    }

    private fun navigationInvalid(detail: String): NavigationCheck.Invalid = NavigationCheck.Invalid(FlightReason.NAVIGATION_NOT_AUTHORIZED, detail)
    private fun navigationLost(detail: String): NavigationCheck.Invalid = NavigationCheck.Invalid(FlightReason.NAVIGATION_LOST, detail)
    private fun navigationLossLandAfterMs(): Long = config.navigation?.lossLandAfterMs ?: 0

    private fun distanceToSegment(point: Triple<Double, Double, Double>, segment: NavigationSegment): Double =
        distanceToSegment(point, Triple(segment.startXMm / 1000.0, segment.startYMm / 1000.0, segment.startZMm / 1000.0), Triple(segment.endXMm / 1000.0, segment.endYMm / 1000.0, segment.endZMm / 1000.0))

    private fun distanceToSegment(point: Triple<Double, Double, Double>, start: Triple<Double, Double, Double>, target: Triple<Double, Double, Double>): Double {
        val dx = target.first - start.first
        val dy = target.second - start.second
        val dz = target.third - start.third
        val projection = ((point.first - start.first) * dx + (point.second - start.second) * dy + (point.third - start.third) * dz) / (dx * dx + dy * dy + dz * dz)
        val t = projection.coerceIn(0.0, 1.0)
        return sqrt((point.first - (start.first + dx * t)).let { it * it } + (point.second - (start.second + dy * t)).let { it * it } + (point.third - (start.third + dz * t)).let { it * it })
    }

    private fun advanceNavigation(now: Long) {
        val current = active ?: return
        val args = current.command.args as? CommandArgs.Goto ?: return
        when (val check = navigationCheck(current.command, args, now)) {
            is NavigationCheck.Invalid -> Unit
            is NavigationCheck.Ready -> if (check.arrived) {
                completeActive("route arrival confirmed by signed pose")
                releaseVirtualStick()
            } else progress(now, "signed route ${args.navigationRouteId}: tracking mapped pose")
        }
    }

    private fun advanceRunning(current: Phase.Running, now: Long) {
        val step = current.steps[current.index]
        val elapsed = now - current.startedMs
        when (step) {
            is MotionStep.Velocity -> {
                if (elapsed >= step.durationMs) {
                    nextStep(current, now)
                } else {
                    progress(now, "${describe(step, current.index + 1, current.steps.size)}: $elapsed of ${step.durationMs} ms")
                }
            }
            is MotionStep.Yaw -> {
                val error = AxisMapping.yawDelta(facts.yawDeg, step.targetDeg)
                if (abs(error) <= config.yawToleranceDeg) {
                    val since = current.yawSettledSinceMs ?: now
                    if (now - since >= config.yawSettleMs) {
                        nextStep(current, now)
                        return
                    }
                    phase = current.copy(yawSettledSinceMs = since)
                } else {
                    if (current.yawSettledSinceMs != null) phase = current.copy(yawSettledSinceMs = null)
                    if (elapsed >= step.durationMs) {
                        failActive(FlightReason.YAW_NOT_REACHED, "heading ${format(facts.yawDeg)} is ${format(error)} deg from ${format(step.targetDeg)} after $elapsed ms")
                        releaseVirtualStick()
                        return
                    }
                }
                progress(now, "${describe(step, current.index + 1, current.steps.size)}: heading ${format(facts.yawDeg)}, error ${format(error)} deg, $elapsed ms")
            }
        }
    }

    private fun nextStep(current: Phase.Running, now: Long) {
        val next = current.index + 1
        if (next < current.steps.size) {
            transition(Phase.Running(current.steps, next, now, null))
        } else {
            val detail = current.steps.mapIndexed { index, step -> describe(step, index + 1, current.steps.size) }.joinToString("; ")
            transition(Phase.Settling(now + config.settleMs, "$detail; settled ${config.settleMs} ms"))
        }
    }

    private fun advanceTakeoff(current: Phase.TakingOff, now: Long) {
        config.supervisedVertical?.let { supervised ->
            if (guardVerticalHeight(supervised, now) == null) return
        }
        val elapsed = now - current.startedMs
        if (facts.flying && elapsed >= config.takeoffMinMs) {
            val hovering = facts.speedMS < config.hoverSpeedMS && facts.flightState != TAKING_OFF
            if (hovering) {
                val since = current.hoverSinceMs ?: now
                if (now - since >= config.settleMs || elapsed >= config.takeoffTimeoutMs) {
                    afterTakeoff(current.targetZM, now, elapsed)
                    return
                }
                phase = current.copy(hoverSinceMs = since)
            } else if (elapsed >= config.takeoffTimeoutMs) {
                afterTakeoff(current.targetZM, now, elapsed)
                return
            } else if (current.hoverSinceMs != null) {
                phase = current.copy(hoverSinceMs = null)
            }
        } else if (!facts.flying && elapsed >= config.takeoffTimeoutMs) {
            supervisedFlightTargetZM = null
            supervisedFlightAirborne = false
            supervisedTakeoffStopIssued = false
            failActive(FlightReason.TAKEOFF_TIMEOUT, "aircraft is still ${facts.flightState} after $elapsed ms")
            transition(Phase.Idle)
            return
        }
        progress(now, "taking off: state ${facts.flightState}, z ${format(facts.zUp)} m, $elapsed ms")
    }

    private fun advanceSupervisedClimb(current: Phase.SupervisedClimb, now: Long) {
        val supervised = config.supervisedVertical ?: return
        val height = guardVerticalHeight(supervised, now) ?: return
        if (height >= current.targetZM) {
            val since = current.settledSinceMs ?: now
            if (now - since >= supervised.targetSettleMs) {
                completeActive(
                    "local height ${format(height)} m reached the soft target ${format(current.targetZM)} m " +
                        "for ${now - since} ms",
                )
                releaseVirtualStick()
            } else {
                phase = current.copy(settledSinceMs = since)
            }
            return
        }
        if (current.settledSinceMs != null) phase = current.copy(settledSinceMs = null)
        progress(now, "supervised climb: local height ${format(height)} m, target ${format(current.targetZM)} m")
    }

    private fun supervisedBenchHeightIsSafe(now: Long): Boolean {
        val supervised = config.supervisedVertical ?: return true
        val height = freshLocalHeight(supervised)
        val failure = when {
            height == null -> FlightReason.LOCAL_HEIGHT_UNAVAILABLE to "KeyAltitude is absent, invalid, future-dated, or older than ${supervised.maximumHeightAgeMs} ms"
            height < SUPERVISED_BENCH_MIN_HEIGHT_M -> FlightReason.NOT_AIRBORNE to "local height ${format(height)} m is below ${format(SUPERVISED_BENCH_MIN_HEIGHT_M)} m"
            height >= supervised.hardCeilingM -> FlightReason.VERTICAL_CEILING_EXCEEDED to "local height ${format(height)} m reached the hard ceiling ${format(supervised.hardCeilingM)} m"
            else -> return true
        }
        failActive(failure.first, "supervised bench motion stopped: ${failure.second}")
        event("supervised bench safety stop: ${failure.second}")
        if (facts.flying) startLanding(now, failure.first.wire) else releaseVirtualStick()
        return false
    }

    private fun freshLocalHeight(supervised: SupervisedVerticalConfig): Double? {
        val sample = facts.localHeight ?: return null
        val ageMs = monotonicNowMs() - sample.receivedAtMonotonicMs
        return sample.zUpM.takeIf { it >= 0.0 && ageMs in 0..supervised.maximumHeightAgeMs }
    }

    private fun guardVerticalHeight(supervised: SupervisedVerticalConfig, now: Long): Double? {
        val height = freshLocalHeight(supervised)
        if (height == null) {
            stopVertical(
                FlightReason.LOCAL_HEIGHT_UNAVAILABLE,
                "KeyAltitude is absent, invalid, future-dated, or older than ${supervised.maximumHeightAgeMs} ms",
                now,
            )
            return null
        }
        if (height >= supervised.hardCeilingM) {
            stopVertical(
                FlightReason.VERTICAL_CEILING_EXCEEDED,
                "local height ${format(height)} m reached the hard ceiling ${format(supervised.hardCeilingM)} m",
                now,
            )
            return null
        }
        return height
    }

    private fun stopVertical(reason: FlightReason, detail: String, now: Long) =
        stopSupervisedFlight(reason, detail, now)

    private fun stopSupervisedFlight(reason: FlightReason, detail: String, now: Long) {
        failActive(reason, detail)
        event("supervised vertical safety stop: $detail")
        if (facts.flying) {
            startLanding(now, reason.wire)
            return
        }
        if (!supervisedTakeoffStopIssued) {
            supervisedTakeoffStopIssued = true
            port.stopTakeoff { result ->
                if (result is PortResult.Failed) log("stop takeoff failed: ${result.detail}")
            }
        }
        transition(Phase.Idle)
    }

    private fun afterTakeoff(targetZM: Double, now: Long, elapsedMs: Long) {
        val supervised = config.supervisedVertical
        if (supervised != null) {
            if (guardVerticalHeight(supervised, now) == null) return
            event("takeoff hover reached at z ${format(facts.zUp)} m; closing the climb on fresh KeyAltitude toward ${format(targetZM)} m")
            beginVirtualStick(now) {
                transition(Phase.SupervisedClimb(targetZM, null))
            }
            return
        }
        val climb = MotionPlanner.climb(targetZM, facts, config.limits, config.altitudeToleranceM)
        if (climb == null) {
            completeActive("airborne at z ${format(facts.zUp)} m after $elapsedMs ms (target ${format(targetZM)} m)")
            transition(Phase.Idle)
            return
        }
        event("takeoff hover reached at z ${format(facts.zUp)} m; climbing to ${format(targetZM)} m under virtual stick")
        beginVirtualStick(now) { transition(Phase.Running(listOf(climb), 0, clock.nowMs(), null)) }
    }

    private fun startLanding(now: Long, reason: String) {
        if (vsEnabled || phase is Phase.Enabling) {
            port.sendStick(StickFrame.NEUTRAL)
            vsEnabled = false
            port.disableVirtualStick { result -> if (result is PortResult.Failed) log("virtual stick disable before landing failed: ${result.detail}") }
        }
        landingReason = reason
        issueLanding(now, reason, attempts = 1)
    }

    private fun issueLanding(now: Long, reason: String, attempts: Int) {
        transition(Phase.Landing(now, reason, attempts, awaitingResult = true, retryAtMs = null))
        val gen = generation
        port.startLanding { result ->
            if (gen != generation) return@startLanding
            val current = phase as? Phase.Landing ?: return@startLanding
            when (result) {
                PortResult.Ok -> {
                    active?.executingNow("auto-landing started ($reason)")
                    phase = current.copy(awaitingResult = false)
                }
                is PortResult.Failed -> {
                    if (reason == LAND_COMMAND) {
                        failActive(FlightReason.LANDING_FAILED, "landing action refused: ${result.detail}")
                        landingReason = null
                        transition(Phase.Idle)
                    } else if (current.attempts >= config.landingRetries) {
                        event("landing action refused ${current.attempts} times ($reason): ${result.detail}; RC operator must land")
                        phase = current.copy(awaitingResult = false, retryAtMs = null)
                    } else {
                        event("landing action refused ($reason): ${result.detail}; retrying in ${config.landingRetryMs} ms")
                        phase = current.copy(awaitingResult = false, retryAtMs = clock.nowMs() + config.landingRetryMs)
                    }
                }
            }
        }
    }

    private fun advanceLanding(current: Phase.Landing, now: Long) {
        val elapsed = now - current.startedMs
        if (facts.onGround) {
            completeActive("landed after $elapsed ms (${current.reason})")
            event("landed (${current.reason})")
            supervisedFlightTargetZM = null
            supervisedFlightAirborne = false
            supervisedTakeoffStopIssued = false
            landingReason = null
            transition(Phase.Idle)
            return
        }
        val retryAt = current.retryAtMs
        if (!current.awaitingResult && retryAt != null && now >= retryAt) {
            issueLanding(current.startedMs, current.reason, current.attempts + 1)
            return
        }
        if (elapsed >= config.landingTimeoutMs) {
            failActive(FlightReason.LANDING_TIMEOUT, "aircraft is still ${facts.flightState} at z ${format(facts.zUp)} m after $elapsed ms; RC operator lands")
            event("landing timed out (${current.reason}); RC operator lands")
            landingReason = null
            transition(Phase.Idle)
            return
        }
        progress(now, "landing (${current.reason}): state ${facts.flightState}, z ${format(facts.zUp)} m, $elapsed ms")
    }

    private fun streamSticks(now: Long) {
        if (!vsEnabled) return
        val frame = when (val current = phase) {
            is Phase.Running -> frameFor(current.steps[current.index])
            is Phase.SupervisedClimb -> supervisedClimbFrame(current, now)
            Phase.Navigating -> navigationFrame(now)
            is Phase.Bench -> current.frame
            else -> StickFrame.NEUTRAL
        }
        port.sendStick(frame)
        stickSeq += 1
        rate.record(now)
        lastFrame = frame
        onStickSent?.invoke(stickSeq, frame, now)
        active?.let { it.executingNow(it.startDetail) }
    }

    private fun frameFor(step: MotionStep): StickFrame = when (step) {
        is MotionStep.Velocity -> mapping.toFrame(step.body)
        is MotionStep.Yaw -> mapping.toFrame(BodyVelocity(yawTargetDeg = step.targetDeg))
    }

    private fun supervisedClimbFrame(current: Phase.SupervisedClimb, now: Long): StickFrame {
        val supervised = config.supervisedVertical ?: return StickFrame.NEUTRAL
        val height = guardVerticalHeight(supervised, now) ?: return StickFrame.NEUTRAL
        if (height >= minOf(current.targetZM, supervised.softCeilingM, supervised.hardCeilingM)) return StickFrame.NEUTRAL
        return StickFrame.NEUTRAL.copy(verticalThrottle = config.limits.maxVerticalMS)
    }

    // ---- virtual stick lifecycle ----

    private fun beginVirtualStick(
        now: Long,
        then: () -> Unit,
    ) {
        if (vsEnabled) {
            then()
            return
        }
        // No stick stream without the deadman. Its thresholds arrive with the relay's
        // auth.accepted and it arms on join, so a wire command always finds it armed; a bench
        // takeoff's climb without a relay, or anything after a failsafe, does not.
        when (watchdogState) {
            WatchdogState.DISARMED -> {
                failActive(FlightReason.WATCHDOG_DISARMED, "deadman not armed; the aircraft stays under the flight controller and the RC")
                transition(Phase.Idle)
                return
            }
            WatchdogState.HOLD -> {
                failActive(FlightReason.WATCHDOG_HOLD, "deadman is holding after relay silence; no virtual stick enable until a fresh verified control heartbeat")
                transition(Phase.Idle)
                return
            }
            WatchdogState.FAILSAFE -> {
                failActive(FlightReason.WATCHDOG_FAILSAFE, "deadman is in failsafe; no virtual stick until the next join")
                transition(Phase.Idle)
                return
            }
            else -> Unit
        }
        transition(Phase.Enabling(now))
        val gen = generation
        observedVirtualStickEnabled = null
        port.enableVirtualStick { result ->
            if (gen != generation) {
                port.sendStick(StickFrame.NEUTRAL)
                port.disableVirtualStick { }
                return@enableVirtualStick
            }
            val enabling = phase as? Phase.Enabling ?: return@enableVirtualStick
            if (clock.nowMs() - enabling.sinceMs > config.enableTimeoutMs) {
                failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick mode contract answered after its deadline")
                releaseVirtualStick()
                return@enableVirtualStick
            }
            when (result) {
                PortResult.Ok -> {
                    vsEnabled = true
                    port.setAdvancedMode(true)
                    event("virtual stick mode contract verified (advanced mode, velocity, BODY frame, ${cadence.hz} Hz)")
                    then()
                }
                is PortResult.Failed -> {
                    failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick enable refused: ${result.detail}")
                    releaseVirtualStick()
                }
            }
        }
    }

    private fun releaseVirtualStick(
        awaitResult: Boolean = false,
        onResult: ((PortResult) -> Unit)? = null,
    ) {
        val mayBeEnabled = vsEnabled || phase is Phase.Enabling
        transition(if (awaitResult) Phase.Releasing(clock.nowMs()) else Phase.Idle)
        if (mayBeEnabled) {
            port.sendStick(StickFrame.NEUTRAL)
            vsEnabled = false
            event("virtual stick disable requested; aircraft stays on neutral sticks until the SDK answers")
            port.disableVirtualStick { result ->
                if (result is PortResult.Failed) log("virtual stick disable failed: ${result.detail}")
                onResult?.invoke(result)
            }
        } else {
            onResult?.invoke(PortResult.Ok)
        }
    }

    // ---- reporting ----

    private fun progress(now: Long, detail: String) {
        val current = active ?: return
        if (!current.executingSent) return
        if (now - current.lastProgressMs < config.progressIntervalMs) return
        current.lastProgressMs = now
        current.sink.executing(detail)
    }

    private fun completeActive(detail: String) {
        val current = active ?: return
        active = null
        current.executingNow(current.startDetail)
        current.sink.completed(detail)
        event("${current.command.operation} ${current.command.commandId} completed: $detail")
    }

    private fun failActive(reason: FlightReason, detail: String) {
        val current = active ?: return
        active = null
        fail(current.sink, reason, detail)
        event("${current.command.operation} ${current.command.commandId} failed: ${reason.wire}: $detail")
    }

    private fun preempt(byOperation: String) {
        val current = active ?: return
        active = null
        fail(current.sink, FlightReason.SUPERSEDED, "preempted by $byOperation")
        event("${current.command.operation} ${current.command.commandId} superseded by $byOperation")
    }

    private fun fail(sink: ReportSink, reason: FlightReason, detail: String) {
        sink.failed(reason, "$detail [${reason.classWord}]")
    }

    private fun transition(next: Phase) {
        if (next !is Phase.Idle) pilotInputNoted = false
        phase = next
        generation += 1
    }

    private fun event(text: String) {
        lastEvent = text
        log(text)
    }

    private fun publish(now: Long) {
        val next = snapshot(now)
        if (next == published) return
        published = next
        onStatus?.invoke(next)
    }

    private fun snapshot(now: Long): FlightStatus = FlightStatus(
        phase = phaseName(phase),
        activeCommandId = active?.command?.commandId,
        activeOperation = active?.command?.operation,
        virtualStickEnabled = vsEnabled,
        watchdog = watchdogState.name.lowercase(),
        authorityLostReason = authorityLost,
        estopLatched = estopLatched,
        landingReason = landingReason,
        lastFrame = lastFrame,
        sticksSent = stickSeq,
        stickRateHz = rate.rateHz(now),
        settings = settings,
        mapping = mapping,
        lastEvent = lastEvent,
        failsafeSetting = failsafeSetting,
    )

    private fun phaseName(phase: Phase): String = when (phase) {
        Phase.Idle -> "idle"
        is Phase.Enabling -> "enabling_virtual_stick"
        is Phase.Releasing -> "releasing_virtual_stick"
        is Phase.Running -> when (phase.steps[phase.index]) {
            is MotionStep.Velocity -> "velocity_step"
            is MotionStep.Yaw -> "yaw_step"
        }
        is Phase.Settling -> "settling"
        is Phase.TakingOff -> "taking_off"
        is Phase.SupervisedClimb -> "supervised_climb"
        is Phase.Landing -> "landing"
        is Phase.Holding -> "watchdog_hold"
        Phase.Navigating -> "navigating"
        is Phase.NavigationHolding -> "navigation_hold"
        is Phase.Bench -> "bench_${phase.label}"
    }

    private fun describe(step: MotionStep, index: Int, total: Int): String = when (step) {
        is MotionStep.Velocity -> {
            val (east, north, up) = step.displacementM
            "step $index/$total: body forward ${format(step.body.forwardMS)}, right ${format(step.body.rightMS)}, up ${format(step.body.upMS)} m/s " +
                "for ${step.durationMs} ms (ground displacement east ${format(east)}, north ${format(north)}, up ${format(up)} m" +
                (if (step.slowed) ", slowed to the node limit)" else ")")
        }
        is MotionStep.Yaw -> "step $index/$total: yaw angle mode to ${format(step.targetDeg)} deg (${format(step.deltaDeg)} deg at ${format(step.speedDegS)} deg/s, deadline ${step.durationMs} ms)"
    }

    private fun frameWord(frame: StickFrame): String =
        if (frame.isNeutral) "neutral sticks" else "pitch ${format(frame.pitch)}, roll ${format(frame.roll)}, yaw ${format(frame.yaw)} (${frame.yawMode.name.lowercase()}), vertical ${format(frame.verticalThrottle)}"

    private fun format(value: Double): String = String.format(java.util.Locale.ROOT, "%.2f", value)

    private companion object {
        const val TAKING_OFF = "taking_off"
        const val LAND_COMMAND = "land_command"
        const val SPEED_EPSILON_MS = 0.02
        const val HOVERING = "hovering"
        const val SUPERVISED_BENCH_MIN_HEIGHT_M = 0.3
        const val SUPERVISED_BENCH_MAX_SPEED_MS = 0.3
        const val SUPERVISED_BENCH_MAX_DURATION_MS = 1_500L
    }
}
