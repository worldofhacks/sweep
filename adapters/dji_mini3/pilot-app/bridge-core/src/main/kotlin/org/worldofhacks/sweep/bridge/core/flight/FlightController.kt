package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
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
 * 1. Deadman. The loop keeps its own [Watchdog] on the relay-distributed thresholds, fed by
 *    the relay frames it sees through [updateLink] and by admitted commands. It runs
 *    independently of the relay link object, so tearing the link down cannot stop the
 *    protection. Hold decays the stream to neutral sticks and fails the active command with
 *    `watchdog_hold`; failsafe commands auto-landing (indoors: land, never return to home)
 *    and fails with `watchdog_failsafe`. Neutral sticks keep flowing while Virtual Stick is
 *    enabled, so the stream never stops silently.
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
    private val log: (String) -> Unit = {},
) {
    private sealed interface Phase {
        data object Idle : Phase

        data class Enabling(val sinceMs: Long) : Phase

        data class Running(val steps: List<MotionStep>, val index: Int, val startedMs: Long, val yawSettledSinceMs: Long?) : Phase

        data class Settling(val untilMs: Long, val detail: String) : Phase

        data class TakingOff(val startedMs: Long, val targetZM: Double, val hoverSinceMs: Long?) : Phase

        data class Landing(val startedMs: Long, val reason: String, val attempts: Int, val awaitingResult: Boolean, val retryAtMs: Long?) : Phase

        data class Holding(val sinceMs: Long) : Phase

        data class Bench(val frame: StickFrame, val untilMs: Long, val label: String) : Phase
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
    private var settings: FlightSettings? = null
    private var watchdog: Watchdog? = null
    private var lastRelayActivityMs: Long? = null
    private var phase: Phase = Phase.Idle
    private var generation = 0L
    private var active: Active? = null
    private var vsEnabled = false
    private var authorityLost: String? = null
    private var pilotInputNoted = false
    private var estopLatched = false
    private var estopSinceMs = 0L
    private var estopLandStarted = false
    private var landingReason: String? = null
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
            event("joined the relay; deadman armed (hold ${dog.config.holdMs} ms, failsafe ${dog.config.failsafeMs} ms)")
        }
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
        if (vsEnabled) {
            vsEnabled = false
            port.disableVirtualStick { result -> if (result is PortResult.Failed) log("virtual stick disable after takeover failed: ${result.detail}") }
        }
        landingReason = null
        transition(Phase.Idle)
    }

    /**
     * The SDK's own view of Virtual Stick: losing it while the loop believes it is on is a
     * takeover; finding it enabled for the node while the loop is idle (a link blip or an app
     * restart mid-command left the flight controller waiting for frames nobody sends) is
     * cleared at once so the aircraft goes back to the flight controller and the RC.
     */
    fun onVirtualStickState(enabled: Boolean, ownedBySdk: Boolean, owner: String) {
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
        watchdog?.command()
        authorityLost?.let {
            fail(sink, FlightReason.AUTHORITY_LOST, "control authority not re-armed after $it; re-arm on the flight card")
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
            if (vsEnabled) releaseVirtualStick()
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
            if (vsEnabled) releaseVirtualStick()
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
        active = Active(command, sink, now, "auto takeoff")
        transition(Phase.TakingOff(now, targetZ, null))
        val gen = generation
        port.startTakeoff { result ->
            if (gen != generation) return@startTakeoff
            when (result) {
                PortResult.Ok -> active?.executingNow("auto takeoff started; target z ${format(targetZ)} m, completes on the reported flight state")
                is PortResult.Failed -> {
                    failActive(FlightReason.TAKEOFF_FAILED, "takeoff action refused: ${result.detail}")
                    transition(Phase.Idle)
                }
            }
        }
    }

    private fun goto(command: FlightCommand, args: CommandArgs.Goto, sink: ReportSink, now: Long) {
        if (!motionAllowed(sink)) return
        if (!facts.flying) {
            fail(sink, FlightReason.NOT_AIRBORNE, "aircraft is ${facts.flightState}; goto needs a hovering aircraft")
            return
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
        if (!motionAllowed(sink)) return false
        if (!facts.flying) {
            fail(sink, FlightReason.NOT_AIRBORNE, "bench procedures start from a guarded hover")
            return false
        }
        val body = mapping.toBody(frame)
        if (!config.limits.within(body)) {
            fail(sink, FlightReason.UNSUPPORTED, "bench frame exceeds the flight limits")
            return false
        }
        active = Active(command, sink, now, "bench $label: holding ${frameWord(frame)} for $durationMs ms")
        beginVirtualStick(now) { transition(Phase.Bench(frame, clock.nowMs() + durationMs, label)) }
        return true
    }

    /** Ends a bench hold early; the command completes as "stopped by the operator". */
    fun stopBench() {
        val current = phase
        if (current !is Phase.Bench) return
        completeActive("bench ${current.label} stopped by the operator")
        releaseVirtualStick()
    }

    /** Takeoff and land from the bench card use the command path with a logging sink. */
    fun benchTakeoff(zMm: Long, sink: ReportSink) = execute(FlightCommand("bench-takeoff-${clock.nowMs()}", CommandArgs.Takeoff(zMm), "bench takeoff"), sink)

    fun benchLand(sink: ReportSink) = execute(FlightCommand("bench-land-${clock.nowMs()}", CommandArgs.Land, "bench land"), sink)

    // ---- the tick ----

    fun tick(nowMs: Long) {
        port.advance(nowMs)
        pollDeadman(nowMs)
        checkLink()
        checkEstop(nowMs)
        advancePhase(nowMs)
        streamSticks(nowMs)
        publish(nowMs)
    }

    private fun pollDeadman(now: Long) {
        val transition = watchdog?.poll() ?: return
        when (transition.to) {
            WatchdogState.HOLD -> enterHold(now, transition.elapsedMs)
            WatchdogState.FAILSAFE -> enterFailsafe(now, transition.elapsedMs)
            WatchdogState.ARMED -> if (phase is Phase.Holding) {
                event("relay activity resumed after hold; virtual stick released")
                releaseVirtualStick()
            }
            WatchdogState.DISARMED -> Unit
        }
    }

    private fun enterHold(now: Long, elapsedMs: Long) {
        val detail = "no relay activity for $elapsedMs ms (hold threshold ${settings?.holdMs} ms): sticks neutral until the link recovers"
        event("watchdog hold: $detail")
        if (phase is Phase.Landing) return
        failActive(FlightReason.WATCHDOG_HOLD, detail)
        transition(if (vsEnabled) Phase.Holding(now) else Phase.Idle)
    }

    private fun enterFailsafe(now: Long, elapsedMs: Long) {
        val detail = "no relay activity for $elapsedMs ms (failsafe threshold ${settings?.failsafeMs} ms): auto-landing now, never return to home"
        event("watchdog failsafe: $detail")
        if (phase is Phase.Landing) return
        // Decided before failActive clears the command: the node lands only what it was flying.
        val flownByNode = vsEnabled || active != null || phase !is Phase.Idle
        failActive(FlightReason.WATCHDOG_FAILSAFE, detail)
        val lost = authorityLost
        when {
            lost != null -> {
                event("failsafe: the RC has the aircraft ($lost); no landing commanded")
                transition(Phase.Idle)
            }
            facts.onGround -> if (vsEnabled) releaseVirtualStick() else transition(Phase.Idle)
            !flownByNode -> {
                event("failsafe: the loop was idle with virtual stick off; the aircraft stays with the flight controller and the RC operator, no landing commanded")
                transition(Phase.Idle)
            }
            else -> startLanding(now, "watchdog_failsafe")
        }
    }

    private fun checkLink() {
        if (facts.linked) return
        if (phase == Phase.Idle && active == null && !vsEnabled) return
        val reason = if (!facts.aircraftConnected) "aircraft_disconnected" else "rc_disconnected"
        event("authority lost: $reason; loop cancelled, physical RC remains primary")
        failActive(FlightReason.AUTHORITY_LOST, "$reason: the loop cancelled; physical RC remains primary")
        if (vsEnabled) {
            // Best effort: the SDK may be disconnected, but if only the RC dropped the flight
            // controller must not be left waiting for frames nobody sends.
            vsEnabled = false
            port.disableVirtualStick { result -> if (result is PortResult.Failed) log("virtual stick disable after link loss failed: ${result.detail}") }
        }
        landingReason = null
        transition(Phase.Idle)
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
                is Phase.Running, is Phase.Bench -> {
                    failActive(FlightReason.ESTOP_ASSERTED, "relay network stop asserted: sticks neutral, hovering")
                    transition(Phase.Settling(now + config.settleMs, "network stop hover"))
                }
                is Phase.TakingOff -> {
                    failActive(FlightReason.ESTOP_ASSERTED, "relay network stop asserted during takeoff; the aircraft finishes the takeoff under the flight controller")
                    transition(Phase.Idle)
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

    private fun advancePhase(now: Long) {
        when (val current = phase) {
            Phase.Idle, is Phase.Holding -> Unit
            is Phase.Enabling -> if (now - current.sinceMs > config.enableTimeoutMs) {
                failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick enable did not answer within ${config.enableTimeoutMs} ms")
                transition(Phase.Idle)
            }
            is Phase.Running -> advanceRunning(current, now)
            is Phase.Settling -> if (now >= current.untilMs) {
                completeActive("${current.detail}; measured speed ${format(facts.speedMS)} m/s")
                releaseVirtualStick()
            }
            is Phase.TakingOff -> advanceTakeoff(current, now)
            is Phase.Landing -> advanceLanding(current, now)
            is Phase.Bench -> if (now >= current.untilMs) {
                completeActive("bench ${current.label} held ${frameWord(current.frame)} for ${now - (active?.startedMs ?: now)} ms")
                releaseVirtualStick()
            }
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
            failActive(FlightReason.TAKEOFF_TIMEOUT, "aircraft is still ${facts.flightState} after $elapsed ms")
            transition(Phase.Idle)
            return
        }
        progress(now, "taking off: state ${facts.flightState}, z ${format(facts.zUp)} m, $elapsed ms")
    }

    private fun afterTakeoff(targetZM: Double, now: Long, elapsedMs: Long) {
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
        if (vsEnabled) {
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

    // ---- virtual stick lifecycle ----

    private fun beginVirtualStick(now: Long, then: () -> Unit) {
        if (vsEnabled) {
            then()
            return
        }
        transition(Phase.Enabling(now))
        val gen = generation
        port.enableVirtualStick { result ->
            if (gen != generation) {
                // The loop moved on (hold, takeover, link loss) while the SDK was enabling;
                // never leave Virtual Stick on with nobody streaming to it.
                if (result == PortResult.Ok) port.disableVirtualStick { }
                return@enableVirtualStick
            }
            when (result) {
                PortResult.Ok -> {
                    vsEnabled = true
                    port.setAdvancedMode(true)
                    event("virtual stick enabled (advanced mode, velocity, BODY frame, ${cadence.hz} Hz)")
                    then()
                }
                is PortResult.Failed -> {
                    failActive(FlightReason.VIRTUAL_STICK_UNAVAILABLE, "virtual stick enable refused: ${result.detail}")
                    transition(Phase.Idle)
                }
            }
        }
    }

    private fun releaseVirtualStick() {
        if (vsEnabled) {
            port.sendStick(StickFrame.NEUTRAL)
            vsEnabled = false
            port.disableVirtualStick { result -> if (result is PortResult.Failed) log("virtual stick disable failed: ${result.detail}") }
            event("virtual stick disabled; aircraft under the flight controller and the RC")
        }
        transition(Phase.Idle)
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
        is Phase.Running -> when (phase.steps[phase.index]) {
            is MotionStep.Velocity -> "velocity_step"
            is MotionStep.Yaw -> "yaw_step"
        }
        is Phase.Settling -> "settling"
        is Phase.TakingOff -> "taking_off"
        is Phase.Landing -> "landing"
        is Phase.Holding -> "watchdog_hold"
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
    }
}
