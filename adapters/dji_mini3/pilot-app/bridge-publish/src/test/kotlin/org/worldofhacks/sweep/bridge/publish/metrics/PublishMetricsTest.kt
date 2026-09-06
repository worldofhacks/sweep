package org.worldofhacks.sweep.bridge.publish.metrics

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class PublishMetricsTest {
    private fun sample(atMs: Long, bytes: Long, frames: Long, keyframes: Long = 1, rtt: Double? = 4.0, ice: String = "connected") =
        TransportSample(atMs, bytesSent = bytes, framesSent = frames, framesEncoded = frames, keyFramesEncoded = keyframes, rttMs = rtt, iceState = ice, codec = "H264", qualityLimitation = "none")

    @Test
    fun `rates come from the delta against the previous sample`() {
        val aggregator = PublishMetricsAggregator()
        val first = aggregator.onTransport(sample(1_000, 100_000, 30))
        assertNull(first.bitrateKbps)
        assertNull(first.fps)
        assertEquals(30, first.framesSent)
        assertEquals("connected", first.iceState)
        assertEquals(4.0, first.rttMs)

        val second = aggregator.onTransport(sample(2_000, 600_000, 60))
        assertEquals(4_000.0, second.bitrateKbps!!, 1e-9) // 500 000 bytes in 1 s
        assertEquals(30.0, second.fps!!, 1e-9)
        assertEquals("H264", second.codec)

        val third = aggregator.onTransport(sample(2_500, 700_000, 75, rtt = null, ice = "disconnected"))
        assertEquals(1_600.0, third.bitrateKbps!!, 1e-9) // 100 000 bytes in 0.5 s
        assertEquals(30.0, third.fps!!, 1e-9)
        assertNull(third.rttMs)
        assertEquals("disconnected", third.iceState)
        assertTrue(third.compactLabel().startsWith("1.6 Mbps · 30.0 fps"), third.compactLabel())
        assertTrue(third.compactLabel().contains("rtt -"), third.compactLabel())
    }

    @Test
    fun `counters that regress after a reconnect never produce negative rates`() {
        val aggregator = PublishMetricsAggregator()
        aggregator.onTransport(sample(1_000, 900_000, 300))
        val after = aggregator.onTransport(sample(2_000, 10_000, 5))
        assertEquals(0.0, after.bitrateKbps)
        assertEquals(0.0, after.fps)
        aggregator.reset()
        assertNull(aggregator.onTransport(sample(3_000, 20_000, 10)).bitrateKbps)
    }

    @Test
    fun `the source window and extra drops are folded in`() {
        val aggregator = PublishMetricsAggregator()
        aggregator.onSource(
            WebRTCStreamMetrics(
                sourceWidth = 1280,
                sourceHeight = 720,
                outputWidth = 1280,
                outputHeight = 720,
                inputFps = 30.0,
                outputFps = 29.5,
                averageFrameProcessingMs = 0.4,
                totalDroppedFrames = 3,
                status = "running",
            ),
        )
        aggregator.onExtraDropped(2)
        aggregator.onKeyframeInterval(1_000)
        val metrics = aggregator.onTransport(sample(1_000, 0, 0))
        assertEquals(5, metrics.droppedFrames)
        assertEquals(1280, metrics.width)
        assertEquals(720, metrics.height)
        assertEquals(30.0, metrics.sourceInputFps)
        assertEquals(29.5, metrics.sourceOutputFps)
        assertEquals(0.4, metrics.processingMs)
        assertEquals(1_000L, metrics.keyframeIntervalMs)
        val label = metrics.compactLabel()
        assertTrue(label.contains("1280x720") && label.contains("dropped 5") && label.contains("processing 0.4 ms") && label.contains("keyframe every 1000 ms"), label)
    }

    @Test
    fun `the keyframe interval is measured from consecutive keyframe counts when the source gives none`() {
        val aggregator = PublishMetricsAggregator()
        aggregator.onTransport(sample(1_000, 0, 0, keyframes = 1))
        assertNull(aggregator.onTransport(sample(2_000, 0, 0, keyframes = 1)).keyframeIntervalMs)
        assertEquals(2_000L, aggregator.onTransport(sample(3_000, 0, 0, keyframes = 2)).keyframeIntervalMs)
        assertEquals(2_000L, aggregator.onTransport(sample(9_000, 0, 0, keyframes = 3)).keyframeIntervalMs)
    }
}
