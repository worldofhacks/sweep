package org.worldofhacks.sweep.bridge.core.video

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test

class KeyframeCadenceTest {
    @Test
    fun `measures frame rate and keyframe interval on synthetic timestamps`() {
        val cadence = KeyframeCadence(windowMs = 5_000)
        // 25 fps (40 ms spacing), keyframe every 25 frames: 4 seconds of frames.
        var t = 1_000L
        repeat(100) { index ->
            cadence.frame(t, keyFrame = index % 25 == 0)
            t += 40
        }
        val stats = cadence.stats(nowMs = t)
        assertEquals(100, stats.frames)
        assertEquals(4, stats.keyframes)
        assertEquals(25.0, stats.measuredFrameRateHz!!, 1e-9)
        assertEquals(1_000L, stats.keyframeIntervalMs)
        assertEquals(1_000L, stats.keyframeIntervalMinMs)
        assertEquals(1_000L, stats.keyframeIntervalMaxMs)
        assertEquals(25, stats.keyframeIntervalFrames)
        assertEquals(t - 40, stats.lastFrameAtMs)
        assertEquals(1_000L + 75 * 40, stats.lastKeyframeAtMs)
    }

    @Test
    fun `irregular keyframes report min max and mean`() {
        val cadence = KeyframeCadence()
        val keyframesAt = listOf(0L, 500L, 2_000L, 3_000L)
        var t = 0L
        var next = 0
        while (t <= 3_000L) {
            val key = next < keyframesAt.size && keyframesAt[next] == t
            if (key) next++
            cadence.frame(t, key)
            t += 100
        }
        val stats = cadence.stats(3_000L)
        assertEquals(4, stats.keyframes)
        assertEquals(500L, stats.keyframeIntervalMinMs)
        assertEquals(1_500L, stats.keyframeIntervalMaxMs)
        assertEquals(1_000L, stats.keyframeIntervalMs)
        assertEquals(10, stats.keyframeIntervalFrames)
    }

    @Test
    fun `no rate before two frames and no interval before two keyframes`() {
        val cadence = KeyframeCadence()
        assertEquals(CadenceStats.EMPTY, cadence.stats(0))
        cadence.frame(10, keyFrame = true)
        val one = cadence.stats(10)
        assertEquals(1, one.frames)
        assertEquals(1, one.keyframes)
        assertNull(one.measuredFrameRateHz)
        assertNull(one.keyframeIntervalMs)
        assertNull(one.keyframeIntervalFrames)
        cadence.frame(50, keyFrame = false)
        assertEquals(25.0, cadence.stats(50).measuredFrameRateHz!!, 1e-9)
    }

    @Test
    fun `a stalled stream drops out of the rate window but keeps its counts`() {
        val cadence = KeyframeCadence(windowMs = 1_000)
        repeat(30) { cadence.frame(it * 33L, keyFrame = it == 0) }
        assertEquals(30, cadence.stats(29 * 33L).frames)
        val later = cadence.stats(29 * 33L + 5_000)
        assertNull(later.measuredFrameRateHz)
        assertEquals(30, later.frames)
        assertEquals(29 * 33L, later.lastFrameAtMs)
    }

    @Test
    fun `interval samples are bounded and reset clears everything`() {
        val cadence = KeyframeCadence(intervalSamples = 2)
        // Keyframes at 0, 100, 300, 600: intervals 100, 200, 300; only the last two are kept.
        listOf(0L, 100L, 300L, 600L).forEach { cadence.frame(it, keyFrame = true) }
        val stats = cadence.stats(600)
        assertEquals(250L, stats.keyframeIntervalMs)
        assertEquals(200L, stats.keyframeIntervalMinMs)
        cadence.reset()
        assertEquals(CadenceStats.EMPTY, cadence.stats(600))
    }
}
