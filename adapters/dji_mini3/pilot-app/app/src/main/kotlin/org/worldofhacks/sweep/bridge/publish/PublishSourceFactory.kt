package org.worldofhacks.sweep.bridge.publish

import org.webrtc.VideoCapturer
import org.webrtc.VideoEncoderFactory
import org.worldofhacks.sweep.bridge.publish.codec.CodecDecision
import org.worldofhacks.sweep.bridge.publish.codec.CodecEvidence
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics

/** Callbacks from an open frame source; they may arrive on the SDK's threads. */
interface PublishSourceListener {
    /** The encoded stream's codec evidence and the gate's verdict; an unsupported verdict ends the session. */
    fun onCodecEvidence(evidence: CodecEvidence, decision: CodecDecision)

    /** The source's one-second health window. */
    fun onSourceMetrics(metrics: WebRTCStreamMetrics)

    /** The source stopped producing frames for a reason the publisher should report. */
    fun onSourceFailure(reason: String, detail: String)
}

/** The frame source could not be opened right now (SDK not registered, no camera stream). */
class SourceUnavailableException(detail: String) : RuntimeException(detail)

/**
 * An open frame source: the WebRTC capturer to feed the peer connection, the encoder factory
 * to build it with (null for the platform default), the sender bitrate to ask for, a codec
 * label when the source already knows it (passthrough), and how to close it.
 */
class OpenSource(
    val source: PublishSource,
    val capturer: VideoCapturer,
    val encoderFactory: VideoEncoderFactory?,
    val targetBitrateBps: Int,
    val maxBitrateBps: Int,
    val codecLabel: String?,
    /** Frames the passthrough path lost between capture and encode; null when not applicable. */
    val extraDropped: (() -> Long)?,
    private val onClose: () -> Unit,
) {
    fun close() = onClose()
}

/** Flavor-specific: the probe flavor opens the DJI stream, the fake flavor a test pattern. */
interface PublishSourceFactory {
    /** Sources this flavor offers, in display order; the first is the default. */
    val available: List<PublishSource>

    @Throws(SourceUnavailableException::class)
    fun open(source: PublishSource, droneId: Int, listener: PublishSourceListener): OpenSource
}
