package org.worldofhacks.sweep.bridge.publish.metrics

/**
 * One reading of the sender's transport counters (WebRTC `outbound-rtp`, `candidate-pair`,
 * and `transport` stats). Counters are cumulative since the peer connection was created; the
 * aggregator turns consecutive samples into rates.
 */
data class TransportSample(
    val atMs: Long,
    val bytesSent: Long,
    val framesSent: Long,
    val framesEncoded: Long,
    val keyFramesEncoded: Long,
    /** Selected candidate pair round trip in milliseconds, when the pair has reported one. */
    val rttMs: Double? = null,
    val iceState: String = "unknown",
    val codec: String? = null,
    val qualityLimitation: String? = null,
)

/** The one-second publish record: transport rates plus the frame source's own health. */
data class PublishMetrics(
    val atMs: Long,
    val bitrateKbps: Double?,
    val fps: Double?,
    val framesSent: Long,
    val keyframes: Long,
    val droppedFrames: Long,
    val rttMs: Double?,
    val iceState: String,
    val codec: String?,
    val sourceInputFps: Double?,
    val sourceOutputFps: Double?,
    /** Android processing per frame: NV21 scale for the re-encode path, queueing for passthrough. */
    val processingMs: Double?,
    val width: Int,
    val height: Int,
    val keyframeIntervalMs: Long?,
    val qualityLimitation: String?,
) {
    fun compactLabel(): String = buildString {
        append(bitrateKbps?.let { "${format1(it / 1000.0)} Mbps" } ?: "- Mbps")
        append(" · ").append(fps?.let { "${format1(it)} fps" } ?: "- fps")
        if (width > 0 && height > 0) append(" · ").append(width).append('x').append(height)
        append(" · dropped ").append(droppedFrames)
        append(" · rtt ").append(rttMs?.let { "${format1(it)} ms" } ?: "-")
        append(" · ice ").append(iceState)
        codec?.let { append(" · ").append(it) }
        processingMs?.let { append(" · processing ${format1(it)} ms") }
        keyframeIntervalMs?.let { append(" · keyframe every $it ms") }
        qualityLimitation?.takeIf { it != "none" }?.let { append(" · limited by $it") }
    }

    private fun format1(value: Double): String = String.format(java.util.Locale.US, "%.1f", value)
}

/**
 * Folds transport samples and the frame source's [WebRTCStreamMetrics] into one
 * [PublishMetrics] per tick. Rates come from the delta against the previous transport sample,
 * so the first sample after [reset] carries no bitrate or frame rate.
 */
class PublishMetricsAggregator {
    private val lock = Any()
    private var previous: TransportSample? = null
    private var source: WebRTCStreamMetrics? = null
    private var extraDropped: Long = 0
    private var keyframeIntervalMs: Long? = null
    private var lastKeyframes: Long = 0
    private var lastKeyframeAtMs: Long? = null

    /** The frame source's latest one-second window. */
    fun onSource(metrics: WebRTCStreamMetrics) {
        synchronized(lock) { source = metrics }
    }

    /** Frames the passthrough queue discarded (counted outside the source's own window). */
    fun onExtraDropped(total: Long) {
        synchronized(lock) { extraDropped = total }
    }

    /** Keyframe cadence measured by the frame source (the SDK's GOP for passthrough). */
    fun onKeyframeInterval(intervalMs: Long?) {
        synchronized(lock) { keyframeIntervalMs = intervalMs }
    }

    fun onTransport(sample: TransportSample): PublishMetrics = synchronized(lock) {
        val last = previous
        previous = sample
        val elapsedMs = last?.let { sample.atMs - it.atMs }?.takeIf { it > 0 }
        val bitrate = if (last != null && elapsedMs != null) {
            (sample.bytesSent - last.bytesSent).coerceAtLeast(0) * 8.0 / elapsedMs
        } else {
            null
        }
        val fps = if (last != null && elapsedMs != null) {
            (sample.framesSent - last.framesSent).coerceAtLeast(0) * 1000.0 / elapsedMs
        } else {
            null
        }
        if (keyframeIntervalMs == null && sample.keyFramesEncoded > lastKeyframes) {
            val at = lastKeyframeAtMs
            if (at != null && sample.keyFramesEncoded == lastKeyframes + 1) keyframeIntervalMs = sample.atMs - at
            lastKeyframeAtMs = sample.atMs
            lastKeyframes = sample.keyFramesEncoded
        }
        val src = source
        PublishMetrics(
            atMs = sample.atMs,
            bitrateKbps = bitrate,
            fps = fps,
            framesSent = sample.framesSent,
            keyframes = sample.keyFramesEncoded,
            droppedFrames = (src?.totalDroppedFrames ?: 0) + extraDropped,
            rttMs = sample.rttMs,
            iceState = sample.iceState,
            codec = sample.codec,
            sourceInputFps = src?.inputFps,
            sourceOutputFps = src?.outputFps,
            processingMs = src?.averageFrameProcessingMs,
            width = src?.outputWidth ?: 0,
            height = src?.outputHeight ?: 0,
            keyframeIntervalMs = keyframeIntervalMs,
            qualityLimitation = sample.qualityLimitation,
        )
    }

    fun reset() {
        synchronized(lock) {
            previous = null
            source = null
            extraDropped = 0
            keyframeIntervalMs = null
            lastKeyframes = 0
            lastKeyframeAtMs = null
        }
    }
}
