package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.hypot
import kotlin.math.sqrt

/**
 * The aircraft facts the control loop reads on every tick, in the planner's telemetry frame
 * (`x` east, `y` north, `z` up, metres and metres per second) plus the compass heading. The
 * bridge-node layer derives it from the aircraft snapshot; [flying] and [onGround] come from
 * the planner `FlightState` name the node already reports.
 */
data class AircraftFacts(
    val aircraftConnected: Boolean = false,
    val rcConnected: Boolean = false,
    val flightState: String = "disarmed",
    val flying: Boolean = false,
    val onGround: Boolean = true,
    val xEast: Double = 0.0,
    val yNorth: Double = 0.0,
    val zUp: Double = 0.0,
    val vxEast: Double = 0.0,
    val vyNorth: Double = 0.0,
    val vzUp: Double = 0.0,
    val yawDeg: Double = 0.0,
) {
    val linked: Boolean
        get() = aircraftConnected && rcConnected

    val horizontalSpeedMS: Double
        get() = hypot(vxEast, vyNorth)

    val speedMS: Double
        get() = sqrt(vxEast * vxEast + vyNorth * vyNorth + vzUp * vzUp)

    /** Measured velocity rotated into the body frame (forward, right) at the current heading. */
    val bodyVelocity: Pair<Double, Double>
        get() = GroundFrame.toBody(vxEast, vyNorth, yawDeg)
}

/**
 * The relay-distributed thresholds the loop runs on (`auth.accepted.node`): the Virtual Stick
 * rate, clamped into DJI's documented 5 to 25 Hz, and the watchdog hold and failsafe windows.
 */
data class FlightSettings(
    val stickHz: Int,
    val holdMs: Long,
    val failsafeMs: Long,
) {
    init {
        require(holdMs >= 0 && failsafeMs > holdMs) { "watchdog thresholds must satisfy 0 <= hold < failsafe" }
    }

    val clampedStickHz: Int
        get() = StickCadence.clamp(stickHz)

    companion object {
        const val DEFAULT_STICK_HZ = 10
    }
}

/**
 * What the loop needs from the relay link: whether the node is joined, the relay's
 * authoritative network-stop flag, the time of the last authenticated relay frame (the
 * loop's own deadman clock, kept independently of the link object so the stick stream can
 * never stop silently when the link is torn down), and the thresholds.
 */
data class LinkFacts(
    val joined: Boolean = false,
    val estop: Boolean = false,
    val lastRelayActivityMs: Long? = null,
    val settings: FlightSettings? = null,
)
