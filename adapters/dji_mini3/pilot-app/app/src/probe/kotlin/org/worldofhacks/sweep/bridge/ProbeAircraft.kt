package org.worldofhacks.sweep.bridge

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import dji.sdk.keyvalue.key.AirLinkKey
import dji.sdk.keyvalue.key.BatteryKey
import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.DJIKeyInfo
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.GimbalKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.common.Attitude
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotation
import dji.sdk.keyvalue.value.gimbal.GimbalAngleRotationMode
import dji.sdk.keyvalue.value.common.LocationCoordinate3D
import dji.sdk.keyvalue.value.common.Velocity3D
import dji.sdk.keyvalue.value.flightcontroller.FlightMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
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
import org.worldofhacks.sweep.bridge.node.AttitudeSample
import org.worldofhacks.sweep.bridge.node.CaptureAlignmentCollector
import org.worldofhacks.sweep.bridge.node.FlightStates
import org.worldofhacks.sweep.bridge.node.GimbalPitchOperationCoordinator
import org.worldofhacks.sweep.bridge.node.LocalHeightMeasurement
import org.worldofhacks.sweep.bridge.node.TelemetryKeyLedger
import org.worldofhacks.sweep.bridge.node.TelemetryKeyStatus
import org.worldofhacks.sweep.bridge.session.AircraftIdentity
import org.worldofhacks.sweep.bridge.session.GimbalPitchState

