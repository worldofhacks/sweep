package org.worldofhacks.sweep.bridge

import dji.sdk.keyvalue.key.AirLinkKey
import dji.sdk.keyvalue.key.BatteryKey
import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.common.Attitude
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.LocationCoordinate3D
import dji.sdk.keyvalue.value.common.Velocity3D
import dji.sdk.keyvalue.value.flightcontroller.FlightMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.manager.KeyManager
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.hypot
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.HardwareProfile
import org.worldofhacks.sweep.bridge.node.AircraftSnapshot
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.CommandExecutor
import org.worldofhacks.sweep.bridge.node.CommandReport
import org.worldofhacks.sweep.bridge.node.FlightStates
import org.worldofhacks.sweep.bridge.session.AircraftIdentity

/**
 * The probe flavor's aircraft: DJI `KeyManager` listeners assembled into the Telemetry v1
 * snapshot the relay link streams at 10 Hz (Phase C2), plus the measured update rate of every
 * listened key. Rates are measured, never assumed: the prior-art notes on issue #43 found a
 * bridge whose telemetry ran at 2 Hz while its README claimed 20.
 *
 * Provisional mappings, to be confirmed against the aircraft in the next hardware session:
 * - `x`, `y` are east/north metres from the first valid `KeyAircraftLocation3D` fix; indoors
 *   there is usually no fix, so they stay 0 and `pos_quality` stays 0.
 * - `z` is `KeyAltitude` (barometric, metres above takeoff) or, failing that,
 *   `KeyUltrasonicHeight` in decimetres divided by ten.
 * - `vx`, `vy`, `vz`: `KeyAircraftVelocity` is N-E-D (x north, y east, z down), so the SDK's
 *   `y` is the planner's `vx` (east), its `x` the planner's `vy` (north), and `vz` flips to
 *   z-up, matching the east/north position mapping above. The #85 axis probe reads these.
 * - `yaw` is `KeyAircraftAttitude.yaw` (degrees, 0 north, clockwise), which the Phase E
 *   body-frame steps and `rotate_to` use.
 * - `state` follows motors and flying flags plus the flight mode name (`taking_off`,
 *   `landing`, `hovering` below 0.2 m/s, else `airborne`).
 *
 * Flight commands are routed to the Phase E `FlightExecutor` before they reach this class;
 * the camera and media commands fail with `unsupported` until Phase G, so the node never
 * claims it executed something it cannot drive.
 */
