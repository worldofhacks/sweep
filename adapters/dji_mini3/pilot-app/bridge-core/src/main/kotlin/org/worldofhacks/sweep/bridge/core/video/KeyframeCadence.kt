package org.worldofhacks.sweep.bridge.core.video

/** What the receive-stream listener actually delivered, measured from arrival times. */
data class CadenceStats(
    val frames: Long,
    val keyframes: Long,
    /** Frames per second over the sliding window, or null until two frames have arrived in it. */
    val measuredFrameRateHz: Double?,
    /** Mean interval between the most recent keyframes, or null before the second keyframe. */
    val keyframeIntervalMs: Long?,
    val keyframeIntervalMinMs: Long?,
    val keyframeIntervalMaxMs: Long?,
    /** Frames from one keyframe up to the next (the GOP length), from the last completed group. */
    val keyframeIntervalFrames: Int?,
    val lastFrameAtMs: Long?,
    val lastKeyframeAtMs: Long?,
) {
    companion object {
        val EMPTY = CadenceStats(0, 0, null, null, null, null, null, null, null)
    }
}

/**
 * Measures the frame rate and keyframe cadence of an encoded stream from the arrival time of
 * each frame and its keyframe flag. Nothing is read from the stream's headers: the nominal
 * frame rate the SDK reports sits beside the measured one in [StreamEvidence], and the
 * keyframe interval in frames is the GOP length the encoder really used. The frame-rate
 * window is short so a stall shows up within seconds; the keyframe intervals keep the last
 * [intervalSamples] groups.
 */
class KeyframeCadence(
    private val windowMs: Long = DEFAULT_WINDOW_MS,
    private val intervalSamples: Int = DEFAULT_INTERVAL_SAMPLES,
) {
    init {
        require(windowMs > 0) { "window must be positive" }
        require(intervalSamples > 0) { "interval sample count must be positive" }
    }

    private val arrivals = ArrayDeque<Long>()
    private val intervalsMs = ArrayDeque<Long>()
    private var frames = 0L
    private var keyframes = 0L
    private var lastFrameAt: Long? = null
    private var lastKeyframeAt: Long? = null
    private var framesSinceKeyframe = 0
    private var lastGroupFrames: Int? = null

    fun frame(atMs: Long, keyFrame: Boolean) {
        frames++
        lastFrameAt = atMs
        arrivals.addLast(atMs)
        trim(atMs)
        if (keyFrame) {
            keyframes++
            val previous = lastKeyframeAt
            if (previous != null) {
                intervalsMs.addLast(atMs - previous)
                while (intervalsMs.size > intervalSamples) intervalsMs.removeFirst()
                lastGroupFrames = framesSinceKeyframe
            }
            lastKeyframeAt = atMs
            framesSinceKeyframe = 0
        }
        framesSinceKeyframe++
    }

    fun stats(nowMs: Long): CadenceStats {
        trim(nowMs)
        val rate = if (arrivals.size >= 2) {
            val span = arrivals.last() - arrivals.first()
            if (span > 0) (arrivals.size - 1) * 1000.0 / span else null
        } else {
            null
        }
        return CadenceStats(
            frames = frames,
            keyframes = keyframes,
            measuredFrameRateHz = rate,
            keyframeIntervalMs = if (intervalsMs.isEmpty()) null else intervalsMs.sum() / intervalsMs.size,
            keyframeIntervalMinMs = intervalsMs.minOrNull(),
            keyframeIntervalMaxMs = intervalsMs.maxOrNull(),
            keyframeIntervalFrames = lastGroupFrames,
            lastFrameAtMs = lastFrameAt,
            lastKeyframeAtMs = lastKeyframeAt,
        )
    }

    fun reset() {
        arrivals.clear()
        intervalsMs.clear()
        frames = 0
        keyframes = 0
        lastFrameAt = null
        lastKeyframeAt = null
        framesSinceKeyframe = 0
        lastGroupFrames = null
    }

    private fun trim(nowMs: Long) {
        while (arrivals.isNotEmpty() && arrivals.first() < nowMs - windowMs) arrivals.removeFirst()
    }

    companion object {
        const val DEFAULT_WINDOW_MS = 5_000L
        const val DEFAULT_INTERVAL_SAMPLES = 8
    }
}
