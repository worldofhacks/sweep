package org.worldofhacks.sweep.bridge.camera

import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import kotlin.math.abs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.MediaFileRecord
import org.worldofhacks.sweep.bridge.core.frames.RetrievalStatus
import org.worldofhacks.sweep.bridge.core.frames.WireIntrinsics
import org.worldofhacks.sweep.bridge.core.frames.WirePose
import org.worldofhacks.sweep.bridge.core.video.CapturePhase
import org.worldofhacks.sweep.bridge.core.video.CaptureProgress
import org.worldofhacks.sweep.bridge.core.video.FlightOverlay
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.CommandExecutor
import org.worldofhacks.sweep.bridge.node.CommandReport
import org.worldofhacks.sweep.bridge.node.NodeLog

/** Timing and tolerances of the camera path; the gimbal tolerance matches the arbiter's `max_capture_gimbal_error_deg`. */
data class CameraConfig(
    val gimbalToleranceDeg: Double = 1.0,
    val gimbalPollMs: Long = 100,
    val gimbalTimeoutMs: Long = 6_000,
    val modeTimeoutMs: Long = 8_000,
    val shutterTimeoutMs: Long = 8_000,
    /** How long to wait for the camera to announce the new file after the shutter answered. */
    val fileAnnounceTimeoutMs: Long = 8_000,
    val downloadTimeoutMs: Long = 120_000,
    /** `executing` progress acknowledgements during a download, kept under the relay's command TTL. */
    val progressIntervalMs: Long = 1_000,
    /** The node's own storage floor; the arbiter applies its own `min_capture_storage_bytes` on top. */
    val minStorageBytes: Long = 1_000_000,
    /** The only pattern this node drives; the overlay counts frames against it. */
    val framesPerCapture: Int = 8,
)

/** One captured file as the camera path knows it, for the capture card and the media records. */
data class CapturedFile(
    val captureId: String,
    val fileId: String,
    val frameNumber: Int,
    val camera: CameraFile,
    val record: MediaFileRecord,
    /** The file on the phone once retrieved. */
    val path: String?,
)

/** The camera path's observable state for the capture card. */
data class CameraStatus(
    val phase: String = "idle",
    val activeCommandId: String? = null,
    val activeOperation: String? = null,
    val gimbalPitchDeg: Double? = null,
    val files: List<CapturedFile> = emptyList(),
    val lastEvent: String? = null,
)

/**
 * Runs the camera and media commands of a `capture_room` plan (issue #43, Phase G) on one
 * dedicated thread, in wire order: `camera_capabilities`, `set_gimbal_pitch`, `camera_ready`,
 * then per frame `capture_photo` and `retrieve_media`; `capture_panorama` is refused as
 * `camera_unsupported` because a native panorama yaws the aircraft under the flight
 * controller, outside the Virtual Stick loop and the arbiter's pose lock. The flight loop
 * owns `rotate_to` and every other motion; nothing here enables Virtual Stick.
 *
 * Each captured file becomes a `media_file` record (`pending`, all-zero checksum, the
 * aircraft file as `storage_ref`) sent before the shutter command completes, and a
 * `completed` record (SHA-256 of the downloaded bytes, the phone file as `storage_ref`)
 * sent before the retrieval command completes; both carry the aircraft pose, heading, and
 * gimbal pitch at the shutter. The command wire carries only `capture_id`, so the node does
 * not know the room or pattern: the relay composes the closing `capture_bundle` from the
 * dispatcher's validated result.
 */
