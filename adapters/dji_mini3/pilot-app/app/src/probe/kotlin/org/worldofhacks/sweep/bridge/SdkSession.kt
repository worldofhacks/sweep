package org.worldofhacks.sweep.bridge

import android.app.Application
import android.os.Build
import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.key.ProductKey
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.sdk.keyvalue.value.product.ProductType
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.manager.KeyManager
import dji.v5.manager.SDKManager
import dji.v5.manager.interfaces.SDKManagerCallback
import java.io.File
import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.camera.CameraExecutor
import org.worldofhacks.sweep.bridge.camera.DjiCameraPort
import org.worldofhacks.sweep.bridge.flight.DjiFlightPort
import org.worldofhacks.sweep.bridge.flight.FlightExecutor
import org.worldofhacks.sweep.bridge.flight.FlightNode
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.CommandExecutor
import org.worldofhacks.sweep.bridge.node.TelemetryKeyStatus
import org.worldofhacks.sweep.bridge.publish.BenchSink
import org.worldofhacks.sweep.bridge.session.AircraftIdentity
import org.worldofhacks.sweep.bridge.session.AircraftSession
import org.worldofhacks.sweep.bridge.session.ExportResult
import org.worldofhacks.sweep.bridge.session.ProbeReport
import org.worldofhacks.sweep.bridge.session.ProductConnection
import org.worldofhacks.sweep.bridge.session.SensorRecordingSession
import org.worldofhacks.sweep.bridge.session.SensorRelayContext
import org.worldofhacks.sweep.bridge.session.SessionModel
import org.worldofhacks.sweep.bridge.session.SessionState
import org.worldofhacks.sweep.bridge.video.DjiFpv
import org.worldofhacks.sweep.bridge.video.FlowCaptureProgress
import org.worldofhacks.sweep.bridge.video.FpvSessionHost

/**
 * MSDK v5 init, registration, and product identity (Phase B4), plus the telemetry listeners
 * of [ProbeAircraft] (Phase C2).
 *
 * The SDKManager init and registration flow, the registerApp-on-INITIALIZE_COMPLETE rule, and
 * the KeyProductType / KeyRcFirmwareInfo identity check are ported from techmexdev/drone-maps
 * app/src/probe/.../DjiProbeController.kt. New here: every product connect, disconnect, or
 * change bumps a connection generation in [SessionModel], and each identity read carries the
 * generation it was issued for, so a late key callback from a previous product is dropped
 * instead of overwriting the current one. The capture matrix and camera probing of the
 * original are not carried over.
 */
