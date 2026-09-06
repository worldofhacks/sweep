package org.worldofhacks.sweep.bridge.publish.codec

import org.junit.jupiter.api.Assertions.assertArrayEquals
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.video.VideoCodec
import org.worldofhacks.sweep.bridge.publish.PublishReasons

class CodecGateTest {
    private fun hex(text: String): ByteArray =
        text.trim().split(Regex("\\s+")).map { it.toInt(16).toByte() }.toByteArray()

    private val startCode = hex("00 00 00 01")

    // The same vectors bridge-core's SpsParserTest uses.
    private val h264High40 = hex("67 64 00 28 AC D9 40 78 02 27 E5 84 00 00 03 00 04 00 00 03 00 F0 3C 60 C6 58")
    private val h264ConstrainedBaseline30 = hex("67 42 C0 1E D9 00 F0 11 6E FF F0 11 11 D0 00 00 03 00 10 00 00 03 03 C0 F1 62 D9 60")
    private val h264Main31 = hex("67 4D 40 1F E8 80 50 17 FC B0 80 00 00 03 00 80 00 00 19 07 8B 16 CB")
    private val h264High10 = hex("67 6E 00 28 AC D9 40 78 02 27 E5 84 00 00 03 00 04 00 00 03 00 F0 3C 60 C6 58")
    private val h265Main31 = hex("42 01 01 01 60 00 00 03 00 90 00 00 03 00 00 03 00 5D A0 02 80 80 2D 16 59 59 A4 93 2B C0 5A 70 80 00 01 F4 80 00 3A 98 04")
    private val pps = hex("68 EE 3C B0")

    // IDR slice: first_mb_in_slice ue(0)=1, slice_type ue(7)=0001000 → bits 1 0001000 → 0x88.
    private val idrSlice = hex("65 88 84 00 20 00 00 03 00 40")

    // Non-IDR P slice: ue(0)=1, ue(0)=1 → 11xxxxxx → 0xC0.
    private val pSlice = hex("41 C0 00 20 00 00 03 00 40")

    // Non-IDR B slice: ue(0)=1, ue(1)=010 → 1010xxxx → 0xA8; and slice_type 6: ue(6)=00111 → 1 00111 → 0x9C.
    private val bSlice = hex("41 A8 00 20 00 00 03 00 40")
    private val bSliceAll = hex("41 9C 00 20 00 00 03 00 40")

    private fun keyframe(sps: ByteArray): ByteArray = startCode + sps + startCode + pps + startCode + idrSlice

    @Test
    fun `h264 baseline main and high pass the gate with the profile spelled out`() {
        for ((sps, profile) in listOf(h264ConstrainedBaseline30 to "Constrained Baseline", h264Main31 to "Main", h264High40 to "High")) {
            val evidence = CodecGate.evidence("H264", keyframe(sps), 1280, 720, 30, bSlices = false, keyframeIntervalMs = 1000)
            val decision = CodecGate.evaluate(evidence)
            assertTrue(decision.supported, decision.detail)
            assertNull(decision.reason)
            assertEquals(VideoCodec.H264, evidence.codec)
            assertEquals(profile, evidence.sps?.profileName)
            assertTrue(decision.detail.startsWith("H264 $profile"), decision.detail)
            assertTrue(decision.detail.contains("1280x720 30fps keyframe/1000ms"), decision.detail)
        }
    }

    @Test
    fun `h265 fails closed with codec_unsupported and the profile in the detail`() {
        val evidence = CodecGate.evidence("H265", startCode + h265Main31, 1280, 720, 30, bSlices = false, keyframeIntervalMs = null)
        val decision = CodecGate.evaluate(evidence)
        assertFalse(decision.supported)
        assertEquals(PublishReasons.CODEC_UNSUPPORTED, decision.reason)
        assertTrue(decision.detail.contains("H.265"), decision.detail)
        assertTrue(decision.detail.contains("H265 Main 3.1 1280x720 30fps"), decision.detail)
        assertTrue(PublishReasons.isTerminal(decision.reason!!))
    }

