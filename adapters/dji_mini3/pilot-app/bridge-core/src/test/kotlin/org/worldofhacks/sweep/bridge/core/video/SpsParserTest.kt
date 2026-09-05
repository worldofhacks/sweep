package org.worldofhacks.sweep.bridge.core.video

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class SpsParserTest {
    private fun hex(text: String): ByteArray =
        text.trim().split(Regex("\\s+")).map { it.toInt(16).toByte() }.toByteArray()

    // H.264 High profile, level 4.0, as emitted for 1080p streams.
    private val h264High40 = hex("67 64 00 28 AC D9 40 78 02 27 E5 84 00 00 03 00 04 00 00 03 00 F0 3C 60 C6 58")

    // H.264 Constrained Baseline (constraint_set0 + constraint_set1), level 3.0.
    private val h264ConstrainedBaseline30 = hex("67 42 C0 1E D9 00 F0 11 6E FF F0 11 11 D0 00 00 03 00 10 00 00 03 03 C0 F1 62 D9 60")

    // H.264 Main (constraint_set1), level 3.1.
    private val h264Main31 = hex("67 4D 40 1F E8 80 50 17 FC B0 80 00 00 03 00 80 00 00 19 07 8B 16 CB")

    // H.264 Baseline with constraint_set3 and level_idc 11 means level 1b.
    private val h264Level1b = hex("67 42 10 0B DA 02 80 F6 C0 44 00 00 03 00 04 00 00 03 00 F0 3C 58 B4 48")

    // H.265 Main profile, main tier, level_idc 93 (level 3.1). Contains emulation-prevention
    // bytes (00 00 03) that must be removed before reading the profile_tier_level fields.
    private val h265Main31 = hex("42 01 01 01 60 00 00 03 00 90 00 00 03 00 00 03 00 5D A0 02 80 80 2D 16 59 59 A4 93 2B C0 5A 70 80 00 01 F4 80 00 3A 98 04")

    // H.265 Main 10, high tier, level_idc 153 (level 5.1).
    private val h265Main10High51 = hex("42 01 01 22 20 00 00 03 00 90 00 00 03 00 00 03 00 99 A0 01 E0 20 02 1C 4D 8D 35 92 4C 92 B8 2E 5C 98 80 80 08 00 00 03 00 80 00 00 0F 40")

    @Test
    fun `h264 high 4_0`() {
        val info = SpsParser.parse(h264High40)
        assertEquals(SpsInfo(VideoCodec.H264, 100, "High", 40, "4.0", null, 0x00), info)
    }

    @Test
    fun `h264 constrained baseline 3_0`() {
        val info = SpsParser.parse(h264ConstrainedBaseline30)
        assertEquals(VideoCodec.H264, info.codec)
        assertEquals(66, info.profileIdc)
        assertEquals("Constrained Baseline", info.profileName)
        assertEquals(30, info.levelIdc)
        assertEquals("3.0", info.level)
        assertEquals(0xC0, info.constraintFlags)
    }

    @Test
    fun `h264 main 3_1`() {
        val info = SpsParser.parse(h264Main31)
        assertEquals("Main", info.profileName)
        assertEquals("3.1", info.level)
    }

    @Test
    fun `h264 level 1b via constraint_set3`() {
        val info = SpsParser.parse(h264Level1b)
        assertEquals("Baseline", info.profileName)
        assertEquals(11, info.levelIdc)
        assertEquals("1b", info.level)
    }

    @Test
    fun `annex b prefix and trailing pps are tolerated`() {
        val pps = hex("00 00 00 01 68 EE 3C B0")
        val withStartCode = hex("00 00 00 01") + h264High40 + pps
        assertEquals("4.0", SpsParser.parse(withStartCode).level)
        val threeByteStartCode = hex("00 00 01") + h264Main31
        assertEquals("3.1", SpsParser.parse(threeByteStartCode).level)
    }

    @Test
    fun `h265 main 3_1 with emulation prevention bytes`() {
        val info = SpsParser.parse(h265Main31)
        assertEquals(SpsInfo(VideoCodec.H265, 1, "Main", 93, "3.1", "main", null), info)
    }

    @Test
    fun `h265 main 10 high tier 5_1`() {
        val info = SpsParser.parse(h265Main10High51)
        assertEquals(VideoCodec.H265, info.codec)
        assertEquals(2, info.profileIdc)
        assertEquals("Main 10", info.profileName)
        assertEquals(153, info.levelIdc)
        assertEquals("5.1", info.level)
        assertEquals("high", info.tier)
    }

    @Test
    fun `h265 sps after a vps in the same buffer`() {
        val vps = hex("00 00 00 01 40 01 0C 01 FF FF 01 60 00 00 03 00 90 00 00 03 00 00 03 00 5D 95 98 09")
        val buffer = vps + hex("00 00 00 01") + h265Main31
        assertEquals("3.1", SpsParser.parse(buffer).level)
        assertEquals("3.1", SpsParser.parse(buffer, codecHint = VideoCodec.H265).level)
    }

    @Test
    fun `codec hint must match the nal unit`() {
        assertThrows(SpsParseException::class.java) { SpsParser.parse(h264High40, codecHint = VideoCodec.H265) }
        assertThrows(SpsParseException::class.java) { SpsParser.parse(h265Main31, codecHint = VideoCodec.H264) }
    }

    @Test
    fun `non sps input is refused`() {
        assertThrows(SpsParseException::class.java) { SpsParser.parse(hex("68 EE 3C B0")) }
        assertThrows(SpsParseException::class.java) { SpsParser.parse(ByteArray(0)) }
        assertThrows(SpsParseException::class.java) { SpsParser.parse(hex("67 64")) }
    }

    @Test
    fun `mime types name the codecs`() {
        assertEquals("video/avc", VideoCodec.H264.mime)
        assertEquals("video/hevc", VideoCodec.H265.mime)
        assertEquals(VideoCodec.H265, VideoCodec.fromMime("video/hevc"))
        assertEquals(null, VideoCodec.fromMime("video/x-vnd.on2.vp9"))
    }
}
