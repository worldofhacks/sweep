package org.worldofhacks.sweep.bridge.core.video

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class StreamMonitorTest {
    private fun hex(text: String): ByteArray =
        text.trim().split(Regex("\\s+")).map { it.toInt(16).toByte() }.toByteArray()

    // H.264 Main profile, level 3.1 SPS followed by a PPS, with Annex B start codes, as an
    // IDR access unit begins.
    private val h264Main31 = hex("00 00 00 01 67 4D 40 1F E8 80 50 17 FC B0 80 00 00 03 00 80 00 00 19 07 8B 16 CB") +
        hex("00 00 00 01 68 EE 3C B0")

    private val h265Main31 = hex("00 00 00 01") +
        hex("42 01 01 01 60 00 00 03 00 90 00 00 03 00 00 03 00 5D A0 02 80 80 2D 16 59 59 A4 93 2B C0 5A 70 80 00 01 F4 80 00 3A 98 04")

    private fun frame(
        mime: String = "video/avc",
        width: Int = 1280,
        height: Int = 720,
        rate: Int = 30,
        key: Boolean = false,
        pts: Long = 0,
        size: Int = 1_000,
    ) = StreamFrame(mime, width, height, rate, key, pts, size)

    @Test
    fun `stream info maps to evidence and the keyframe sps gives profile and level`() {
        val monitor = StreamMonitor()
        assertNull(monitor.evidence(0))
        val changed = monitor.frame(frame(key = true, size = h264Main31.size), atMs = 1_000, data = h264Main31, length = h264Main31.size)
        assertTrue(changed)
        val evidence = requireNotNull(monitor.evidence(1_000))
        assertEquals("video/avc", evidence.mimeType)
        assertEquals(VideoCodec.H264, evidence.codec)
        assertEquals(1280, evidence.width)
        assertEquals(720, evidence.height)
        assertEquals(30, evidence.nominalFrameRateHz)
        assertEquals("Main", evidence.profile)
        assertEquals("3.1", evidence.level)
        assertEquals(77, evidence.sps?.profileIdc)
        assertNull(evidence.spsError)
        assertEquals(1, evidence.cadence.frames)
        assertEquals(1, evidence.cadence.keyframes)
        assertEquals(h264Main31.size.toLong(), evidence.bytes)
        assertEquals(1_000L, evidence.firstFrameAtMs)
    }

    @Test
    fun `non keyframes never parse and steady frames do not report a change`() {
        val monitor = StreamMonitor()
        assertTrue(monitor.frame(frame(pts = 0), atMs = 0, data = h264Main31, length = h264Main31.size))
        assertFalse(monitor.frame(frame(pts = 33), atMs = 33, data = h264Main31, length = h264Main31.size))
        val evidence = requireNotNull(monitor.evidence(33))
        assertNull(evidence.sps)
        assertNull(evidence.spsError)
        assertEquals(2, evidence.cadence.frames)
    }

    @Test
    fun `a keyframe without a parameter set records the parse error and retries on the next keyframe`() {
        val monitor = StreamMonitor()
        val slice = hex("00 00 00 01 65 88 84 00")
        assertTrue(monitor.frame(frame(key = true), atMs = 0, data = slice, length = slice.size))
        val failed = requireNotNull(monitor.evidence(0))
        assertNull(failed.sps)
        assertNotNull(failed.spsError)
        assertTrue(monitor.frame(frame(key = true), atMs = 1_000, data = h264Main31, length = h264Main31.size))
        val parsed = requireNotNull(monitor.evidence(1_000))
        assertEquals("Main", parsed.profile)
        assertNull(parsed.spsError)
    }

    @Test
    fun `sps search honours the buffer offset and the search bound`() {
        val padding = ByteArray(8)
        val buffer = padding + h264Main31
        // Six bytes end inside the SPS header, before profile_idc and level_idc can be read.
        val monitor = StreamMonitor(spsSearchBytes = 6)
        monitor.frame(frame(key = true), atMs = 0, data = buffer, offset = padding.size, length = h264Main31.size)
        assertNotNull(monitor.evidence(0)?.spsError)
        val wide = StreamMonitor()
        wide.frame(frame(key = true), atMs = 0, data = buffer, offset = padding.size, length = h264Main31.size)
        assertEquals("3.1", wide.evidence(0)?.level)
    }

    @Test
    fun `a descriptor change clears the sps until the next keyframe`() {
        val monitor = StreamMonitor()
        monitor.frame(frame(key = true), atMs = 0, data = h264Main31, length = h264Main31.size)
        assertEquals("Main", monitor.evidence(0)?.profile)
        assertTrue(monitor.frame(frame(width = 1920, height = 1080), atMs = 33))
        val switched = requireNotNull(monitor.evidence(33))
        assertEquals(1920, switched.width)
        assertNull(switched.sps)
        assertNull(switched.spsError)
        monitor.frame(frame(width = 1920, height = 1080, key = true), atMs = 66, data = h264Main31, length = h264Main31.size)
        assertEquals("Main", monitor.evidence(66)?.profile)
    }

    @Test
    fun `hevc and unknown mime types`() {
        val monitor = StreamMonitor()
        monitor.frame(frame(mime = "video/hevc", key = true), atMs = 0, data = h265Main31, length = h265Main31.size)
        val hevc = requireNotNull(monitor.evidence(0))
        assertEquals(VideoCodec.H265, hevc.codec)
        assertEquals("Main", hevc.profile)
        assertEquals("main", hevc.sps?.tier)
        val other = StreamMonitor()
        other.frame(frame(mime = "video/x-vnd.on2.vp9", key = true), atMs = 0, data = h264Main31, length = h264Main31.size)
        val unknown = requireNotNull(other.evidence(0))
        assertNull(unknown.codec)
        // No codec hint: the parser still finds the H.264 SPS in the buffer.
        assertEquals("Main", unknown.profile)
    }

    @Test
    fun `hint mismatch is an error not a wrong answer`() {
        val monitor = StreamMonitor()
        monitor.frame(frame(mime = "video/hevc", key = true), atMs = 0, data = h264Main31, length = h264Main31.size)
        val evidence = requireNotNull(monitor.evidence(0))
        assertNull(evidence.sps)
        assertTrue(evidence.spsError!!.contains("H265"))
    }

    @Test
    fun `reset forgets the stream`() {
        val monitor = StreamMonitor()
        monitor.frame(frame(key = true), atMs = 0, data = h264Main31, length = h264Main31.size)
        monitor.reset()
        assertNull(monitor.evidence(0))
        assertTrue(monitor.frame(frame(), atMs = 10))
        assertEquals(1, monitor.evidence(10)?.cadence?.frames)
    }
}
