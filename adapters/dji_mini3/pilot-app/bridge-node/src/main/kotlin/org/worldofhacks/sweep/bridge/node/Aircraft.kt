package org.worldofhacks.sweep.bridge.node

import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.HardwareProfile
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState

/** Planner `FlightState` names the node reports in telemetry `state`. */
object FlightStates {
    const val DISARMED = "disarmed"
    const val LANDED = "landed"
    const val ARMED = "armed"
    const val TAKING_OFF = "taking_off"
    const val AIRBORNE = "airborne"
    const val HOVERING = "hovering"
    const val LANDING = "landing"
    const val EMERGENCY = "emergency"
}

/**
 * The latest aircraft facts the relay link reads on every telemetry tick. The `fake` flavor
 * synthesizes it; the `probe` flavor assembles it from DJI `KeyManager` listeners and records
 * each key's measured update rate in [keyRatesHz] (the prior-art notes on issue #43 found a
 * bridge whose telemetry ran at 2 Hz while its README claimed 20, so the rate is measured,
 * never assumed).
 *
 * Positions are metres in the planner frame, velocities metres per second; `battery`, `link`,
 * and `posQuality` are unit intervals. When there is no aircraft the link sends no telemetry
 * rather than inventing values, and the relay reports `telemetry_missing` truthfully.
 */
data class AircraftSnapshot(
    val aircraftConnected: Boolean = false,
    val rcConnected: Boolean = false,
    val x: Double = 0.0,
    val y: Double = 0.0,
    val z: Double = 0.0,
    val vx: Double = 0.0,
    val vy: Double = 0.0,
    val vz: Double = 0.0,
    val battery: Double = 0.0,
    val state: String = FlightStates.DISARMED,
    val link: Double = 0.0,
    val posQuality: Double = 0.0,
    val hardware: HardwareProfile,
    val camera: CameraProbe = CameraProbe(),
    val keyRatesHz: Map<String, Double> = emptyMap(),
)

interface AircraftSource {
    val snapshot: StateFlow<AircraftSnapshot>
}

/**
 * How an executor reports progress on an admitted command. The link has already sent
 * `accepted`; `executing` then `completed` or `failed` follow. Reports after a terminal state,
 * or after the connection epoch changed underneath the command, are dropped and logged.
 */
interface CommandReport {
    fun executing()

    fun completed()

    fun failed(reason: String, detail: String? = null)
}

/**
 * Runs an admitted command. The `fake` flavor's [FakeAircraft] moves a kinematic fixture;
 * the `probe` flavor fails every command with `control_loop_unavailable` until the Phase E
 * Virtual Stick control loop lands.
 */
fun interface CommandExecutor {
    fun execute(command: CommandFrame, report: CommandReport)
}

data class PhoneStatus(val batteryPercent: Int, val thermalState: PhoneThermalState)

fun interface PhoneStatusSource {
    fun current(): PhoneStatus
}

/** The video publisher's live `node_status.video_publish_state`; the Phase F publisher implements it. */
fun interface VideoPublishSource {
    fun current(): VideoPublishState
}
