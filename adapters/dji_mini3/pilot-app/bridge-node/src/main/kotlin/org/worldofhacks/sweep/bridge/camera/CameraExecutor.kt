package org.worldofhacks.sweep.bridge.camera

import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import java.util.concurrent.atomic.AtomicLong
import kotlin.math.abs
import kotlin.math.sqrt
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
import org.worldofhacks.sweep.bridge.node.AircraftSnapshot
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
    /** Hard bound for the connection-epoch file ledger and the Capture card projection. */
    val maxTrackedFiles: Int = 64,
    /** Reject an announced photo above this size before it enters the retrieval ledger. */
    val maxFileBytes: Long = 64L * 1024 * 1024,
    /** Bound completed and temporary capture bytes retained below the app's capture root. */
    val maxStoredBytes: Long = 512L * 1024 * 1024,
    /** A shutter needs a measured near-stationary aircraft state, not default zero velocity. */
    val maxCaptureSpeedMS: Double = 0.10,
    /** Maximum age of each position, velocity, and attitude measurement used at the shutter. */
    val maxTelemetryAgeMs: Long = 1_000,
    /** Camera creation times commonly have whole-second precision. */
    val fileTimestampSkewMs: Long = 2_000,
) {
    init {
        require(
            minOf(
                gimbalPollMs,
                gimbalTimeoutMs,
                modeTimeoutMs,
                shutterTimeoutMs,
                fileAnnounceTimeoutMs,
                downloadTimeoutMs,
                progressIntervalMs,
            ) > 0,
        ) { "camera timeouts and intervals must be positive" }
        require(gimbalToleranceDeg >= 0.0 && gimbalToleranceDeg.isFinite())
        require(minStorageBytes >= 0)
        require(framesPerCapture > 0)
        require(maxTrackedFiles > 0)
        require(maxFileBytes > 0)
        require(maxStoredBytes > 0)
        require(maxCaptureSpeedMS >= 0.0 && maxCaptureSpeedMS.isFinite())
        require(maxTelemetryAgeMs > 0)
        require(fileTimestampSkewMs >= 0)
    }
}

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

private data class CaptureEvidence(
    val timestampMs: Long,
    val pose: WirePose,
    val yawDeg: Double,
    val gimbalPitchDeg: Double,
    val intrinsics: WireIntrinsics,
)

private sealed interface CaptureEvidenceResult {
    data class Available(val value: CaptureEvidence) : CaptureEvidenceResult

