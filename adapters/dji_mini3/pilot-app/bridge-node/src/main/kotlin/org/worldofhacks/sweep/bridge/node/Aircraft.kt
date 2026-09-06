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
    /**
     * The `probe` flavor's listener evidence per key: registered or not, what `isKeySupported`
     * answered at registration and at the last product connect, and the first value's time.
     * Empty in the `fake` flavor. Like [keyRatesHz] it is shown on the phone, never sent.
     */
    val telemetryKeys: Map<String, TelemetryKeyStatus> = emptyMap(),
    // Phase E flight hooks: compass heading (degrees, 0 north, clockwise) for body-frame
    // steps, whether the loop has Virtual Stick enabled (reported in `node_status`), and
    // the snake_case reason the loop lost control authority (an RC takeover latched until the
    // pilot re-arms; reported as `control_authority=false` in readiness).
    val yawDeg: Double = 0.0,
    val virtualStickEnabled: Boolean = false,
    val authorityLostReason: String? = null,
)

interface AircraftSource {
    val snapshot: StateFlow<AircraftSnapshot>
}

/**
 * How an executor reports progress on an admitted command. The link has already sent
 * `accepted`; `executing` then `completed` or `failed` follow. `executing` may repeat with a
 * progress detail for long operations (the MAVLink `IN_PROGRESS` shape adopted on issue #43;
 * the relay audits every one and the remote adapter keeps waiting). Reports after a terminal
 * state, or after the connection epoch changed underneath the command, are dropped and logged.
 */
interface CommandReport {
    fun executing(detail: String? = null)

    fun completed(detail: String? = null)

    fun failed(reason: String, detail: String? = null)
}

/**
 * Runs an admitted command. Flight operations go to the Phase E `FlightExecutor` in both
 * flavors; the `fake` flavor's [FakeAircraft] keeps the camera and gimbal fixture semantics
 * of `fake_node.py` and the `probe` flavor's camera path lands with Phase G.
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
