package org.worldofhacks.sweep.bridge.publish

import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.datacenter.camera.StreamInfo
import dji.v5.manager.interfaces.ICameraStreamManager
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import org.worldofhacks.sweep.bridge.publish.codec.AnnexB
import org.worldofhacks.sweep.bridge.publish.codec.CodecDecision
import org.worldofhacks.sweep.bridge.publish.codec.CodecGate
import org.worldofhacks.sweep.bridge.publish.codec.H264Slices
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics
import org.worldofhacks.sweep.bridge.publish.webrtc.EncodedUnit

/**
 * The SDK's encoded stream (`ICameraStreamManager.addReceiveStreamListener`, MSDK 5.8.0+):
 * every access unit arrives with its `StreamInfo` (mime type, size, frame rate, keyframe flag).
 *
 * The first keyframes are the codec evidence: the SPS is parsed for profile and level, the
 * delta frames of one GOP are scanned for B slices, and the keyframe cadence is measured.
 * The gate then decides once; a supported stream flows to [sink] as Annex B with the
 * parameter sets present on every keyframe, an unsupported one ends the session with
 * `codec_unsupported` and the profile in the log. Frames the SDK reports with another mime
 * type or size after the decision are re-evaluated.
 */
class DjiEncodedFrameSource(
    private val cameraIndex: ComponentIndexType,
    private val listener: PublishSourceListener,
    private val log: (String) -> Unit,
) {
    fun interface Sink {
        fun onAccessUnit(unit: EncodedUnit)
    }

    @Volatile
    var sink: Sink? = null

    private val manager: ICameraStreamManager
        get() = MediaDataCenter.getInstance().cameraStreamManager

    private val started = AtomicBoolean(false)
    private val lock = Any()
    private var decision: CodecDecision? = null
    private var decidedMime: String? = null
    private var decidedSize: Pair<Int, Int>? = null
    private var parameterSets: ByteArray? = null
    private var keyframesSeen = 0
    private var firstKeyframeAtMs = 0L
    private var lastKeyframeAtMs = 0L
    private var keyframeIntervalMs: Long? = null
    private var bSlices = false
    private var deltaFramesScanned = 0
    private var firstKeyframe: ByteArray? = null
    private var lastInfo: StreamInfo? = null

    private val totalFrames = AtomicLong()
    private val deliveredFrames = AtomicLong()
    private var windowInput = 0L
    private var windowOutput = 0L
    private var windowProcessingNs = 0L
    private var windowStartedNs = System.nanoTime()
    private var lastError: String? = null

    private val streamListener = ICameraStreamManager.ReceiveStreamListener { data, offset, length, info -> onStream(data, offset, length, info) }

    fun start() {
        if (!started.compareAndSet(false, true)) return
        runCatching { manager.enableStream(cameraIndex, true) }
            .onFailure { log("could not enable the ${cameraIndex.name} stream: ${it.message}") }
        manager.addReceiveStreamListener(cameraIndex, streamListener)
        log("listening to the ${cameraIndex.name} encoded stream")
    }

    fun stop() {
        if (!started.compareAndSet(true, false)) return
        runCatching { manager.removeReceiveStreamListener(streamListener) }
        sink = null
    }

    fun deliveredFrames(): Long = deliveredFrames.get()

    private fun onStream(data: ByteArray, offset: Int, length: Int, info: StreamInfo) {
        if (!started.get() || length <= 0) return
        val arrivedNs = System.nanoTime()
        val arrivedMs = System.currentTimeMillis()
        try {
            val unit = synchronized(lock) { admit(data, offset, length, info, arrivedNs, arrivedMs) }
            if (unit != null) {
                sink?.onAccessUnit(unit)
                deliveredFrames.incrementAndGet()
                synchronized(lock) { windowOutput++ }
            }
        } catch (error: RuntimeException) {
            synchronized(lock) { lastError = "${error.javaClass.simpleName}: ${error.message}" }
            log("encoded frame dropped: ${error.javaClass.simpleName}: ${error.message}")
        }
        synchronized(lock) {
            windowInput++
            windowProcessingNs += System.nanoTime() - arrivedNs
            maybeEmitMetrics(System.nanoTime(), info)
        }
    }

    /** Runs under [lock]; returns the unit to deliver, or null while gating or when refused. */
    private fun admit(data: ByteArray, offset: Int, length: Int, info: StreamInfo, arrivedNs: Long, arrivedMs: Long): EncodedUnit? {
        totalFrames.incrementAndGet()
        lastInfo = info
        val mime = info.mimeType?.name ?: "unknown"
        val size = info.width to info.height
        var annexB = AnnexB.normalize(data, offset, length)
        val keyframe = info.isKeyFrame
        if (keyframe) {
            keyframesSeen++
            if (lastKeyframeAtMs != 0L) keyframeIntervalMs = arrivedMs - lastKeyframeAtMs
            if (firstKeyframeAtMs == 0L) firstKeyframeAtMs = arrivedMs
            lastKeyframeAtMs = arrivedMs
            H264Slices.parameterSets(annexB)?.let { parameterSets = it }
            if (!H264Slices.hasSps(annexB)) parameterSets?.let { annexB = H264Slices.prependParameterSets(annexB, it) }
        } else if (mime.equals("H264", ignoreCase = true) && deltaFramesScanned < MAX_DELTA_SCAN) {
            deltaFramesScanned++
            if (H264Slices.containsBSlice(annexB)) bSlices = true
        }
        val current = decision
        if (current != null && decidedMime == mime && decidedSize == size) {
            return if (current.supported) EncodedUnit(annexB, keyframe, info.width, info.height, arrivedNs) else null
        }
        if (current != null) {
            log("stream changed to $mime ${info.width}x${info.height}; re-evaluating the codec gate")
            decision = null
            firstKeyframe = null
            keyframesSeen = if (keyframe) 1 else 0
            bSlices = false
            deltaFramesScanned = 0
            keyframeIntervalMs = null
            lastKeyframeAtMs = if (keyframe) arrivedMs else 0L
        }
        if (keyframe && firstKeyframe == null) firstKeyframe = annexB
        val evidenceFrame = firstKeyframe ?: return null
        // Decide on the second keyframe (one GOP of delta frames scanned, cadence measured) or
        // after a bounded wait when the GOP is long.
        val waitedMs = arrivedMs - firstKeyframeAtMs
        if (keyframesSeen < 2 && waitedMs < MAX_GATE_WAIT_MS) return null
        val evidence = CodecGate.evidence(mime, evidenceFrame, info.width, info.height, info.frameRate, bSlices, keyframeIntervalMs)
        val verdict = CodecGate.evaluate(evidence)
        decision = verdict
        decidedMime = mime
        decidedSize = size
        log("codec gate: ${verdict.detail} (keyframes $keyframesSeen, delta frames scanned $deltaFramesScanned)")
        listener.onCodecEvidence(evidence, verdict)
        return if (verdict.supported) EncodedUnit(annexB, keyframe, info.width, info.height, arrivedNs) else null
    }

    private fun maybeEmitMetrics(nowNs: Long, info: StreamInfo) {
        val elapsedNs = nowNs - windowStartedNs
        if (elapsedNs < 1_000_000_000L) return
        val seconds = elapsedNs / 1_000_000_000.0
        val input = windowInput
        val output = windowOutput
        val processingMs = if (input > 0) windowProcessingNs / input / 1_000_000.0 else 0.0
        windowInput = 0
        windowOutput = 0
        windowProcessingNs = 0
        windowStartedNs = nowNs
        listener.onSourceMetrics(
            WebRTCStreamMetrics(
                sourceWidth = info.width,
                sourceHeight = info.height,
                outputWidth = info.width,
                outputHeight = info.height,
                requestedWidth = 0,
                requestedHeight = 0,
                targetFps = info.frameRate,
                inputFps = input / seconds,
                outputFps = output / seconds,
                droppedFps = (input - output).coerceAtLeast(0) / seconds,
                averageFrameProcessingMs = processingMs,
                totalFrames = totalFrames.get(),
                totalDroppedFrames = (totalFrames.get() - deliveredFrames.get()).coerceAtLeast(0),
                observerCount = if (sink != null) 1 else 0,
                activeCamera = cameraIndex.name,
                status = if (decision?.supported == true) "running" else "gating",
                configuredFps = info.frameRate,
                scaleMode = "native",
                lastError = lastError,
            ),
        )
    }

    private companion object {
        const val MAX_DELTA_SCAN = 120
        const val MAX_GATE_WAIT_MS = 4_000L
    }
}