    @Test
    fun `h264 profiles browsers do not decode and b slices are refused`() {
        val high10 = CodecGate.evaluate(CodecGate.evidence("video/avc", keyframe(h264High10), 0, 0, 0, bSlices = false, keyframeIntervalMs = null))
        assertFalse(high10.supported)
        assertEquals(PublishReasons.CODEC_UNSUPPORTED, high10.reason)
        assertTrue(high10.detail.contains("High 10 (profile_idc 110)"), high10.detail)

        val bFrames = CodecGate.evaluate(CodecGate.evidence("H264", keyframe(h264High40), 1920, 1080, 30, bSlices = true, keyframeIntervalMs = null))
        assertFalse(bFrames.supported)
        assertTrue(bFrames.detail.contains("B slices"), bFrames.detail)

        val noSps = CodecGate.evaluate(CodecGate.evidence("H264", startCode + idrSlice, 1280, 720, 30, bSlices = false, keyframeIntervalMs = null))
        assertFalse(noSps.supported)
        assertTrue(noSps.detail.contains("without a readable SPS"), noSps.detail)

        val unknown = CodecGate.evaluate(CodecGate.evidence("MJPEG", startCode + idrSlice, 0, 0, 0, bSlices = false, keyframeIntervalMs = null))
        assertFalse(unknown.supported)
        assertTrue(unknown.detail.contains("unknown stream mime type MJPEG"))
    }

    @Test
    fun `mime names from the sdk and mime types both resolve`() {
        assertEquals(VideoCodec.H264, CodecGate.codecOf("H264"))
        assertEquals(VideoCodec.H264, CodecGate.codecOf("video/avc"))
        assertEquals(VideoCodec.H265, CodecGate.codecOf("h265"))
        assertEquals(VideoCodec.H265, CodecGate.codecOf("video/hevc"))
        assertNull(CodecGate.codecOf("video/x-vnd.on2.vp8"))
    }

    @Test
    fun `slice headers reveal b slices and parameter sets are cached and prepended`() {
        assertFalse(H264Slices.containsBSlice(keyframe(h264High40)))
        assertFalse(H264Slices.containsBSlice(startCode + pSlice))
        assertTrue(H264Slices.containsBSlice(startCode + bSlice))
        assertTrue(H264Slices.containsBSlice(startCode + pSlice + startCode + bSliceAll))
        assertFalse(H264Slices.containsBSlice(startCode + pps))

        val sets = requireNotNull(H264Slices.parameterSets(keyframe(h264High40)))
        assertArrayEquals(startCode + h264High40 + startCode + pps, sets)
        assertNull(H264Slices.parameterSets(startCode + idrSlice))
        assertTrue(H264Slices.hasSps(keyframe(h264High40)))
        assertFalse(H264Slices.hasSps(startCode + idrSlice))
        val repaired = H264Slices.prependParameterSets(startCode + idrSlice, sets)
        assertTrue(H264Slices.hasSps(repaired))
        assertEquals(3, AnnexB.nalUnits(repaired).size)
    }

    @Test
    fun `annex b is passed through and avcc length prefixes become start codes`() {
        val annexB = keyframe(h264Main31)
        assertTrue(AnnexB.startsWithStartCode(annexB))
        assertArrayEquals(annexB, AnnexB.normalize(annexB))
        assertArrayEquals(hex("00 00 01 65 88"), AnnexB.normalize(hex("FF 00 00 01 65 88"), 1, 5))

        fun avcc(vararg units: ByteArray): ByteArray = units.fold(ByteArray(0)) { acc, unit ->
            val size = unit.size
            acc + byteArrayOf((size ushr 24).toByte(), (size ushr 16).toByte(), (size ushr 8).toByte(), size.toByte()) + unit
        }
        val lengthPrefixed = avcc(h264Main31, pps, idrSlice)
        assertFalse(AnnexB.startsWithStartCode(lengthPrefixed))
        assertTrue(AnnexB.looksLikeAvcc(lengthPrefixed))
        assertArrayEquals(annexB, AnnexB.normalize(lengthPrefixed))
        assertFalse(AnnexB.looksLikeAvcc(hex("00 00 00 09 01 02")))
        assertArrayEquals(hex("12 34"), AnnexB.normalize(hex("12 34")))
        assertEquals(listOf(4 until 4 + h264Main31.size, 8 + h264Main31.size until 8 + h264Main31.size + pps.size), AnnexB.nalUnits(annexB).take(2))
        assertEquals(7, AnnexB.h264NalType(h264Main31[0]))
        assertTrue(AnnexB.nalUnits(ByteArray(0)).isEmpty())
    }
}