/**
 * The probe flavor's aircraft: DJI `KeyManager` listeners assembled into the Telemetry v1
 * snapshot the relay link streams at 10 Hz (Phase C2), plus the measured update rate of every
 * listened key. Rates are measured, never assumed: the prior-art notes on issue #43 found a
 * bridge whose telemetry ran at 2 Hz while its README claimed 20.
 *
 * Every key is listened as soon as the SDK registers, before the RC and aircraft are there.
 * `isKeySupported` answers for the product connected right now, so at that moment it is
 * usually false, and skipping a key on that answer left a session without its telemetry
 * once the aircraft did connect. The answer is recorded at registration and again on every
 * product connect and shown, never acted on; [TelemetryKeyLedger] holds those decisions
 * (plain JVM, tested in `bridge-node`) and makes sure a reconnect registers no key twice.
 * All listeners share one [holder], which is what MSDK v5 cancels them by.
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
internal class ProbeAircraft(
    phoneModel: String,
    androidVersion: String,
    private val sdkVersion: () -> String,
    private val log: (name: String, detail: String) -> Unit,
    /** Bench log hook: one call per key and listener event (`attached`, `product_connected`, `first_value`). */
    private val record: (key: String, event: String, status: TelemetryKeyStatus) -> Unit = { _, _, _ -> },
    private val rawRecorder: SensorRawRecorder = SensorRawRecorder.NONE,
    private val captureAlignment: CaptureAlignmentCollector? = null,
) : AircraftSource, CommandExecutor {
    private val lock = Any()

    /** The one holder every listener is registered with; `cancelListen(holder)` removes them all. */
    private val holder = Any()
    private val rates = KeyRates()
    private var attached = false

    private var location: LocationCoordinate3D? = null
    private var origin: LocationCoordinate3D? = null
    private var velocity: Velocity3D? = null
    private var attitude: Attitude? = null
    private var altitude: Double? = null
    private var altitudeReceivedAtMonotonicMs: Long? = null
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
    private val altitudePollHandler = Handler(Looper.getMainLooper())
    private val gimbalPitchHandler = Handler(Looper.getMainLooper())
    private val gimbalPitchCoordinator = GimbalPitchOperationCoordinator<CommandReport?>()
    private val gimbalPitchReportLock = Any()
    private val gimbalPitchDeadlines = HashMap<Long, Runnable>()
    private val _gimbalPitch = MutableStateFlow(GimbalPitchState())
    val gimbalPitch: StateFlow<GimbalPitchState> = _gimbalPitch.asStateFlow()
    private val gimbalRotateKey by lazy {
        KeyTools.createKey(GimbalKey.KeyRotateByAngle, ComponentIndexType.LEFT_OR_MAIN)
    }
    private val altitudeHardwareKey: DJIKey<Double> by lazy { KeyTools.createKey(FlightControllerKey.KeyAltitude) }
    private val altitudePoller = AltitudeHardwarePoller(
        scheduler = AltitudePollScheduler { delayMs, action ->
            altitudePollHandler.postDelayed(action, delayMs)
        },
        query = AltitudeHardwareQuery { callback ->
            KeyManager.getInstance().getValue(
                altitudeHardwareKey,
                object : CommonCallbacks.CompletionCallbackWithParam<Double> {
                    override fun onSuccess(value: Double?) = callback.onSuccess(value)

                    override fun onFailure(error: IDJIError) = callback.onFailure()
                },
            )
        },
        monotonicNowMs = SystemClock::elapsedRealtime,
        onSample = ::receivedHardwareAltitude,
        guard = lock,
    )
    private var hardware = HardwareProfile(
        aircraftModel = HardwareProfile.UNREPORTED,
        aircraftFirmware = HardwareProfile.UNREPORTED,
        rcFirmware = HardwareProfile.UNREPORTED,
        phoneModel = phoneModel.ifBlank { HardwareProfile.UNREPORTED },
        androidVersion = androidVersion.ifBlank { HardwareProfile.UNREPORTED },
        sdkVersion = HardwareProfile.UNREPORTED,
        measuredHfovDeg = null,
    )

    /** The listened keys; each DJI key object is created on first use, once the SDK is initialized. */
    private val bindings: List<Binding<*>> = listOf(
        Binding("KeyConnection", FlightControllerKey.KeyConnection) { connected ->
            aircraftConnected = connected
            altitudePoller.connectionChanged(connected)
            if (!connected) {
                origin = null
                altitude = null
                altitudeReceivedAtMonotonicMs = null
            }
        },
        Binding("KeyRcConnection", RemoteControllerKey.KeyConnection, ComponentIndexType.LEFT_OR_MAIN) { rcConnected = it },
        Binding("KeyAircraftLocation3D", FlightControllerKey.KeyAircraftLocation3D) { location = it },
        Binding("KeyAircraftVelocity", FlightControllerKey.KeyAircraftVelocity) { velocity = it },
        Binding("KeyAircraftAttitude", FlightControllerKey.KeyAircraftAttitude) { attitude = it },
        Binding("KeyGimbalAttitude", GimbalKey.KeyGimbalAttitude, ComponentIndexType.LEFT_OR_MAIN) { },
        Binding("KeyAltitude", FlightControllerKey.KeyAltitude) { value ->
            value.takeIf { it.isFinite() }?.let { altitude = it }
        },
        Binding("KeyUltrasonicHeight", FlightControllerKey.KeyUltrasonicHeight) { ultrasonicHeightDm = it },
        Binding("KeyFlightMode", FlightControllerKey.KeyFlightMode) { flightMode = it },
        Binding("KeyAreMotorsOn", FlightControllerKey.KeyAreMotorsOn) { motorsOn = it },
        Binding("KeyIsFlying", FlightControllerKey.KeyIsFlying) { flying = it },
        Binding("KeyChargeRemainingInPercent", BatteryKey.KeyChargeRemainingInPercent) { batteryPercent = it },
        Binding("KeySignalQuality", AirLinkKey.KeySignalQuality) { signalQuality = it },
    )
    private val byName = bindings.associateBy { it.name }
    private val ledger = TelemetryKeyLedger(bindings.map { it.name })

    private val _snapshot = MutableStateFlow(AircraftSnapshot(hardware = hardware))
    override val snapshot: StateFlow<AircraftSnapshot> = _snapshot.asStateFlow()

    /** Phase E hooks: [onAttached] fires once, when [attach] registers the listeners; [onProductConnected] on every product connection. */
    @Volatile
    var onAttached: (() -> Unit)? = null

    @Volatile
    var onProductConnected: (() -> Unit)? = null

    /**
     * Registers every telemetry listener, whatever `isKeySupported` says at the moment (the
     * SDK is registered, the aircraft usually not yet connected); safe to call again, a key
     * that already has a listener is not registered twice.
     */
    fun attach() {
        val manager = KeyManager.getInstance()
        val now = System.currentTimeMillis()
        val answers = bindings.associate { it.name to it.supported(manager) }
        val (firstAttach, registered, statuses) = synchronized(lock) {
            val first = !attached
            attached = true
            if (first) hardware = hardware.copy(sdkVersion = sdkVersion().ifBlank { HardwareProfile.UNREPORTED })
            val names = ledger.attach(now) { answers.getValue(it) }
            Triple(first, names.map(byName::getValue), ledger.snapshot())
        }
        registered.forEach { it.listen(manager) }
        altitudePoller.attach()
        if (registered.isNotEmpty()) {
            log(
                "Telemetry keys",
                "${registered.size} listeners registered; isKeySupported now: ${support(statuses) { it.supportedAtAttach }}. " +
                    "Every key is listened regardless (the answer is per connected product) and asked again when a product connects.",
            )
            registered.forEach { record(it.name, "attached", statuses.getValue(it.name)) }
        }
        publish()
        if (firstAttach) onAttached?.invoke()
    }

    fun detach() {
        synchronized(lock) {
            if (!attached) return
            attached = false
        }
        failActiveGimbalPitch("gimbal disconnected")
        altitudePoller.detach()
        // The same holder every listener was registered with: MSDK v5 removes them by holder,
        // so the next attach() starts from none and cannot stack a second listener on a key.
        KeyManager.getInstance().cancelListen(holder)
        synchronized(lock) { ledger.detach() }
    }

    /**
     * The SDK manager's own connect and disconnect callbacks, independent of `KeyConnection`.
     * On connect, `isKeySupported` is asked again for the product that is now there, for the
     * record, and any key still without a listener gets one; keys already listened are left
     * alone, so a power cycle of the aircraft never doubles a subscription.
     */
    fun productConnected(connected: Boolean) {
        val manager = KeyManager.getInstance()
        val answers = if (connected) bindings.associate { it.name to it.supported(manager) } else emptyMap()
        val (registered, statuses) = synchronized(lock) {
            aircraftConnected = connected
            altitudePoller.connectionChanged(connected)
            if (!connected) {
                rcConnected = false
                origin = null
                altitude = null
                altitudeReceivedAtMonotonicMs = null
            }
            val names = if (connected) ledger.productConnected { answers.getValue(it) } else emptyList()
            Pair(names.map(byName::getValue), ledger.snapshot())
        }
        registered.forEach { it.listen(manager) }
        if (!connected) failActiveGimbalPitch("gimbal disconnected")
        if (connected) {
            val late = if (registered.isEmpty()) "all listeners were registered before the aircraft connected" else "${registered.size} listeners registered only now"
            log("Telemetry keys", "product connected; isKeySupported now: ${support(statuses) { it.supportedAtConnect }}; $late.")
            statuses.forEach { (name, status) -> record(name, "product_connected", status) }
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
        when (val args = command.args) {
            CommandArgs.CameraCapabilities -> {
                report.executing("capabilities frame sent by the link")
                report.completed("probed camera capabilities reported")
            }
            is CommandArgs.SetGimbalPitch -> requestGimbalPitch(args.pitchMdeg / 1_000.0, report)
            is CommandArgs.Takeoff, is CommandArgs.Goto, is CommandArgs.RotateTo, CommandArgs.Hover, CommandArgs.Land, CommandArgs.Estop ->
                report.failed("control_loop_unavailable", "flight commands are routed to the Virtual Stick loop; this executor never drives motion")
            else -> report.failed("unsupported", "the camera and media path lands with Phase G")
        }
    }

    fun requestLocalGimbalPitch(targetDegrees: Double) = requestGimbalPitch(targetDegrees, null)

    private fun requestGimbalPitch(targetDegrees: Double, report: CommandReport?) {
        synchronized(gimbalPitchReportLock) {
            requestGimbalPitchSerialized(targetDegrees, report)
        }
    }

    private fun requestGimbalPitchSerialized(targetDegrees: Double, report: CommandReport?) {
        if (!targetDegrees.isFinite() || targetDegrees !in MIN_GIMBAL_PITCH_DEGREES..MAX_GIMBAL_PITCH_DEGREES) {
            failGimbalPitch(report, targetDegrees, "software safety clamp permits $MIN_GIMBAL_PITCH_DEGREES° through $MAX_GIMBAL_PITCH_DEGREES°")
            return
        }
        if (!synchronized(lock) { attached && aircraftConnected }) {
            failGimbalPitch(report, targetDegrees, "gimbal is disconnected")
            return
        }
        val manager = KeyManager.getInstance()
        if (!runCatching { manager.isKeySupported(gimbalRotateKey) }.getOrDefault(false)) {
            failGimbalPitch(report, targetDegrees, "gimbal rotation is unavailable for the connected product")
            return
        }
        val start = synchronized(lock) {
            when {
                !attached || !aircraftConnected -> GimbalPitchStart.Unavailable
                else -> when (val begun = gimbalPitchCoordinator.begin(targetDegrees, SystemClock.elapsedRealtime(), report)) {
                    GimbalPitchOperationCoordinator.Start.Busy -> GimbalPitchStart.Busy
                    is GimbalPitchOperationCoordinator.Start.Begun -> {
                        _gimbalPitch.value = GimbalPitchState(true, targetDegrees, "Request sent. Waiting for DJI acceptance.")
                        GimbalPitchStart.Begun(begun.active)
                    }
                }
            }
        }
        val active = when (start) {
            GimbalPitchStart.Busy -> {
                report?.failed("gimbal_pitch_busy", "another gimbal pitch request is awaiting confirmation")
                return
            }
            GimbalPitchStart.Unavailable -> {
                failGimbalPitch(report, targetDegrees, "gimbal is disconnected")
                return
            }
            is GimbalPitchStart.Begun -> start.active
        }
        report?.executing("absolute gimbal pitch request sent; awaiting DJI acceptance")
        val dispatchFailure = synchronized(lock) {
            if (!attached || !aircraftConnected || !gimbalPitchCoordinator.isActive(active.request.id)) {
                "gimbal disconnected"
            } else {
                try {
                    manager.performAction(
                        gimbalRotateKey,
                        GimbalAngleRotation().apply {
                            mode = GimbalAngleRotationMode.ABSOLUTE_ANGLE
                            pitch = targetDegrees
                            pitchIgnored = false
                            rollIgnored = true
                            yawIgnored = true
                            duration = GIMBAL_ROTATION_DURATION_SECONDS
                            timeout = GIMBAL_ACTION_TIMEOUT_MS.toInt()
                        },
                        object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                            override fun onSuccess(value: EmptyMsg?) {
                                processGimbalPitchUpdate { gimbalPitchCoordinator.accepted(active.request.id, SystemClock.elapsedRealtime()) }
                            }

                            override fun onFailure(error: IDJIError) {
                                processGimbalPitchUpdate { gimbalPitchCoordinator.rejected(active.request.id, describeGimbalError(error)) }
                            }
                        },
                    )
                    null
                } catch (error: Exception) {
                    error.message ?: error.javaClass.simpleName
                }
            }
        }
        if (dispatchFailure != null) {
            processGimbalPitchUpdate { gimbalPitchCoordinator.rejected(active.request.id, dispatchFailure) }
            return
        }
        val deadline = Runnable {
            processGimbalPitchUpdate { gimbalPitchCoordinator.expired(active.request.id, SystemClock.elapsedRealtime()) }
        }
        val scheduled = synchronized(lock) {
            if (gimbalPitchCoordinator.isActive(active.request.id)) {
                gimbalPitchDeadlines[active.request.id] = deadline
                true
            } else {
                false
            }
        }
        if (scheduled) {
            val remainingMs = (active.request.deadlineMonotonicMs - SystemClock.elapsedRealtime()).coerceAtLeast(0)
            gimbalPitchHandler.postDelayed(deadline, remainingMs)
        }
    }

    private fun describeGimbalError(error: IDJIError): String =
        "${error.errorType()} ${error.errorCode()} ${error.description().orEmpty()}".trim()

    private fun failGimbalPitch(report: CommandReport?, targetDegrees: Double, detail: String) {
        synchronized(lock) {
            if (gimbalPitchCoordinator.activeId() == null) {
                _gimbalPitch.value = GimbalPitchState(false, targetDegrees.takeIf { it.isFinite() }, detail)
            }
        }
        report?.failed("gimbal_pitch_unavailable", detail)
    }

    private fun failActiveGimbalPitch(detail: String) {
        processGimbalPitchUpdate {
            gimbalPitchCoordinator.activeId()?.let { gimbalPitchCoordinator.rejected(it, detail) }
                ?: GimbalPitchOperationCoordinator.Update.Ignored
        }
    }

    private fun processGimbalPitchUpdate(
        update: () -> GimbalPitchOperationCoordinator.Update<CommandReport?>,
    ) {
        synchronized(gimbalPitchReportLock) {
            val effect = synchronized(lock) {
                when (val result = update()) {
                    GimbalPitchOperationCoordinator.Update.Ignored -> null
                    is GimbalPitchOperationCoordinator.Update.Accepted -> {
                        _gimbalPitch.value = GimbalPitchState(true, result.active.request.targetDegrees, "DJI accepted the request. Waiting for gimbal attitude.")
                        GimbalPitchReportEffect(result.active.operation, "DJI accepted rotation; awaiting a fresh gimbal-attitude confirmation", GimbalPitchReportState.EXECUTING)
                    }
                    is GimbalPitchOperationCoordinator.Update.Completed -> {
                        gimbalPitchDeadlines.remove(result.active.request.id)?.let(gimbalPitchHandler::removeCallbacks)
                        val detail = "Gimbal pitch confirmed at ${"%.1f".format(result.observedDegrees)}°."
                        _gimbalPitch.value = GimbalPitchState(false, result.active.request.targetDegrees, detail)
                        GimbalPitchReportEffect(result.active.operation, detail, GimbalPitchReportState.COMPLETED)
                    }
                    is GimbalPitchOperationCoordinator.Update.Failed -> {
                        gimbalPitchDeadlines.remove(result.active.request.id)?.let(gimbalPitchHandler::removeCallbacks)
                        _gimbalPitch.value = GimbalPitchState(false, result.active.request.targetDegrees, result.reason)
                        GimbalPitchReportEffect(result.active.operation, result.reason, GimbalPitchReportState.FAILED)
                    }
                }
            } ?: return
            when (effect.state) {
                GimbalPitchReportState.EXECUTING -> effect.report?.executing(effect.detail)
                GimbalPitchReportState.COMPLETED -> effect.report?.completed(effect.detail)
                GimbalPitchReportState.FAILED -> effect.report?.failed("gimbal_pitch_unconfirmed", effect.detail)
            }
        }
    }

    /** One listened key: its name, the DJI key (created lazily), and where its value goes. */
    private inner class Binding<T : Any>(
        val name: String,
        info: DJIKeyInfo<T>,
        component: ComponentIndexType? = null,
        private val onValue: (T) -> Unit,
    ) {
        private val key: DJIKey<T> by lazy { if (component == null) KeyTools.createKey(info) else KeyTools.createKey(info, component) }

        fun supported(manager: KeyManager): Boolean = manager.isKeySupported(key)

        fun listen(manager: KeyManager) {
            manager.listen(
                key,
                holder,
                CommonCallbacks.KeyListener<T> { _, newValue -> if (newValue != null) received(this@Binding, newValue) },
            )
        }

        fun accept(value: T) = onValue(value)
    }

    private fun <T : Any> received(binding: Binding<T>, value: T) {
        val now = System.currentTimeMillis()
        val (first, sinceAttachMs) = synchronized(lock) {
            if (binding.name == "KeyAltitude" && (value as? Double)?.isFinite() == true) {
                altitudePoller.listenerSampleReceived()
            }
            rates.tick(binding.name, now)
            val status = if (ledger.value(binding.name, now)) ledger.status(binding.name) else null
            binding.accept(value)
            if (binding.name == "KeyAltitude") {
                altitudeReceivedAtMonotonicMs = (value as? Double)
                    ?.takeIf { height -> height.isFinite() }
                    ?.let { SystemClock.elapsedRealtime() }
            }
            Pair(status, ledger.attachedAtMs?.let { now - it })
        }
        if (first != null) {
            log("Telemetry key", "${binding.name} first value" + (sinceAttachMs?.let { " $it ms after its listener was registered" } ?: ""))
            record(binding.name, "first_value", first)
        }
        when (binding.name) {
            "KeyConnection" -> if (value as? Boolean == false) failActiveGimbalPitch("gimbal disconnected")
            "KeyAircraftVelocity" -> (value as? Velocity3D)?.let { velocity ->
                rawRecorder.recordVelocityNedMps(velocity.x, velocity.y, velocity.z)
            }
            "KeyAltitude" -> (value as? Double)?.let(rawRecorder::recordBarometricHeightM)
            "KeyUltrasonicHeight" -> (value as? Int)?.let(rawRecorder::recordUltrasonicHeightDm)
            "KeyAircraftAttitude" -> (value as? Attitude)?.let { attitude ->
                rawRecorder.recordAircraftAttitudeDegrees(attitude.yaw, attitude.pitch, attitude.roll)
                captureAlignment?.recordBodyAttitude(AttitudeSample(attitude.yaw, attitude.pitch, attitude.roll, SystemClock.elapsedRealtime()))
            }
            "KeyGimbalAttitude" -> (value as? Attitude)?.let { attitude ->
                val receivedAt = SystemClock.elapsedRealtime()
                rawRecorder.recordGimbalAttitudeDegrees(attitude.yaw, attitude.pitch, attitude.roll)
                captureAlignment?.recordGimbal(AttitudeSample(attitude.yaw, attitude.pitch, attitude.roll, receivedAt))
                processGimbalPitchUpdate { gimbalPitchCoordinator.observed(attitude.pitch, receivedAt) }
            }
        }
        publish()
    }

    private fun receivedHardwareAltitude(value: Double, receivedAtMonotonicMs: Long) {
        synchronized(lock) {
            altitude = value
            altitudeReceivedAtMonotonicMs = receivedAtMonotonicMs
        }
        publish()
    }

    private fun support(statuses: Map<String, TelemetryKeyStatus>, answer: (TelemetryKeyStatus) -> Boolean?): String {
        val yes = statuses.filterValues { answer(it) == true }.keys.joinToString().ifEmpty { "none" }
        val no = statuses.filterValues { answer(it) == false }.keys.joinToString().ifEmpty { "none" }
        return "yes for $yes; no for $no"
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
            telemetryKeys = ledger.snapshot(),
            yawDeg = yaw,
            virtualStickEnabled = virtualStickEnabled,
            authorityLostReason = authorityLostReason,
            localHeight = altitude?.let { height ->
                altitudeReceivedAtMonotonicMs?.let { receivedAtMs -> LocalHeightMeasurement(height, receivedAtMs) }
            },
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

    private sealed interface GimbalPitchStart {
        data class Begun(val active: GimbalPitchOperationCoordinator.Active<CommandReport?>) : GimbalPitchStart
        data object Busy : GimbalPitchStart
        data object Unavailable : GimbalPitchStart
    }

    private data class GimbalPitchReportEffect(
        val report: CommandReport?,
        val detail: String,
        val state: GimbalPitchReportState,
    )

    private enum class GimbalPitchReportState { EXECUTING, COMPLETED, FAILED }

    private companion object {
        const val MIN_GIMBAL_PITCH_DEGREES = -85.0
        const val MAX_GIMBAL_PITCH_DEGREES = 0.0
        const val GIMBAL_ROTATION_DURATION_SECONDS = 2.0
        const val GIMBAL_ACTION_TIMEOUT_MS = 2_000L
        const val HOVER_SPEED_M_S = 0.2
        const val PROVISIONAL_FIX_QUALITY = 0.5
        const val METRES_PER_DEGREE_LATITUDE = 110_574.0
        const val METRES_PER_DEGREE_LONGITUDE = 111_320.0
    }
}