class ProbeAircraft(
    phoneModel: String,
    androidVersion: String,
    private val sdkVersion: () -> String,
    private val log: (name: String, detail: String) -> Unit,
) : AircraftSource, CommandExecutor {
    private val lock = Any()
    private val holder = Any()
    private val rates = KeyRates()
    private var attached = false

    private var location: LocationCoordinate3D? = null
    private var origin: LocationCoordinate3D? = null
    private var velocity: Velocity3D? = null
    private var attitude: Attitude? = null
    private var altitude: Double? = null
    private var ultrasonicHeightDm: Int? = null
    private var flightMode: FlightMode? = null
    private var motorsOn: Boolean? = null
    private var flying: Boolean? = null
    private var aircraftConnected = false
    private var rcConnected = false
    private var batteryPercent: Int? = null
    private var signalQuality: Int? = null
    private var virtualStickEnabled = false
    private var authorityLostReason: String? = null
    private var hardware = HardwareProfile(
        aircraftModel = HardwareProfile.UNREPORTED,
        aircraftFirmware = HardwareProfile.UNREPORTED,
        rcFirmware = HardwareProfile.UNREPORTED,
        phoneModel = phoneModel.ifBlank { HardwareProfile.UNREPORTED },
        androidVersion = androidVersion.ifBlank { HardwareProfile.UNREPORTED },
        sdkVersion = HardwareProfile.UNREPORTED,
        measuredHfovDeg = null,
    )

    private val _snapshot = MutableStateFlow(AircraftSnapshot(hardware = hardware))
    override val snapshot: StateFlow<AircraftSnapshot> = _snapshot.asStateFlow()

    /** Registers every telemetry listener once; safe to call again. */
    /** Phase E hooks: [onAttached] fires once, when [attach] registers the listeners; [onProductConnected] on every product connection. */
    @Volatile
    var onAttached: (() -> Unit)? = null

    @Volatile
    var onProductConnected: (() -> Unit)? = null

    fun attach() {
        synchronized(lock) {
            if (attached) return
            attached = true
            hardware = hardware.copy(sdkVersion = sdkVersion().ifBlank { HardwareProfile.UNREPORTED })
        }
        listen("KeyConnection", KeyTools.createKey(FlightControllerKey.KeyConnection)) { connected ->
            aircraftConnected = connected
            if (!connected) origin = null
        }
        listen(
            "KeyRcConnection",
            KeyTools.createKey(RemoteControllerKey.KeyConnection, ComponentIndexType.LEFT_OR_MAIN),
        ) { connected -> rcConnected = connected }
        listen("KeyAircraftLocation3D", KeyTools.createKey(FlightControllerKey.KeyAircraftLocation3D)) { location = it }
        listen("KeyAircraftVelocity", KeyTools.createKey(FlightControllerKey.KeyAircraftVelocity)) { velocity = it }
        listen("KeyAircraftAttitude", KeyTools.createKey(FlightControllerKey.KeyAircraftAttitude)) { attitude = it }
        listen("KeyAltitude", KeyTools.createKey(FlightControllerKey.KeyAltitude)) { altitude = it }
        listen("KeyUltrasonicHeight", KeyTools.createKey(FlightControllerKey.KeyUltrasonicHeight)) { ultrasonicHeightDm = it }
        listen("KeyFlightMode", KeyTools.createKey(FlightControllerKey.KeyFlightMode)) { flightMode = it }
        listen("KeyAreMotorsOn", KeyTools.createKey(FlightControllerKey.KeyAreMotorsOn)) { motorsOn = it }
        listen("KeyIsFlying", KeyTools.createKey(FlightControllerKey.KeyIsFlying)) { flying = it }
        listen("KeyChargeRemainingInPercent", KeyTools.createKey(BatteryKey.KeyChargeRemainingInPercent)) { batteryPercent = it }
        listen("KeySignalQuality", KeyTools.createKey(AirLinkKey.KeySignalQuality)) { signalQuality = it }
        publish()
        onAttached?.invoke()
    }

    fun detach() {
        synchronized(lock) {
            if (!attached) return
            attached = false
        }
        KeyManager.getInstance().cancelListen(holder)
    }

    /** The SDK manager's own connect and disconnect callbacks, independent of `KeyConnection`. */
    fun productConnected(connected: Boolean) {
        synchronized(lock) {
            aircraftConnected = connected
            if (!connected) {
                rcConnected = false
                origin = null
            }
        }
        publish()
        if (connected) onProductConnected?.invoke()
    }

    fun updateIdentity(identity: AircraftIdentity) {
        synchronized(lock) {
            hardware = hardware.copy(
                aircraftModel = identity.productType ?: HardwareProfile.UNREPORTED,
                aircraftFirmware = identity.aircraftFirmware?.ifBlank { null } ?: HardwareProfile.UNREPORTED,
                rcFirmware = identity.rcFirmware?.ifBlank { null } ?: HardwareProfile.UNREPORTED,
            )
        }
        publish()
    }

    /** The Phase E loop's status, reported in `node_status` and (after a takeover) in readiness. */
    fun setFlightStatus(virtualStickEnabled: Boolean, authorityLostReason: String?) {
        synchronized(lock) {
            this.virtualStickEnabled = virtualStickEnabled
            this.authorityLostReason = authorityLostReason
        }
        publish()
    }

    override fun execute(command: CommandFrame, report: CommandReport) {
        when (command.args) {
            CommandArgs.CameraCapabilities -> {
                report.executing("capabilities frame sent by the link")
                report.completed("probed camera capabilities reported")
            }
            is CommandArgs.Takeoff, is CommandArgs.Goto, is CommandArgs.RotateTo, CommandArgs.Hover, CommandArgs.Land, CommandArgs.Estop ->
                report.failed("control_loop_unavailable", "flight commands are routed to the Virtual Stick loop; this executor never drives motion")
            else -> report.failed("unsupported", "the camera and media path lands with Phase G")
        }
    }

    private fun <T : Any> listen(name: String, key: DJIKey<T>, apply: (T) -> Unit) {
        val manager = KeyManager.getInstance()
        if (!manager.isKeySupported(key)) {
            log("Telemetry key", "$name is not supported by this product")
            return
        }
        manager.listen(
            key,
            holder,
            CommonCallbacks.KeyListener<T> { _, newValue ->
                if (newValue != null) {
                    synchronized(lock) {
                        rates.tick(name, System.currentTimeMillis())
                        apply(newValue)
                    }
                    publish()
                }
            },
        )
    }

    private fun publish() {
        val next = synchronized(lock) { build() }
        _snapshot.value = next
    }

    private fun build(): AircraftSnapshot {
        val now = System.currentTimeMillis()
        val fix = location?.takeIf { validFix(it) }
        if (fix != null && origin == null) origin = fix
        val base = origin
        val (x, y) = if (fix != null && base != null) eastNorthMetres(base, fix) else 0.0 to 0.0
        val z = altitude ?: ultrasonicHeightDm?.let { it / 10.0 } ?: 0.0
        val v = velocity
        // KeyAircraftVelocity is N-E-D: SDK y (east) is planner x, SDK x (north) is planner y.
        val vx = v?.y ?: 0.0
        val vy = v?.x ?: 0.0
        val vz = -(v?.z ?: 0.0)
        val yaw = attitude?.yaw ?: 0.0
        val mode = flightMode?.name.orEmpty()
        val state = when {
            motorsOn != true -> FlightStates.LANDED
            flying != true -> FlightStates.ARMED
            mode.contains("TAKE_OFF") -> FlightStates.TAKING_OFF
            mode.contains("LANDING") -> FlightStates.LANDING
            hypot(vx, vy) < HOVER_SPEED_M_S -> FlightStates.HOVERING
            else -> FlightStates.AIRBORNE
        }
        return AircraftSnapshot(
            aircraftConnected = aircraftConnected,
            rcConnected = rcConnected,
            x = x,
            y = y,
            z = z,
            vx = vx,
            vy = vy,
            vz = vz,
            battery = ((batteryPercent ?: 0) / 100.0).coerceIn(0.0, 1.0),
            state = state,
            link = ((signalQuality ?: 0) / 100.0).coerceIn(0.0, 1.0),
            // Provisional until the indoor positioning mapping is measured (issue #43, Phase C2).
            posQuality = if (fix != null) PROVISIONAL_FIX_QUALITY else 0.0,
            hardware = hardware,
            keyRatesHz = rates.snapshot(now),
            yawDeg = yaw,
            virtualStickEnabled = virtualStickEnabled,
            authorityLostReason = authorityLostReason,
        )
    }

    private fun validFix(fix: LocationCoordinate3D): Boolean {
        val latitude = fix.latitude
        val longitude = fix.longitude
        return latitude.isFinite() && longitude.isFinite() &&
            abs(latitude) <= 90.0 && abs(longitude) <= 180.0 &&
            !(latitude == 0.0 && longitude == 0.0)
    }

    private fun eastNorthMetres(base: LocationCoordinate3D, fix: LocationCoordinate3D): Pair<Double, Double> {
        val east = (fix.longitude - base.longitude) * cos(Math.toRadians(base.latitude)) * METRES_PER_DEGREE_LONGITUDE
        val north = (fix.latitude - base.latitude) * METRES_PER_DEGREE_LATITUDE
        return east to north
    }

    /** Per-key update timestamps over a sliding window; the rate is updates per second. */
    private class KeyRates {
        private val times = HashMap<String, ArrayDeque<Long>>()

        fun tick(name: String, nowMs: Long) {
            val queue = times.getOrPut(name) { ArrayDeque() }
            queue.addLast(nowMs)
            while (queue.isNotEmpty() && queue.first() < nowMs - WINDOW_MS) queue.removeFirst()
        }

        fun snapshot(nowMs: Long): Map<String, Double> = times.mapValues { (_, queue) ->
            while (queue.isNotEmpty() && queue.first() < nowMs - WINDOW_MS) queue.removeFirst()
            queue.size * 1000.0 / WINDOW_MS
        }

        private companion object {
            const val WINDOW_MS = 5_000L
        }
    }

    private companion object {
        const val HOVER_SPEED_M_S = 0.2
        const val PROVISIONAL_FIX_QUALITY = 0.5
        const val METRES_PER_DEGREE_LATITUDE = 110_574.0
        const val METRES_PER_DEGREE_LONGITUDE = 111_320.0
    }
}