internal class SdkSession(private val application: Application) :
    AircraftSession,
    FpvSessionHost,
    SensorRecordingSession {
    private val model = SessionModel()
    private val sensorRawLock = Any()
    private var sensorRelayContext: SensorRelayContext? = null

    @Volatile
    private var sensorRaw: SensorRawSink? = null
    private var sensorRawUnavailable = false

    private val recorderConfigSha256 = SensorRawSink.recorderConfigSha256(
        BuildConfig.APPLICATION_ID,
        "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
        BuildConfig.AIRCRAFT,
    )
    private val rawRecorder = object : SensorRawRecorder {
        override fun recordVelocityNedMps(
            northMps: Double,
            eastMps: Double,
            downMps: Double,
        ): SensorRawAppendResult =
            sensorRaw?.recordVelocityNedMps(northMps, eastMps, downMps)
                ?: SensorRawAppendResult.NO_IDENTITY

        override fun recordBarometricHeightM(heightM: Double): SensorRawAppendResult =
            sensorRaw?.recordBarometricHeightM(heightM) ?: SensorRawAppendResult.NO_IDENTITY

        override fun recordUltrasonicHeightDm(heightDm: Int): SensorRawAppendResult =
            sensorRaw?.recordUltrasonicHeightDm(heightDm) ?: SensorRawAppendResult.NO_IDENTITY
    }
    private val probe = ProbeAircraft(
        phoneModel = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
        androidVersion = Build.VERSION.RELEASE ?: "",
        sdkVersion = { runCatching { SDKManager.getInstance().sdkVersion }.getOrNull().orEmpty() },
        log = { name, detail -> model.event(name, detail) },
        record = { key, event, status -> recordKey(key, event, status) },
        rawRecorder = rawRecorder,
    )

    override fun updateSensorRelayContext(context: SensorRelayContext?) {
        val old = synchronized(sensorRawLock) {
            if (sensorRelayContext == context && (context == null || sensorRaw != null)) {
                refreshSensorRawIdentityLocked()
                return
            }
            sensorRelayContext = context
            sensorRaw.also { sensorRaw = null }
        }
        old?.updateIdentity(null)
        old?.close()
        val oldMetrics = old?.metrics()
        oldMetrics?.let { model.event("Sensor raw log", "recorder closed; ${it.summary()}") }
        if (oldMetrics?.workerAlive == true) {
            synchronized(sensorRawLock) { sensorRawUnavailable = true }
            model.event("Sensor raw log", "recorder disabled for this app process after a close timeout")
        }
        if (context == null || synchronized(sensorRawLock) { sensorRawUnavailable }) return

        val opened = SensorRawSink.open(application.filesDir) { detail -> model.event("Sensor raw log", detail) }
        if (opened == null) {
            synchronized(sensorRawLock) { sensorRawUnavailable = true }
            return
        }
        var stale = false
        synchronized(sensorRawLock) {
            if (sensorRelayContext == context && sensorRaw == null) {
                sensorRaw = opened
                refreshSensorRawIdentityLocked()
            } else {
                stale = true
            }
        }
        if (stale) {
            opened.close()
            model.event("Sensor raw log", "stale recorder closed; ${opened.metrics().summary()}")
        }
    }

    private fun refreshSensorRawIdentity() {
        synchronized(sensorRawLock) { refreshSensorRawIdentityLocked() }
    }

    private fun refreshSensorRawIdentityLocked() {
        val relay = sensorRelayContext
        val state = model.current
        val productId = state.productId
        val next = if (relay == null || state.product != ProductConnection.CONNECTED || productId == null) {
            null
        } else {
            val identity = state.identity
            SensorRawIdentity(
                session = relay.session,
                productId = productId,
                droneId = relay.droneId,
                connectionGeneration = state.generation,
                connectionEpoch = relay.connectionEpoch,
                productType = boundedSensorProvenance(identity.productType),
                aircraftFirmware = boundedSensorProvenance(identity.aircraftFirmware),
                rcFirmware = boundedSensorProvenance(identity.rcFirmware),
                sdkVersion = boundedSensorProvenance(runCatching { SDKManager.getInstance().sdkVersion }.getOrNull()),
                recorderConfigSha256 = recorderConfigSha256,
            )
        }
        sensorRaw?.updateIdentity(next)
    }

    private fun clearSensorRawIdentity() {
        synchronized(sensorRawLock) { sensorRaw?.updateIdentity(null) }
    }

    /**
     * Phase C follow-up: `filesDir/bench/telemetry-keys-<stamp>.jsonl`, one `telemetry_key`
     * record per key and listener event, so the first on-phone run says which keys reported
     * and when. Opened on the first record, that is when the listeners register; the path
     * is in the SDK events.
     */
    private val keyBench: BenchSink? by lazy {
        BenchSink.open(application.filesDir, "telemetry-keys").also { sink ->
            model.event("Telemetry key log", sink?.file?.absolutePath ?: "could not open bench/telemetry-keys log")
        }
    }

    private fun recordKey(key: String, event: String, status: TelemetryKeyStatus) {
        val sink = keyBench ?: return
        synchronized(sink) { sink.recorder.telemetryKey(key, event, status.supportedAtAttach, status.supportedAtConnect, status.firstValueAtMs) }
    }

    // Phase G: the camera and media path on the DJI camera, gimbal, and media manager. Its
    // facts feed the capabilities frame through the aircraft snapshot; the relay link binds
    // its frame sink when it starts. Files land under filesDir/captures/<capture_id>/.
    private val cameraPort = DjiCameraPort { name, detail -> model.event(name, detail) }
    override val camera: CameraExecutor = CameraExecutor(
        cameraPort,
        probe,
        File(application.filesDir, "captures"),
        log = { line -> model.event("Camera", line) },
        onFacts = probe::setCamera,
    )

    // Phase D hook: local FPV, yaw, and codec evidence (org.worldofhacks.sweep.bridge.video).
    override val fpv: DjiFpv = DjiFpv(
        application.filesDir,
        AndroidPhoneStatus(application),
        { name, detail -> model.event(name, detail) },
        captureProgress = FlowCaptureProgress(camera.progress),
    )

    override val state: StateFlow<SessionState> = model.state

    override val aircraft: AircraftSource
        get() = probe

    override val executor: CommandExecutor
        get() = flightExecutor

    // Phase E hook: DjiFlightPort and FlightExecutor run the Virtual Stick loop. The port's
    // takeover signals attach when ProbeAircraft attaches (SDK registered), and the flight
    // controller's failsafe setting is read, never changed, on every product connection.
    private val port = DjiFlightPort { name, detail -> model.event(name, detail) }
    private val flightExecutor = FlightExecutor(port, probe, fallback = camera, log = { line -> model.event("Flight", line) })
    override val flight: FlightNode = FlightNode(
        flightExecutor,
        probe,
        application.filesDir,
        onStatus = { status -> probe.setFlightStatus(status.virtualStickEnabled, status.authorityLostReason) },
        log = { line -> model.event("Probe", line) },
    )

    init {
        probe.onAttached = {
            port.attach(flightExecutor)
            cameraPort.attach()
        }
        probe.onProductConnected = {
            port.onProductConnected()
            cameraPort.productConnected(true)
        }
    }

    private val callback = object : SDKManagerCallback {
        override fun onRegisterSuccess() {
            model.registerSucceeded()
            probe.attach()
            fpv.attach()
        }

        override fun onRegisterFailure(error: IDJIError) {
            model.registerFailed(describe(error))
        }

        override fun onProductDisconnect(productId: Int) {
            clearSensorRawIdentity()
            model.productDisconnected(productId)
            probe.productConnected(false)
            cameraPort.productConnected(false)
            fpv.productConnected(false)
            probe.updateIdentity(model.current.identity)
        }

        override fun onProductConnect(productId: Int) {
            val generation = model.productConnected(productId)
            refreshSensorRawIdentity()
            probe.attach()
            fpv.attach()
            probe.productConnected(true)
            fpv.productConnected(true)
            queryIdentity(generation)
        }

        override fun onProductChanged(productId: Int) {
            clearSensorRawIdentity()
            val generation = model.productChanged(productId)
            refreshSensorRawIdentity()
            probe.productConnected(true)
            fpv.productConnected(true)
            queryIdentity(generation)
        }

        override fun onInitProcess(event: DJISDKInitEvent, totalProcess: Int) {
            model.initProgress("$event ($totalProcess)")
            if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                model.registering()
                SDKManager.getInstance().registerApp()
            }
        }

        override fun onDatabaseDownloadProgress(current: Long, total: Long) {
            model.event("DJI database download", "$current of $total bytes")
        }
    }

    init {
        model.initProgress("SDKManager.init")
        SDKManager.getInstance().init(application.applicationContext, callback)
    }

    /** Reads the identity keys for one connection generation; stale results are dropped by the model. */
    private fun queryIdentity(generation: Long) {
        read(generation, "Product identity", KeyTools.createKey(ProductKey.KeyProductType)) { productType ->
            val mini3 = productType == ProductType.DJI_MINI_3
            val detail = "${productType.name} (${productType.value()})" + if (mini3) "" else " UNEXPECTED"
            detail to { identity -> identity.copy(productType = productType.name, isMini3 = mini3) }
        }
        read(generation, "Aircraft firmware", KeyTools.createKey(ProductKey.KeyFirmwareVersion)) { firmware ->
            firmware.ifBlank { "returned empty" } to { identity -> identity.copy(aircraftFirmware = firmware) }
        }
        read(
            generation,
            "Remote controller identity",
            KeyTools.createKey(RemoteControllerKey.KeyRcFirmwareInfo, ComponentIndexType.LEFT_OR_MAIN),
        ) { info ->
            val type = info.curFirmwareType?.name
            val versions = info.firmwareDesc.orEmpty()
                .mapNotNull { description -> description.firmwareVersion?.takeIf { it.isNotBlank() } }
                .distinct()
            "RC firmware profile ${type ?: "unknown"}; versions ${versions.ifEmpty { listOf("none") }}" to { identity ->
                identity.copy(rcFirmwareType = type, rcFirmwareVersions = versions)
            }
        }
        read(
            generation,
            "Remote controller firmware",
            KeyTools.createKey(RemoteControllerKey.KeyFirmwareVersion, ComponentIndexType.LEFT_OR_MAIN),
        ) { firmware ->
            firmware.ifBlank { "returned empty" } to { identity -> identity.copy(rcFirmware = firmware) }
        }
    }

    private fun <T : Any> read(
        generation: Long,
        name: String,
        key: DJIKey<T>,
        onValue: (T) -> Pair<String, (AircraftIdentity) -> AircraftIdentity>,
    ) {
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            model.identity(generation, name, "key not supported") { it }
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T) {
                    val (detail, transform) = onValue(value)
                    if (model.identity(generation, name, detail, transform)) {
                        probe.updateIdentity(model.current.identity)
                        refreshSensorRawIdentity()
                    }
                }

                override fun onFailure(error: IDJIError) {
                    model.identity(generation, name, "read failed: ${describe(error)}") { it }
                }
            },
        )
    }

    private fun describe(error: IDJIError): String =
        "${error.errorType()} ${error.errorCode()} ${error.description().orEmpty()}".trim()

    override fun exportProbeReport(): ExportResult = ProbeReport.write(
        directory = application.filesDir,
        state = model.current,
        environment = ProbeReport.Environment(
            aircraftVariant = BuildConfig.AIRCRAFT,
            applicationId = BuildConfig.APPLICATION_ID,
            appVersion = "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
            msdkVersion = SDKManager.getInstance().sdkVersion,
            phone = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
            android = "${Build.VERSION.RELEASE} / API ${Build.VERSION.SDK_INT} / build ${Build.DISPLAY}",
        ),
        exportedAtMs = System.currentTimeMillis(),
    )
}
