package org.worldofhacks.sweep.bridge.core.flight

import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.NavigationPose
import org.worldofhacks.sweep.bridge.core.frames.NavigationRouteAuthorization

sealed interface PortResult {
    data object Ok : PortResult

    data class Failed(val detail: String) : PortResult
}

/**
 * The aircraft-facing side of the control loop. The probe flavor implements it on
 * `IVirtualStickManager` and the `KeyStartTakeoff` / `KeyStartAutoLanding` actions; the fake
 * flavor and the JVM tests implement it on [FakeFlightModel]. Results may arrive on any
 * thread; the caller marshals them back onto the loop thread before they reach the controller.
 */
interface FlightPort {
    fun enableVirtualStick(onResult: (PortResult) -> Unit)

    fun disableVirtualStick(onResult: (PortResult) -> Unit)

    fun setAdvancedMode(enabled: Boolean)

    fun sendStick(frame: StickFrame)

    fun startTakeoff(onResult: (PortResult) -> Unit)

    fun startLanding(onResult: (PortResult) -> Unit)

    /** Called once per tick before the loop reads the aircraft; the fake port integrates its kinematics here. */
    fun advance(nowMs: Long) = Unit
}

/**
 * Acknowledgement reasons the loop returns, with the retryable-versus-terminal class from
 * the MAVLink `COMMAND_ACK` taxonomy adopted on issue #43. The contract fixes the first three
 * words; the rest are the "other snake_case reason" the node protocol allows, and the class
 * travels in the display-only detail because the wire has no field for it.
 */
enum class FlightReason(val wire: String, val retryable: Boolean) {
    AUTHORITY_LOST("authority_lost", false),
    WATCHDOG_HOLD("watchdog_hold", true),
    WATCHDOG_FAILSAFE("watchdog_failsafe", false),
    /** The deadman is not armed: its thresholds arrive with the relay's `auth.accepted` and it arms on join; nothing streams without it. */
    WATCHDOG_DISARMED("watchdog_disarmed", true),
    ESTOP_ASSERTED("estop_asserted", true),
    NOT_AIRBORNE("not_airborne", true),
    ALREADY_AIRBORNE("already_airborne", false),
    AIRCRAFT_UNAVAILABLE("aircraft_unavailable", true),
    VIRTUAL_STICK_UNAVAILABLE("virtual_stick_unavailable", true),
    TAKEOFF_FAILED("takeoff_failed", true),
    TAKEOFF_TIMEOUT("takeoff_timeout", true),
    LANDING_FAILED("landing_failed", true),
    LANDING_TIMEOUT("landing_timeout", true),
    LANDING_IN_PROGRESS("landing_in_progress", true),
    YAW_NOT_REACHED("yaw_not_reached", true),
    NODE_BUSY("node_busy", true),
    SUPERSEDED("superseded", true),
    NAVIGATION_NOT_AUTHORIZED("navigation_not_authorized", false),
    NAVIGATION_HOLD("navigation_hold", true),
    NAVIGATION_LOST("navigation_lost", true),
    NAVIGATION_LAND("navigation_land", false),
    UNSUPPORTED("unsupported", false);

    val classWord: String
        get() = if (retryable) "retryable" else "terminal"
}

/** Progress of one command as the loop sees it; the bridge-node layer maps it onto the wire acknowledgements. */
interface ReportSink {
    fun executing(detail: String?)

    fun completed(detail: String?)

    fun failed(reason: FlightReason, detail: String?)
}

/**
 * An admitted command: the wire id (echoed in acknowledgements) and its typed arguments.
 * Bench procedures reuse the path with a [label] instead of a wire operation.
 */
data class FlightCommand(val commandId: String, val args: CommandArgs, val label: String? = null) {
    val operation: String
        get() = label ?: args.operation.wire

    /**
     * A wire motion command (`takeoff`, `goto`, `rotate_to`) the pilot's Control authority
     * toggle gates; bench procedures carry a label and are the pilot's own.
     */
    val relayMotion: Boolean
        get() = label == null && (args is CommandArgs.Takeoff || args is CommandArgs.Goto || args is CommandArgs.RotateTo)

    companion object {
        /** The operations the loop owns; everything else stays with the flavor's own executor. */
        fun isFlight(args: CommandArgs): Boolean = when (args) {
            is CommandArgs.Takeoff, is CommandArgs.Goto, is CommandArgs.RotateTo,
            CommandArgs.Hover, CommandArgs.Land, CommandArgs.Estop,
            -> true
            else -> false
        }
    }
}

data class NavigationEvidence(
    val authorization: NavigationRouteAuthorization? = null,
    val pose: NavigationPose? = null,
    val poseFreshUntilMs: Long? = null,
    val relayOffsetMs: Long? = null,
)

/** The loop's observable state for the screen, the bench log, and `node_status`. */
data class FlightStatus(
    val phase: String = "idle",
    val activeCommandId: String? = null,
    val activeOperation: String? = null,
    val virtualStickEnabled: Boolean = false,
    val watchdog: String = "disarmed",
    val authorityLostReason: String? = null,
    val estopLatched: Boolean = false,
    val landingReason: String? = null,
    val lastFrame: StickFrame? = null,
    val sticksSent: Long = 0,
    val stickRateHz: Double = 0.0,
    val settings: FlightSettings? = null,
    val mapping: AxisMapping = AxisMapping(),
    val lastEvent: String? = null,
    val failsafeSetting: String? = null,
)
