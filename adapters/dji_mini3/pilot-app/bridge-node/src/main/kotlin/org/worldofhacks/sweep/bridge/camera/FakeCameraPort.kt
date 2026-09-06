package org.worldofhacks.sweep.bridge.camera

import java.io.File
import java.util.Random
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.worldofhacks.sweep.bridge.core.flight.PortResult

/**
 * The fake flavor's camera: a gimbal that reaches every pitch at once, a shutter that
 * announces one synthetic file per shot, and a download that writes that file's bytes to
 * the target with progress callbacks, so the whole capture path (media records, SHA-256,
 * paths, the flight display's states) runs on any phone and in the JVM tests. The bytes are
 * deterministic per file index and are not an image.
 */
class FakeCameraPort(
    private val connected: () -> Boolean = { true },
    storageRemainingBytes: Long = 50_000_000L,
    private val fileBytes: Int = 4096,
    private val latencyMs: Long = 10,
) : CameraPort {
    private val worker = Executors.newSingleThreadScheduledExecutor { runnable -> Thread(runnable, "fake-camera").apply { isDaemon = true } }
    private val _facts = MutableStateFlow(
        CameraFacts(
            cameraConnected = connected(),
            // A Mini 3 boots in still-photo mode; the arbiter refuses a capture_room intent until a
            // capture_readiness frame reports camera_ok, so the fake joins the way the aircraft does.
            photoMode = true,
            storageInserted = true,
            storageRemainingBytes = storageRemainingBytes,
            gimbalPitchMinDeg = -90.0,
            gimbalPitchMaxDeg = 30.0,
            horizontalFovDeg = 66.0,
            photoWidthPx = 4000,
            photoHeightPx = 3000,
            photoDimensionsReported = true,
        ),
    )
    override val facts: StateFlow<CameraFacts> = _facts.asStateFlow()

    private val lock = Any()
    private var pitch: Double? = 0.0
    private var listener: ((CameraFile) -> Unit)? = null
    private var nextIndex = 1
    private var downloadCount = 0
    private val files = LinkedHashMap<Int, ByteArray>()

    /**
     * Test knobs: a pitch the gimbal "reports" instead of the commanded one, a forced download
     * failure, a forced shutter failure, and a download that stops after this many bytes yet
     * reports success, as a port with a swallowed write error would.
     */
    @Volatile
    var reportedPitchOverride: Double? = null

    @Volatile
    var downloadFailure: String? = null

    @Volatile
    var shutterFailure: String? = null

    @Volatile
    var truncateDownloadAt: Int? = null

    val shots: Int
        get() = synchronized(lock) { nextIndex - 1 }

    val downloads: Int
        get() = synchronized(lock) { downloadCount }

    fun bytesOf(index: Int): ByteArray? = synchronized(lock) { files[index]?.copyOf() }

    fun setStorageRemainingBytes(value: Long) = _facts.update { it.copy(storageRemainingBytes = value) }

    /** The RC's photo/video switch: leaves or re-enters still-photo mode without a command. */
    fun setPhotoMode(value: Boolean) = _facts.update { it.copy(photoMode = value) }

    override fun refreshFacts(onResult: (PortResult) -> Unit) {
        _facts.update { it.copy(cameraConnected = connected()) }
        later { onResult(PortResult.Ok) }
    }

    override fun gimbalPitchDeg(): Double? = reportedPitchOverride ?: synchronized(lock) { pitch }

    override fun setGimbalPitch(pitchDeg: Double, onResult: (PortResult) -> Unit) {
        synchronized(lock) { pitch = pitchDeg }
        later { onResult(PortResult.Ok) }
    }

    override fun enterPhotoMode(onResult: (PortResult) -> Unit) {
        if (!connected()) {
            _facts.update { it.copy(cameraConnected = false, photoMode = false) }
            later { onResult(PortResult.Failed("camera is not connected")) }
            return
        }
        _facts.update { it.copy(cameraConnected = true, photoMode = true) }
        later { onResult(PortResult.Ok) }
    }

    override fun shootPhoto(onResult: (PortResult) -> Unit) {
        val failure = shutterFailure
        if (failure != null) {
            later { onResult(PortResult.Failed(failure)) }
            return
        }
        if (!facts.value.photoMode) {
            later { onResult(PortResult.Failed("camera is not in photo mode")) }
            return
        }
        val (file, announce) = synchronized(lock) {
            val index = nextIndex++
            val bytes = synthesize(index)
            while (files.size >= MAX_FILES) files.remove(files.keys.first())
            files[index] = bytes
            CameraFile(index = index, name = "FAKE_%04d.JPG".format(index), sizeBytes = bytes.size.toLong(), createdAtMs = System.currentTimeMillis()) to listener
        }
        later { onResult(PortResult.Ok) }
        // The camera announces the file a moment after the shutter, as the SDK's generated-file key does.
        later(latencyMs * 2) { announce?.invoke(file) }
    }

    override fun setFileListener(listener: ((CameraFile) -> Unit)?) {
        synchronized(lock) { this.listener = listener }
    }

    override fun download(file: CameraFile, target: File, listener: DownloadListener) {
        val failure = downloadFailure
        val truncateAt = truncateDownloadAt
        val bytes = synchronized(lock) {
            downloadCount += 1
            files[file.index]
        }
        later {
            when {
                failure != null -> listener.failed(failure)
                bytes == null -> listener.failed("no file with index ${file.index} on the fake camera")
                else -> {
                    runCatching {
                        target.parentFile?.mkdirs()
                        val total = bytes.size.toLong()
                        listener.progress(0, total)
                        target.outputStream().use { stream ->
                            val half = bytes.size / 2
                            val stopAt = truncateAt?.coerceIn(0, bytes.size) ?: bytes.size
                            stream.write(bytes, 0, minOf(half, stopAt))
                            listener.progress(half.toLong(), total)
                            if (stopAt > half) stream.write(bytes, half, stopAt - half)
                        }
                        listener.progress(total, total)
                    }.onSuccess { listener.finished() }.onFailure { error -> listener.failed(error.message ?: error.javaClass.simpleName) }
                }
            }
        }
    }

    override fun leaveMediaMode(onResult: (PortResult) -> Unit) {
        later { onResult(PortResult.Ok) }
    }

    fun close() {
        worker.shutdownNow()
    }

    private fun synthesize(index: Int): ByteArray {
        val header = "SWEEP-FAKE-CAPTURE index=$index\n".toByteArray(Charsets.US_ASCII)
        val body = ByteArray(fileBytes).also { Random(SEED + index).nextBytes(it) }
        return header + body
    }

    private fun later(delayMs: Long = latencyMs, block: () -> Unit) {
        worker.schedule(block, delayMs, TimeUnit.MILLISECONDS)
    }

    private companion object {
        const val SEED = 0x5EEDL
        const val MAX_FILES = 64
    }
}