class CameraExecutor(
    private val port: CameraPort,
    private val aircraft: AircraftSource,
    private val root: File,
    private val clock: Clock = SystemClock,
    private val config: CameraConfig = CameraConfig(),
    private val log: NodeLog = NodeLog { },
    /** Pushes the probed camera facts into the flavor's aircraft snapshot for the `capabilities` frame. */
    private val onFacts: (CameraProbe) -> Unit = {},
) : CommandExecutor, CaptureReadinessSource, AutoCloseable {
    private val worker = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "camera-loop").apply { isDaemon = true } }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    /** Bound by the app when the relay link starts; null while no link exists. */
    @Volatile
    var frames: NodeFrameSink? = null

    private val _status = MutableStateFlow(CameraStatus())
    val status: StateFlow<CameraStatus> = _status.asStateFlow()

    private val _progress = MutableStateFlow(CaptureProgress())
    val progress: StateFlow<CaptureProgress> = _progress.asStateFlow()

    /** The port's live camera and storage facts, for the capture card. */
    val facts: StateFlow<CameraFacts>
        get() = port.facts

    private val lock = Any()
    private val ledger = LinkedHashMap<String, CapturedFile>()
    private val frameCounts = HashMap<String, Int>()
    private var activeCaptureId: String? = null
    private val announced = LinkedBlockingQueue<CameraFile>()

    init {
        port.setFileListener { file -> announced.offer(file) }
        onFacts(port.facts.value.toProbe())
        scope.launch { port.facts.collect { facts -> onFacts(facts.toProbe()) } }
    }

    override fun execute(command: CommandFrame, report: CommandReport) {
        worker.execute {
            try {
                run(command, report)
            } catch (error: RuntimeException) {
                log.log("camera command ${command.commandId} failed: $error")
                report.failed("camera_failure", "the camera path could not run ${command.operation.wire}: $error [terminal]")
                finish()
            }
        }
    }

    override fun current(): CaptureReadinessBody {
        val facts = port.facts.value
        val snapshot = aircraft.snapshot.value
        val captureId = synchronized(lock) { activeCaptureId }
        val accepted = _progress.value.acceptedHeadingsDeg
        return CaptureReadinessBody(
            roomId = null,
            captureId = captureId,
            cameraOk = cameraOk(facts, snapshot.aircraftConnected),
            storageOk = storageOk(facts),
            motionOk = true,
            imageQualityOk = true,
            coverageMissing = missingHeadings(accepted),
            nextHeadingDeg = null,
            suggestedDelta = null,
        )
    }

    override fun close() {
        scope.cancel()
        port.setFileListener(null)
        worker.shutdownNow()
    }

    // ---- one command at a time on the camera thread ----

    private fun run(command: CommandFrame, report: CommandReport) {
        begin(command)
        when (val args = command.args) {
            CommandArgs.CameraCapabilities -> capabilities(report)
            is CommandArgs.SetGimbalPitch -> gimbal(args.pitchMdeg / 1000.0, report)
            CommandArgs.CameraReady -> ready(report)
            is CommandArgs.CapturePhoto -> photo(args.captureId, report)
            is CommandArgs.CapturePanorama -> {
                report.failed(
                    CAMERA_UNSUPPORTED,
                    "native panorama is not driven by this node: the aircraft would yaw under the flight controller outside " +
                        "the Virtual Stick loop and the arbiter's pose lock; reconstruct_8 is the working pattern [terminal]",
                )
                finish()
            }
            is CommandArgs.RetrieveMedia -> retrieve(args.fileId, report)
            else -> {
                report.failed(UNSUPPORTED, "${command.operation.wire} is not a camera operation [terminal]")
                finish()
            }
        }
    }

    private fun capabilities(report: CommandReport) {
        report.executing("reading camera and storage facts")
        val result = await(config.modeTimeoutMs) { done -> port.refreshFacts(done) }
        val facts = port.facts.value
        onFacts(facts.toProbe())
        if (result is PortResult.Failed) {
            log.log("camera facts refresh failed: ${result.detail}; reporting the last known facts")
        }
        val storage = facts.storageRemainingBytes?.let { "${it / 1_000_000} MB free" } ?: "storage unreported"
        val panorama = if (facts.panoramaAdvertised.isEmpty()) "no native panorama advertised" else "camera advertises ${facts.panoramaAdvertised} (not driven by this node)"
        event("capabilities: camera ${if (facts.cameraConnected) "connected" else "absent"}, $storage, gimbal pitch ${facts.gimbalPitchMinDeg ?: "?"}..${facts.gimbalPitchMaxDeg ?: "?"} deg, $panorama")
        report.completed("camera facts reported: $storage; $panorama")
        finish()
    }

    private fun gimbal(targetDeg: Double, report: CommandReport) {
        val facts = port.facts.value
        val min = facts.gimbalPitchMinDeg
        val max = facts.gimbalPitchMaxDeg
        if (min != null && max != null && (targetDeg < min || targetDeg > max)) {
            report.failed(CAMERA_FAILURE, "gimbal pitch ${fmt(targetDeg)} deg is outside the reported range ${fmt(min)}..${fmt(max)} [terminal]")
            finish()
            return
        }
        if (port.gimbalPitchDeg() == null) {
            report.failed(CAMERA_UNSUPPORTED, "the gimbal has not reported an attitude; cannot confirm a pitch [retryable]")
            finish()
            return
        }
        report.executing("gimbal pitch to ${fmt(targetDeg)} deg")
        when (val result = await(config.gimbalTimeoutMs) { done -> port.setGimbalPitch(targetDeg, done) }) {
            is PortResult.Failed -> {
                report.failed(CAMERA_FAILURE, "gimbal rotation refused: ${result.detail} [retryable]")
                finish()
                return
            }
            PortResult.Ok -> Unit
        }
        val deadline = clock.nowMs() + config.gimbalTimeoutMs
        var reported = port.gimbalPitchDeg()
        while (clock.nowMs() < deadline) {
            reported = port.gimbalPitchDeg()
            if (reported != null && abs(reported - targetDeg) <= config.gimbalToleranceDeg) {
                _status.update { it.copy(gimbalPitchDeg = reported) }
                event("gimbal pitch ${fmt(reported)} deg (target ${fmt(targetDeg)})")
                report.completed("gimbal pitch ${fmt(reported)} deg, target ${fmt(targetDeg)} within ${fmt(config.gimbalToleranceDeg)} deg")
                finish()
                return
            }
            Thread.sleep(config.gimbalPollMs)
        }
        _status.update { it.copy(gimbalPitchDeg = reported) }
        report.failed(CAMERA_FAILURE, "gimbal pitch not reached: reported ${reported?.let(::fmt) ?: "none"} deg, target ${fmt(targetDeg)} deg [retryable]")
        finish()
    }

    private fun ready(report: CommandReport) {
        report.executing("camera to photo mode; checking storage")
        val mode = await(config.modeTimeoutMs) { done -> port.enterPhotoMode(done) }
        await(config.modeTimeoutMs) { done -> port.refreshFacts(done) }
        val facts = port.facts.value
        onFacts(facts.toProbe())
        val connected = aircraft.snapshot.value.aircraftConnected
        val cameraOk = mode is PortResult.Ok && cameraOk(facts, connected)
        val storageOk = storageOk(facts)
        val body = current().copy(cameraOk = cameraOk, storageOk = storageOk)
        val sent = frames?.sendCaptureReadiness(body) ?: false
        val reasons = buildList {
            if (mode is PortResult.Failed) add("photo mode: ${mode.detail}")
            if (!connected) add("aircraft not connected")
            if (!facts.cameraConnected) add("camera not connected")
            if (!facts.photoMode) add("camera not in photo mode")
            if (!facts.storageInserted) add("no storage inserted")
            if (!storageOk) add("storage ${facts.storageRemainingBytes ?: "unreported"} bytes below ${config.minStorageBytes}")
        }
        if (cameraOk && storageOk) {
            event("camera ready: photo mode, ${facts.storageRemainingBytes?.let { it / 1_000_000 } ?: "?"} MB free" + if (sent) "" else " (readiness frame not sent: link not joined)")
            report.completed("camera ready: photo mode, storage ok" + if (sent) ", capture_readiness sent" else "")
        } else {
            report.failed(CAMERA_NOT_READY, reasons.joinToString("; ") + " [retryable]")
        }
        finish()
    }

    private fun photo(captureId: String, report: CommandReport) {
        val identity = frames?.identity()
        if (identity == null) {
            report.failed(CAMERA_FAILURE, "relay link is not joined; the media record cannot name a connection epoch [retryable]")
            finish()
            return
        }
        val facts = port.facts.value
        val snapshot = aircraft.snapshot.value
        if (!cameraOk(facts, snapshot.aircraftConnected)) {
            report.failed(CAMERA_NOT_READY, "camera is not ready for a photo (run camera_ready first) [retryable]")
            finish()
            return
        }
        val frameNumber = synchronized(lock) {
            activeCaptureId = captureId
            val next = (frameCounts[captureId] ?: 0) + 1
            frameCounts[captureId] = next
            next
        }
        _progress.update { it.copy(phase = CapturePhase.Capturing(frameNumber, maxOf(frameNumber, config.framesPerCapture))) }
        announced.clear()
        // Pose, heading, and gimbal at the shutter: the relay validates these against the approved pose.
        val pose = WirePose(snapshot.x, snapshot.y, snapshot.z)
        val yaw = FlightOverlay.heading(snapshot.yawDeg)
        val gimbal = port.gimbalPitchDeg() ?: 0.0
        report.executing("shutter for $captureId frame $frameNumber at heading ${fmt(yaw)} deg")
        when (val shutter = await(config.shutterTimeoutMs) { done -> port.shootPhoto(done) }) {
            is PortResult.Failed -> {
                report.failed(CAMERA_FAILURE, "shutter refused: ${shutter.detail} [retryable]")
                finish()
                return
            }
            PortResult.Ok -> Unit
        }
        val file = announced.poll(config.fileAnnounceTimeoutMs, TimeUnit.MILLISECONDS)
        if (file == null) {
            report.failed(CAMERA_FAILURE, "the camera did not announce a new file within ${config.fileAnnounceTimeoutMs} ms [retryable]")
            finish()
            return
        }
        val fileId = "$captureId-frame-%02d".format(frameNumber)
        val record = MediaFileRecord(
            captureId = captureId,
            fileId = fileId,
            timestampMs = clock.nowMs(),
            droneId = identity.droneId,
            connectionEpoch = identity.connectionEpoch,
            pose = pose,
            actualYawDeg = yaw,
            gimbalPitchDeg = gimbal,
            intrinsics = WireIntrinsics(facts.photoWidthPx, facts.photoHeightPx, facts.horizontalFovDeg, "rectilinear"),
            checksumSha256 = MediaFileRecord.PENDING_CHECKSUM,
            storageRef = "aircraft://camera/${file.index}/${file.name}",
            retrievalStatus = RetrievalStatus.PENDING,
        )
        val captured = CapturedFile(captureId, fileId, frameNumber, file, record, path = null)
        synchronized(lock) { ledger[fileId] = captured }
        _status.update { it.copy(files = synchronized(lock) { ledger.values.toList() }) }
        _progress.update { it.copy(acceptedHeadingsDeg = it.acceptedHeadingsDeg + yaw) }
        val sent = frames?.sendMediaFile(record) ?: false
        if (!sent) {
            report.failed(CAMERA_FAILURE, "media_file for $fileId could not be sent: relay link not joined [retryable]")
            finish()
            return
        }
        event("captured $fileId: ${file.name} (${file.sizeBytes} bytes) at heading ${fmt(yaw)} deg, gimbal ${fmt(gimbal)} deg")
        report.completed("captured $fileId as ${file.name}, ${file.sizeBytes} bytes on the aircraft; media_file pending")
        finish(keepProgress = true)
    }

    private fun retrieve(fileId: String, report: CommandReport) {
        val captured = synchronized(lock) { ledger[fileId] }
        if (captured == null) {
            report.failed(DOWNLOAD_FAILURE, "no file $fileId was captured in this connection epoch [terminal]")
            finish()
            return
        }
        val identity = frames?.identity()
        if (identity == null || identity.connectionEpoch != captured.record.connectionEpoch) {
            report.failed(DOWNLOAD_FAILURE, "the connection epoch changed since $fileId was captured [terminal]")
            finish()
            return
        }
        val position = synchronized(lock) { ledger.values.count { it.captureId == captured.captureId && it.frameNumber <= captured.frameNumber } }
        val total = synchronized(lock) { ledger.values.count { it.captureId == captured.captureId } }
        _progress.update { it.copy(phase = CapturePhase.Downloading(position, total)) }
        val target = File(File(root, safe(captured.captureId)), safe(captured.camera.name))
        report.executing("downloading ${captured.camera.name} (${captured.camera.sizeBytes} bytes) over the RC link")
        val outcome = CompletableFuture<String?>()
        var lastProgressAt = clock.nowMs()
        port.download(
            captured.camera,
            target,
            object : DownloadListener {
                override fun progress(bytes: Long, total: Long) {
                    val now = clock.nowMs()
                    if (total > 0 && now - lastProgressAt >= config.progressIntervalMs) {
                        lastProgressAt = now
                        report.executing("downloading ${captured.camera.name}: ${(bytes * 100 / total)}%")
                    }
                }

                override fun finished() {
                    outcome.complete(null)
                }

                override fun failed(detail: String) {
                    outcome.complete(detail)
                }
            },
        )
        val failure = try {
            outcome.get(config.downloadTimeoutMs, TimeUnit.MILLISECONDS)
        } catch (_: TimeoutException) {
            "download did not finish within ${config.downloadTimeoutMs} ms"
        } catch (error: ExecutionException) {
            error.cause?.message ?: error.message ?: "download failed"
        }
        await(config.modeTimeoutMs) { done -> port.leaveMediaMode(done) }
        if (failure != null) {
            report.failed(DOWNLOAD_FAILURE, "$failure [retryable]")
            finish()
            return
        }
        val checksum = try {
            sha256(target)
        } catch (error: java.io.IOException) {
            report.failed(DOWNLOAD_FAILURE, "downloaded file unreadable: ${error.message} [retryable]")
            finish()
            return
        }
        val size = target.length()
        val record = captured.record.copy(
            timestampMs = clock.nowMs().coerceAtLeast(captured.record.timestampMs + 1),
            checksumSha256 = checksum,
            storageRef = target.toURI().toString(),
            retrievalStatus = RetrievalStatus.COMPLETED,
        )
        val retrieved = captured.copy(record = record, path = target.absolutePath)
        synchronized(lock) { ledger[fileId] = retrieved }
        _status.update { it.copy(files = synchronized(lock) { ledger.values.toList() }) }
        if (frames?.sendMediaFile(record) != true) {
            report.failed(DOWNLOAD_FAILURE, "media_file for $fileId could not be sent: relay link not joined [retryable]")
            finish()
            return
        }
        event("retrieved $fileId: ${target.absolutePath}, $size bytes, sha256 $checksum")
        report.completed("retrieved $fileId to ${target.absolutePath}: $size bytes, sha256 $checksum")
        finish(keepProgress = true)
    }

    // ---- helpers ----

    private fun cameraOk(facts: CameraFacts, aircraftConnected: Boolean): Boolean =
        aircraftConnected && facts.cameraConnected && facts.photoMode

    private fun storageOk(facts: CameraFacts): Boolean =
        facts.storageInserted && (facts.storageRemainingBytes ?: 0L) >= config.minStorageBytes

    /** Headings of the `reconstruct_8` sectors no accepted frame falls into yet, for the compass. */
    private fun missingHeadings(accepted: List<Double>): List<Double> {
        val width = 360.0 / config.framesPerCapture
        return (0 until config.framesPerCapture)
            .map { it * width }
            .filter { start -> accepted.none { heading -> FlightOverlay.heading(heading) >= start && FlightOverlay.heading(heading) < start + width } }
    }

    private fun begin(command: CommandFrame) {
        _status.update { it.copy(phase = command.operation.wire, activeCommandId = command.commandId, activeOperation = command.operation.wire) }
    }

    private fun finish(keepProgress: Boolean = false) {
        _status.update { it.copy(phase = "idle", activeCommandId = null, activeOperation = null) }
        if (!keepProgress) _progress.update { it.copy(phase = CapturePhase.Idle) }
    }

    private fun event(line: String) {
        log.log("camera: $line")
        _status.update { it.copy(lastEvent = line) }
    }

    private fun await(timeoutMs: Long, start: ((PortResult) -> Unit) -> Unit): PortResult {
        val future = CompletableFuture<PortResult>()
        start { result -> future.complete(result) }
        return try {
            future.get(timeoutMs, TimeUnit.MILLISECONDS)
        } catch (_: TimeoutException) {
            PortResult.Failed("no answer within $timeoutMs ms")
        } catch (error: ExecutionException) {
            PortResult.Failed(error.cause?.message ?: error.message ?: "port failure")
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { stream ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = stream.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun safe(value: String): String = value.map { if (it.isLetterOrDigit() || it in "-_.") it else '_' }.joinToString("").take(200)

    private fun fmt(value: Double): String = "%.1f".format(value)

    companion object {
        const val CAMERA_UNSUPPORTED = "camera_unsupported"
        const val CAMERA_NOT_READY = "camera_not_ready"
        const val CAMERA_FAILURE = "camera_failure"
        const val DOWNLOAD_FAILURE = "download_failure"
        const val UNSUPPORTED = "unsupported"
    }
}
