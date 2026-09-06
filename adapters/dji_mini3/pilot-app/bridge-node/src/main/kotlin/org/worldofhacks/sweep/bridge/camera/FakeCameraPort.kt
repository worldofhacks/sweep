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
            photoMode = false,
            storageInserted = true,
            storageRemainingBytes = storageRemainingBytes,
            gimbalPitchMinDeg = -90.0,
            gimbalPitchMaxDeg = 30.0,
            horizontalFovDeg = 66.0,
        ),
    )
    override val facts: StateFlow<CameraFacts> = _facts.asStateFlow()

    private val lock = Any()
    private var pitch: Double? = 0.0
    private var listener: ((CameraFile) -> Unit)? = null
    private var nextIndex = 1
    private val files = LinkedHashMap<Int, ByteArray>()

    /** Test knobs: a pitch the gimbal "reports" instead of the commanded one, and a forced download failure. */
    @Volatile
    var reportedPitchOverride: Double? = null

    @Volatile
    var downloadFailure: String? = null

    @Volatile
    var shutterFailure: String? = null

    val shots: Int
        get() = synchronized(lock) { nextIndex - 1 }

    fun bytesOf(index: Int): ByteArray? = synchronized(lock) { files[index]?.copyOf() }

    fun setStorageRemainingBytes(value: Long) = _facts.update { it.copy(storageRemainingBytes = value) }

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
        val bytes = synchronized(lock) { files[file.index] }
        later {
            when {
                failure != null -> listener.failed(failure)
                bytes == null -> listener.failed("no file with index ${file.index} on the fake camera")
                else -> {
                    runCatching {
                        target.parentFile?.mkdirs()
                        listener.progress(0, bytes.size.toLong())
                        target.outputStream().use { stream ->
                            val half = bytes.size / 2
                            stream.write(bytes, 0, half)
                            listener.progress(half.toLong(), bytes.size.toLong())
                            stream.write(bytes, half, bytes.size - half)
                        }
                        listener.progress(bytes.size.toLong(), bytes.size.toLong())
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
    }
}
