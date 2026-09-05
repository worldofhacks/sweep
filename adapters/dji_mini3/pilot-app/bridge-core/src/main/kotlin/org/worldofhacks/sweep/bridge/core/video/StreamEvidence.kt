package org.worldofhacks.sweep.bridge.core.video

/**
 * SDK-free mirror of the MSDK `StreamInfo` that arrives with every encoded frame from
 * `ICameraStreamManager.addReceiveStreamListener`: mime type, picture size, the nominal
 * frame rate, the keyframe flag, and the presentation time. [sizeBytes] is the frame's
 * length in the listener buffer.
 */
data class StreamFrame(
    val mimeType: String,
    val width: Int,
    val height: Int,
    val nominalFrameRateHz: Int,
    val keyFrame: Boolean,
    val presentationTimeMs: Long,
    val sizeBytes: Int,
)

/**
 * The codec evidence Phase D records: what the aircraft stream really is, as the listener
 * reports it and as the SPS says. `node_status` carries none of this; it goes to the bench
 * log and the screen, and it is what #51 needs before choosing a publish path.
 */
data class StreamEvidence(
    val mimeType: String,
    val codec: VideoCodec?,
    val width: Int,
    val height: Int,
    val nominalFrameRateHz: Int,
    val cadence: CadenceStats,
    val sps: SpsInfo?,
    /** Why the last SPS parse failed, or null once a parameter set has been read. */
    val spsError: String?,
    val bytes: Long,
    val firstFrameAtMs: Long,
) {
    val profile: String?
        get() = sps?.profileName

    val level: String?
        get() = sps?.level
}

/**
 * Folds received frames into [StreamEvidence]. The listener hands over the frame descriptor
 * and, for keyframes, the encoded bytes; the SPS is parsed from the first
 * [spsSearchBytes] of a keyframe whenever no parameter set is known yet or the descriptor
 * (mime type, size, nominal rate) changed, so a stream that switches resolution re-reads its
 * profile and level. Every method is synchronized: frames arrive on the SDK's thread and
 * the screen reads from the main thread.
 */
class StreamMonitor(
    private val cadence: KeyframeCadence = KeyframeCadence(),
    private val spsSearchBytes: Int = DEFAULT_SPS_SEARCH_BYTES,
) {
    private var descriptor: StreamFrame? = null
    private var sps: SpsInfo? = null
    private var spsError: String? = null
    private var bytes = 0L
    private var firstFrameAt: Long? = null

    /**
     * Records one frame. Returns true when the descriptor or the parsed SPS changed, so the
     * caller can publish new evidence immediately rather than on its periodic tick.
     */
    @Synchronized
    fun frame(frame: StreamFrame, atMs: Long, data: ByteArray? = null, offset: Int = 0, length: Int = 0): Boolean {
        var changed = false
        val current = descriptor
        if (current == null || !sameStream(current, frame)) {
            descriptor = frame
            if (current != null) {
                sps = null
                spsError = null
            }
            changed = true
        }
        if (firstFrameAt == null) firstFrameAt = atMs
        bytes += frame.sizeBytes.coerceAtLeast(0)
        cadence.frame(atMs, frame.keyFrame)
        if (frame.keyFrame && sps == null && data != null && length > 0) {
            val end = (offset + minOf(length, spsSearchBytes)).coerceAtMost(data.size)
            if (end > offset) {
                val hint = VideoCodec.fromMime(frame.mimeType)
                try {
                    sps = SpsParser.parse(data.copyOfRange(offset, end), hint)
                    spsError = null
                } catch (error: SpsParseException) {
                    spsError = error.message
                }
                changed = true
            }
        }
        return changed
    }

    @Synchronized
    fun evidence(nowMs: Long): StreamEvidence? {
        val current = descriptor ?: return null
        return StreamEvidence(
            mimeType = current.mimeType,
            codec = VideoCodec.fromMime(current.mimeType),
            width = current.width,
            height = current.height,
            nominalFrameRateHz = current.nominalFrameRateHz,
            cadence = cadence.stats(nowMs),
            sps = sps,
            spsError = spsError,
            bytes = bytes,
            firstFrameAtMs = firstFrameAt ?: nowMs,
        )
    }

    @Synchronized
    fun reset() {
        descriptor = null
        sps = null
        spsError = null
        bytes = 0
        firstFrameAt = null
        cadence.reset()
    }

    private fun sameStream(a: StreamFrame, b: StreamFrame): Boolean =
        a.mimeType == b.mimeType && a.width == b.width && a.height == b.height && a.nominalFrameRateHz == b.nominalFrameRateHz

    companion object {
        /** Parameter sets lead an IDR access unit; a few kilobytes cover SPS and PPS. */
        const val DEFAULT_SPS_SEARCH_BYTES = 16_384
    }
}
