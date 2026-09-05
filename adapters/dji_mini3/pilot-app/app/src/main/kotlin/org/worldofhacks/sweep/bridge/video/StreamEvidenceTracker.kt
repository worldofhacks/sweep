package org.worldofhacks.sweep.bridge.video

import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.util.concurrent.Executors
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.bench.BenchRecorder
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence
import org.worldofhacks.sweep.bridge.core.video.StreamFrame
import org.worldofhacks.sweep.bridge.core.video.StreamMonitor
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource

/**
 * Bridges a camera stream's frames to the screen and the bench log. [frame] is called on the
 * stream's own thread for every encoded frame and does only the O(1) cadence bookkeeping
 * (plus one bounded SPS parse per keyframe until a parameter set is known); the JSONL
 * writing runs on a single worker thread so neither the SDK callback nor the relay link's
 * telemetry ticker ever waits on disk. Evidence reaches Compose through [evidence] at most
 * every [publishIntervalMs], immediately when the descriptor or SPS changes.
 *
 * The bench log holds one `video_frame` record per received frame and one `stream_info`
 * record per second with the phone's battery and thermal state beside the codec evidence.
 */
class StreamEvidenceTracker(
    private val logDirectory: File?,
    private val phone: PhoneStatusSource?,
    private val clock: Clock = SystemClock,
    private val publishIntervalMs: Long = PUBLISH_INTERVAL_MS,
    private val logIntervalMs: Long = LOG_INTERVAL_MS,
) {
    private val monitor = StreamMonitor()
    private val worker = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "stream-evidence-log").apply { isDaemon = true }
    }

    private val _evidence = MutableStateFlow<StreamEvidence?>(null)
    val evidence: StateFlow<StreamEvidence?> = _evidence.asStateFlow()

    private val _logPath = MutableStateFlow<String?>(null)
    val logPath: StateFlow<String?> = _logPath.asStateFlow()

    private val _lastFrameAt = MutableStateFlow<Long?>(null)

    /** Arrival time of the newest frame; [reset] leaves it so a stall or a disconnect can be aged. */
    val lastFrameAtMs: StateFlow<Long?> = _lastFrameAt.asStateFlow()

    @Volatile
    private var lastPublishAt = 0L

    // Worker-thread state.
    private var writer: BufferedWriter? = null
    private var recorder: BenchRecorder? = null
    private var lastLogAt = 0L

    fun frame(frame: StreamFrame, data: ByteArray?, offset: Int, length: Int) {
        val now = clock.nowMs()
        val changed = monitor.frame(frame, now, data, offset, length)
        if (changed || now - lastPublishAt >= publishIntervalMs) {
            lastPublishAt = now
            _evidence.value = monitor.evidence(now)
            _lastFrameAt.value = now
        }
        val sizeBytes = frame.sizeBytes
        val keyframe = frame.keyFrame
        worker.execute { record(sizeBytes, keyframe, changed) }
    }

    /** Opens a fresh log; called when a Surface attaches. */
    fun start() {
        worker.execute { open() }
    }

    /** Writes a note and closes the log; called when the Surface goes away. */
    fun stop(reason: String) {
        worker.execute { close(reason) }
    }

    fun note(text: String) {
        worker.execute { recorder?.note(text) }
    }

    /**
     * Forgets the stream (aircraft disconnect, Surface detach) so stale evidence never shows.
     * [lastFrameAtMs] is kept: when the picture was last seen is a fact about the loss.
     */
    fun reset() {
        monitor.reset()
        lastPublishAt = 0
        _evidence.value = null
    }

    private fun record(sizeBytes: Int, keyframe: Boolean, changed: Boolean) {
        val recorder = recorder ?: return
        recorder.videoFrame(sizeBytes = sizeBytes, keyframe = keyframe)
        val now = clock.nowMs()
        if (changed || now - lastLogAt >= logIntervalMs) {
            lastLogAt = now
            val evidence = monitor.evidence(now) ?: return
            val status = phone?.current()
            recorder.streamInfo(evidence, status?.batteryPercent, status?.thermalState?.wire)
            writer?.flush()
        }
    }

    private fun open() {
        if (recorder != null) return
        val directory = logDirectory ?: return
        runCatching {
            val bench = File(directory, LOG_DIRECTORY).apply { mkdirs() }
            val file = File(bench, "stream-${clock.nowMs()}.jsonl")
            val out = BufferedWriter(FileWriter(file, true))
            writer = out
            recorder = BenchRecorder(out, clock).also { it.note("stream evidence log opened") }
            lastLogAt = 0
            _logPath.value = file.absolutePath
        }
    }

    private fun close(reason: String) {
        recorder?.note(reason)
        runCatching { writer?.flush() }
        runCatching { writer?.close() }
        writer = null
        recorder = null
    }

    companion object {
        const val LOG_DIRECTORY = "bench"
        const val PUBLISH_INTERVAL_MS = 250L
        const val LOG_INTERVAL_MS = 1_000L
    }
}