    data class Missing(val detail: String) : CaptureEvidenceResult
}

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
 * not claim room or pattern authority: the relay composes the closing `capture_bundle` from
 * the dispatcher's validated plan and these immutable file records.
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
    private var ledgerIdentity: NodeIdentity? = null
    private var activeCaptureId: String? = null
    private val announced = LinkedBlockingQueue<CameraFile>()
    private val downloadGeneration = AtomicLong()

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
        val identity = frames?.identity()
        reconcileLedgerIdentity(identity)
        val facts = port.facts.value
        val snapshot = aircraft.snapshot.value
        val captureId = synchronized(lock) { activeCaptureId }
        val accepted = _progress.value.acceptedHeadingsDeg
        return CaptureReadinessBody(
            roomId = null,
            captureId = captureId,
            poseOk = capturePoseAvailable(snapshot, clock.nowMs()),
            clearanceOk = false,
            cameraOk = captureCameraOk(facts, snapshot),
            storageOk = storageOk(facts),
            motionOk = captureMotionOk(snapshot, clock.nowMs()),
            // No image-analysis result exists yet; never pre-approve image quality.
            imageQualityOk = false,
            coverageMissing = missingHeadings(accepted),
            nextHeadingDeg = null,
            suggestedDelta = null,
        )
    }

    override fun close() {
        downloadGeneration.incrementAndGet()
        scope.cancel()
        port.setFileListener(null)
        worker.shutdownNow()
        runCatching { worker.awaitTermination(2, TimeUnit.SECONDS) }
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
        val refresh = await(config.modeTimeoutMs) { done -> port.refreshFacts(done) }
        val facts = port.facts.value
        onFacts(facts.toProbe())
        val snapshot = aircraft.snapshot.value
        val fresh = refresh is PortResult.Ok
        val cameraOk = mode is PortResult.Ok && fresh && captureCameraOk(facts, snapshot)
        val storageOk = fresh && storageOk(facts)
        val body = current().copy(cameraOk = cameraOk, storageOk = storageOk)
        val sent = frames?.sendCaptureReadiness(body) ?: false
        val reasons = buildList {
            if (mode is PortResult.Failed) add("photo mode: ${mode.detail}")
            if (refresh is PortResult.Failed) add("camera facts: ${refresh.detail}")
            if (!snapshot.aircraftConnected) add("aircraft not connected")
            if (!facts.cameraConnected) add("camera not connected")
            if (!facts.photoMode) add("camera not in photo mode")
            if (port.gimbalPitchDeg() == null) add("gimbal attitude unreported")
            if (!facts.photoDimensionsReported) add("photo dimensions unreported")
            if (snapshot.hardware.measuredHfovDeg == null) add("measured horizontal field of view unreported")
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
        reconcileLedgerIdentity(identity)
        if (identity == null) {
            report.failed(CAMERA_FAILURE, "relay link is not joined; the media record cannot name a connection epoch [retryable]")
            finish()
            return
        }
        if (synchronized(lock) { ledger.size >= config.maxTrackedFiles }) {
            report.failed(
                CAPTURE_LIMIT_EXCEEDED,
                "the connection-epoch camera ledger is limited to ${config.maxTrackedFiles} files; reconnect before another capture [terminal]",
            )
            finish()
            return
        }
        when (val evidence = captureEvidence()) {
            is CaptureEvidenceResult.Missing -> {
                report.failed(CAMERA_NOT_READY, "capture evidence unavailable before shutter: ${evidence.detail} [retryable]")
                finish()
                return
            }
            is CaptureEvidenceResult.Available -> Unit
        }
        val newCapture = synchronized(lock) {
            val changed = activeCaptureId != captureId
            activeCaptureId = captureId
            changed
        }
        if (newCapture) {
            _progress.update { it.copy(acceptedHeadingsDeg = emptyList()) }
        }
        // Do not consume the frame number until a correlated file and its pending record exist.
        val frameNumber = synchronized(lock) { (frameCounts[captureId] ?: 0) + 1 }
        _progress.update {
            it.copy(
                phase = CapturePhase.Capturing(
                    frameNumber,
                    maxOf(frameNumber, config.framesPerCapture),
                ),
            )
        }
        announced.clear()
        val shutterStartedAt = clock.nowMs()
        val preflightYaw = aircraft.snapshot.value.yawDeg
        report.executing(
            "shutter for $captureId frame $frameNumber at heading ${fmt(FlightOverlay.heading(preflightYaw))} deg",
        )
        when (val shutter = await(config.shutterTimeoutMs) { done -> port.shootPhoto(done) }) {
            is PortResult.Failed -> {
                report.failed(CAMERA_FAILURE, "shutter refused: ${shutter.detail} [retryable]")
                finish()
                return
            }
            PortResult.Ok -> Unit
        }
        // Snapshot immediately when the shutter action reports success. Waiting for the
        // camera's file announcement can take seconds and must not move this timestamp.
        val atShutter = when (val evidence = captureEvidence()) {
            is CaptureEvidenceResult.Available -> evidence.value
            is CaptureEvidenceResult.Missing -> {
                report.failed(CAMERA_FAILURE, "photo fired but its shutter evidence is incomplete: ${evidence.detail} [terminal]")
                finish()
                return
            }
        }
        val file = awaitNewFile(shutterStartedAt)
        if (file == null) {
            report.failed(
                CAMERA_FAILURE,
                "the camera did not announce a new, correlated file within ${config.fileAnnounceTimeoutMs} ms [retryable]",
            )
            finish()
            return
        }
        val trackedBytes = synchronized(lock) { ledger.values.sumOf { it.camera.sizeBytes } }
        if (
            file.sizeBytes > config.maxFileBytes ||
            trackedBytes > config.maxStoredBytes - file.sizeBytes
        ) {
            report.failed(
                CAPTURE_LIMIT_EXCEEDED,
                "camera file ${file.name} is ${file.sizeBytes} bytes; limits are " +
                    "${config.maxFileBytes} per file and ${config.maxStoredBytes} per epoch [terminal]",
            )
            finish()
            return
        }
        val fileId = "$captureId-frame-%02d".format(frameNumber)
        val record = MediaFileRecord(
            captureId = captureId,
            fileId = fileId,
            timestampMs = atShutter.timestampMs,
            droneId = identity.droneId,
            connectionEpoch = identity.connectionEpoch,
            pose = atShutter.pose,
            actualYawDeg = atShutter.yawDeg,
            gimbalPitchDeg = atShutter.gimbalPitchDeg,
            intrinsics = atShutter.intrinsics,
            checksumSha256 = MediaFileRecord.PENDING_CHECKSUM,
            storageRef = "aircraft://camera/${file.index}/${file.name}",
            retrievalStatus = RetrievalStatus.PENDING,
        )
        val captured = CapturedFile(captureId, fileId, frameNumber, file, record, path = null)
        val sent = frames?.sendMediaFile(record) ?: false
        if (!sent) {
            report.failed(CAMERA_FAILURE, "media_file for $fileId could not be sent: relay link not joined [retryable]")
            finish()
            return
        }
        synchronized(lock) {
            frameCounts[captureId] = frameNumber
            ledger[fileId] = captured
        }
        _status.update { it.copy(files = synchronized(lock) { ledger.values.toList() }) }
        _progress.update { it.copy(acceptedHeadingsDeg = it.acceptedHeadingsDeg + atShutter.yawDeg) }
        event(
            "captured $fileId: ${file.name} (${file.sizeBytes} bytes) at heading " +
                "${fmt(atShutter.yawDeg)} deg, gimbal ${fmt(atShutter.gimbalPitchDeg)} deg",
        )
        report.completed("captured $fileId as ${file.name}, ${file.sizeBytes} bytes on the aircraft; media_file pending")
        finish(keepProgress = true)
    }

    private fun retrieve(fileId: String, report: CommandReport) {
        reconcileLedgerIdentity(frames?.identity())
        val captured = synchronized(lock) { ledger[fileId] }
        if (captured == null) {
            report.failed(DOWNLOAD_FAILURE, "no file $fileId was captured in this connection epoch [terminal]")
            finish()
            return
        }
        val identity = frames?.identity()
        if (
            identity == null ||
            identity.droneId != captured.record.droneId ||
            identity.connectionEpoch != captured.record.connectionEpoch
        ) {
            report.failed(DOWNLOAD_FAILURE, "the connection epoch changed since $fileId was captured [terminal]")
            finish()
            return
        }
        // A transfer may have finalized locally just before its relay send failed. Keep that
        // immutable result in the epoch ledger and retry only the publication; downloading it
        // again would double-count storage and could assign different bytes to the same file id.
        if (captured.record.retrievalStatus == RetrievalStatus.COMPLETED) {
            republishCompleted(captured, report)
            return
        }
        val position = synchronized(lock) { ledger.values.count { it.captureId == captured.captureId && it.frameNumber <= captured.frameNumber } }
        val total = synchronized(lock) { ledger.values.count { it.captureId == captured.captureId } }
        _progress.update { it.copy(phase = CapturePhase.Downloading(position, total)) }
        val captureRoot = File(root, safeSegment(captured.captureId))
        val retainedBytes = storedBytes()
        if (
            captured.camera.sizeBytes > config.maxFileBytes ||
            retainedBytes > config.maxStoredBytes - captured.camera.sizeBytes
        ) {
            report.failed(
                CAPTURE_LIMIT_EXCEEDED,
                "retrieving ${captured.camera.sizeBytes} bytes would exceed the " +
                    "${config.maxStoredBytes}-byte capture-storage limit [terminal]",
            )
            finish()
            return
        }
        val generation = downloadGeneration.incrementAndGet()
        val target = File(captureRoot, ".${safeSegment(captured.fileId)}-$generation.part")
        report.executing("downloading ${captured.camera.name} (${captured.camera.sizeBytes} bytes) over the RC link")
        val outcome = CompletableFuture<String?>()
        var lastProgressAt = clock.nowMs()
        // The camera announcement is the lower-bound contract. Progress may confirm
        // the same total but cannot erase or weaken it.
        val expectedBytes = AtomicLong(captured.camera.sizeBytes)
        port.download(
            captured.camera,
            target,
            object : DownloadListener {
                override fun progress(bytes: Long, total: Long) {
                    if (downloadGeneration.get() != generation) return
                    if (total > 0 && total != expectedBytes.get()) {
                        outcome.complete(
                            "camera size changed from ${expectedBytes.get()} to $total bytes during download",
                        )
                        return
                    }
                    val now = clock.nowMs()
                    if (total > 0 && now - lastProgressAt >= config.progressIntervalMs) {
                        lastProgressAt = now
                        report.executing("downloading ${captured.camera.name}: ${(bytes * 100 / total)}%")
                    }
                }

                override fun finished() {
                    if (downloadGeneration.get() == generation) {
                        outcome.complete(null)
                    } else {
                        target.delete()
                    }
                }

                override fun failed(detail: String) {
                    if (downloadGeneration.get() == generation) {
                        outcome.complete(detail)
                    } else {
                        target.delete()
                    }
                }
            },
        )
        val failure = try {
            outcome.get(config.downloadTimeoutMs, TimeUnit.MILLISECONDS)
        } catch (_: TimeoutException) {
            downloadGeneration.compareAndSet(generation, generation + 1)
            target.delete()
            "download did not finish within ${config.downloadTimeoutMs} ms"
        } catch (error: ExecutionException) {
            error.cause?.message ?: error.message ?: "download failed"
        } catch (_: InterruptedException) {
            downloadGeneration.compareAndSet(generation, generation + 1)
            target.delete()
            Thread.currentThread().interrupt()
            "download interrupted"
        }
        await(config.modeTimeoutMs) { done -> port.leaveMediaMode(done) }
        if (failure != null) {
            // Fence every later callback from this attempt before removing its target.
            // The hardware transfer may finish after an early progress/list failure.
            downloadGeneration.compareAndSet(generation, generation + 1)
            target.delete()
            report.failed(DOWNLOAD_FAILURE, "$failure [retryable]")
            finish()
            return
        }
        val expected = expectedBytes.get()
        val onDisk = target.length()
        if (!target.isFile || expected <= 0 || onDisk != expected) {
            // A port that swallowed a write error would hand over a short file; never checksum one.
            target.delete()
            report.failed(DOWNLOAD_FAILURE, "download truncated: $onDisk of $expected bytes on the phone; partial file removed [retryable]")
            finish()
            return
        }
        val checksum = try {
            sha256(target)
        } catch (error: java.io.IOException) {
            target.delete()
            report.failed(DOWNLOAD_FAILURE, "downloaded file unreadable: ${error.message} [retryable]")
            finish()
            return
        }
        val size = target.length()
        val extension = captured.camera.name.substringAfterLast('.', "bin").filter { it.isLetterOrDigit() }.take(8).ifEmpty { "bin" }
        val completed = File(captureRoot, "${safeSegment(captured.fileId)}-${checksum.take(12)}.$extension")
        if (completed.exists()) {
            val existingMatches = completed.length() == size && runCatching { sha256(completed) }.getOrNull() == checksum
            if (!existingMatches) {
                target.delete()
                report.failed(DOWNLOAD_FAILURE, "completed file path collision for $fileId [terminal]")
                finish()
                return
            }
            target.delete()
        } else if (!target.renameTo(completed)) {
            target.delete()
            report.failed(DOWNLOAD_FAILURE, "downloaded file could not be finalized atomically [retryable]")
            finish()
            return
        }
        val record = captured.record.copy(
            checksumSha256 = checksum,
            storageRef = completed.toURI().toString(),
            retrievalStatus = RetrievalStatus.COMPLETED,
        )
        val currentIdentity = frames?.identity()
        if (
            currentIdentity == null ||
            currentIdentity.droneId != record.droneId ||
            currentIdentity.connectionEpoch != record.connectionEpoch
        ) {
            report.failed(DOWNLOAD_FAILURE, "the connection identity changed while $fileId was downloading [terminal]")
            finish()
            return
        }
        val retrieved = captured.copy(record = record, path = completed.absolutePath)
        synchronized(lock) { ledger[fileId] = retrieved }
        _status.update { it.copy(files = synchronized(lock) { ledger.values.toList() }) }
        if (frames?.sendMediaFile(record) != true) {
            report.failed(
                DOWNLOAD_FAILURE,
                "media_file for $fileId could not be sent; the verified local file is retained for publication retry [retryable]",
            )
            finish()
            return
        }
        captured.path?.let { prior ->
            if (prior != completed.absolutePath) runCatching { File(prior).delete() }
        }
        event("retrieved $fileId: ${completed.absolutePath}, $size bytes, sha256 $checksum")
        report.completed("retrieved $fileId to ${completed.absolutePath}: $size bytes, sha256 $checksum")
        finish(keepProgress = true)
    }

    private fun republishCompleted(captured: CapturedFile, report: CommandReport) {
        val path = captured.path
        val file = path?.let(::File)
        val insideCaptureRoot = runCatching {
            file != null && file.canonicalFile.toPath().startsWith(root.canonicalFile.toPath())
        }.getOrDefault(false)
        val checksum = if (file?.isFile == true) runCatching { sha256(file) }.getOrNull() else null
        if (
            !insideCaptureRoot ||
            file == null ||
            file.length() != captured.camera.sizeBytes ||
            checksum != captured.record.checksumSha256
        ) {
            report.failed(
                DOWNLOAD_FAILURE,
                "the finalized local evidence for ${captured.fileId} is missing or changed; " +
                    "refusing to remint its immutable file id [terminal]",
            )
            finish()
            return
        }
        report.executing("republishing verified media_file ${captured.fileId}")
        if (frames?.sendMediaFile(captured.record) != true) {
            report.failed(
                DOWNLOAD_FAILURE,
                "media_file for ${captured.fileId} could not be sent; " +
                    "the verified local file is retained for publication retry [retryable]",
            )
            finish()
            return
        }
        event(
            "republished ${captured.fileId}: ${file.absolutePath}, ${file.length()} bytes, " +
                "sha256 ${captured.record.checksumSha256}",
        )
        report.completed(
            "retrieved ${captured.fileId} to ${file.absolutePath}: ${file.length()} bytes, " +
                "sha256 ${captured.record.checksumSha256}",
        )
        finish(keepProgress = true)
    }

    // ---- helpers ----

    private fun captureCameraOk(facts: CameraFacts, snapshot: AircraftSnapshot): Boolean {
        val measuredHfov = snapshot.hardware.measuredHfovDeg
        val gimbal = port.gimbalPitchDeg()
        return snapshot.aircraftConnected &&
            facts.cameraConnected &&
            facts.photoMode &&
            facts.photoDimensionsReported &&
            facts.photoWidthPx > 0 &&
            facts.photoHeightPx > 0 &&
            gimbal != null &&
            gimbal.isFinite() &&
            measuredHfov != null &&
            measuredHfov.isFinite() &&
            measuredHfov > 0.0 &&
            measuredHfov <= 180.0
    }

    private fun capturePoseAvailable(snapshot: AircraftSnapshot, nowMs: Long): Boolean =
        snapshot.aircraftConnected &&
            snapshot.positionAvailable &&
            snapshot.attitudeAvailable &&
            measurementFresh(snapshot.positionMeasuredAtMs, nowMs) &&
            measurementFresh(snapshot.attitudeMeasuredAtMs, nowMs) &&
            snapshot.posQuality > 0.0 &&
            listOf(snapshot.x, snapshot.y, snapshot.z, snapshot.yawDeg).all(Double::isFinite)

    private fun captureMotionOk(snapshot: AircraftSnapshot, nowMs: Long): Boolean =
        snapshot.velocityAvailable &&
            measurementFresh(snapshot.velocityMeasuredAtMs, nowMs) &&
            listOf(snapshot.vx, snapshot.vy, snapshot.vz).all(Double::isFinite) &&
            sqrt(snapshot.vx * snapshot.vx + snapshot.vy * snapshot.vy + snapshot.vz * snapshot.vz) <=
            config.maxCaptureSpeedMS

    private fun captureEvidence(): CaptureEvidenceResult {
        val facts = port.facts.value
        val snapshot = aircraft.snapshot.value
        val now = clock.nowMs()
        if (!snapshot.aircraftConnected) return CaptureEvidenceResult.Missing("aircraft disconnected")
        if (!facts.cameraConnected || !facts.photoMode) {
            return CaptureEvidenceResult.Missing("camera disconnected or not in still-photo mode")
        }
        if (!capturePoseAvailable(snapshot, now)) {
            return CaptureEvidenceResult.Missing(
                "measured position and attitude are unavailable or older than ${config.maxTelemetryAgeMs} ms",
            )
        }
        if (!captureMotionOk(snapshot, now)) {
            return CaptureEvidenceResult.Missing(
                "measured velocity is unavailable, stale, or exceeds ${config.maxCaptureSpeedMS} m/s",
            )
        }
        val gimbal = port.gimbalPitchDeg()
        if (gimbal == null || !gimbal.isFinite()) {
            return CaptureEvidenceResult.Missing("gimbal attitude is unreported")
        }
        if (!facts.photoDimensionsReported || facts.photoWidthPx <= 0 || facts.photoHeightPx <= 0) {
            return CaptureEvidenceResult.Missing("photo dimensions are unreported")
        }
        val measuredHfov = snapshot.hardware.measuredHfovDeg
        if (measuredHfov == null || !measuredHfov.isFinite() || measuredHfov <= 0.0 || measuredHfov > 180.0) {
            return CaptureEvidenceResult.Missing("measured horizontal field of view is unreported")
        }
        return CaptureEvidenceResult.Available(
            CaptureEvidence(
                timestampMs = now,
                pose = WirePose(snapshot.x, snapshot.y, snapshot.z),
                yawDeg = FlightOverlay.heading(snapshot.yawDeg),
                gimbalPitchDeg = gimbal,
                intrinsics = WireIntrinsics(
                    facts.photoWidthPx,
                    facts.photoHeightPx,
                    measuredHfov,
                    "rectilinear",
                ),
            ),
        )
    }

    private fun measurementFresh(measuredAtMs: Long?, nowMs: Long): Boolean =
        measuredAtMs != null && measuredAtMs <= nowMs && nowMs - measuredAtMs <= config.maxTelemetryAgeMs

    private fun awaitNewFile(shutterStartedAt: Long): CameraFile? {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(config.fileAnnounceTimeoutMs)
        while (true) {
            val remaining = deadline - System.nanoTime()
            if (remaining <= 0) return null
            val candidate = announced.poll(remaining, TimeUnit.NANOSECONDS) ?: return null
            val now = clock.nowMs()
            val duplicate = synchronized(lock) {
                ledger.values.any {
                    it.camera.index == candidate.index &&
                        it.camera.name == candidate.name &&
                        it.camera.createdAtMs == candidate.createdAtMs
                }
            }
            val timestampMatches = candidate.createdAtMs == null ||
                candidate.createdAtMs in
                (shutterStartedAt - config.fileTimestampSkewMs)..(now + config.fileTimestampSkewMs)
            if (candidate.sizeBytes > 0 && !duplicate && timestampMatches) return candidate
            log.log(
                "camera file announcement ignored: index=${candidate.index} name=${candidate.name} " +
                    "size=${candidate.sizeBytes} created_at=${candidate.createdAtMs} duplicate=$duplicate",
            )
        }
    }

    private fun storageOk(facts: CameraFacts): Boolean =
        facts.storageInserted && (facts.storageRemainingBytes ?: 0L) >= config.minStorageBytes

    /** Keep all mutable capture bookkeeping scoped to the relay's authenticated epoch. */
    private fun reconcileLedgerIdentity(identity: NodeIdentity?) {
        val changed = synchronized(lock) {
            if (ledgerIdentity == identity) {
                false
            } else {
                ledgerIdentity = identity
                ledger.clear()
                frameCounts.clear()
                activeCaptureId = null
                announced.clear()
                true
            }
        }
        if (changed) {
            _status.update { it.copy(files = emptyList()) }
            _progress.value = CaptureProgress()
        }
    }

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

    private fun safeSegment(value: String): String {
        val readable = value
            .map { if (it.isLetterOrDigit() || it in "-_.") it else '_' }
            .joinToString("")
            .take(80)
            .ifEmpty { "id" }
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
            .take(12)
        return "$readable-$digest"
    }

    private fun storedBytes(): Long {
        var total = 0L
        root.walkTopDown().filter { it.isFile }.forEach { file ->
            val size = file.length().coerceAtLeast(0L)
            if (total > Long.MAX_VALUE - size) return Long.MAX_VALUE
            total += size
        }
        return total
    }

    private fun fmt(value: Double): String = "%.1f".format(value)

    companion object {
        const val CAMERA_UNSUPPORTED = "camera_unsupported"
        const val CAMERA_NOT_READY = "camera_not_ready"
        const val CAMERA_FAILURE = "camera_failure"
        const val DOWNLOAD_FAILURE = "download_failure"
        const val CAPTURE_LIMIT_EXCEEDED = "capture_limit_exceeded"
        const val UNSUPPORTED = "unsupported"
    }
}
