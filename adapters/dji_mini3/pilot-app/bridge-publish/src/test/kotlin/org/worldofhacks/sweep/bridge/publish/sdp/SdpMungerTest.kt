package org.worldofhacks.sweep.bridge.publish.sdp

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class SdpMungerTest {
    private val offer = listOf(
        "v=0",
        "o=- 4 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97 98 99 100 101",
        "c=IN IP4 0.0.0.0",
        "a=rtpmap:96 VP8/90000",
        "a=rtcp-fb:96 nack",
        "a=rtpmap:97 rtx/90000",
        "a=fmtp:97 apt=96",
        "a=rtpmap:98 H264/90000",
        "a=rtcp-fb:98 nack pli",
        "a=fmtp:98 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=rtpmap:99 rtx/90000",
        "a=fmtp:99 apt=98",
        "a=rtpmap:100 red/90000",
        "a=rtpmap:101 ulpfec/90000",
        "a=sendonly",
    ).joinToString("\r\n")

    @Test
    fun `forceH264Only keeps h264 its rtx red and ulpfec and drops vp8`() {
        val logs = ArrayList<String>()
        val munged = SdpMunger { logs += it }.forceH264Only(offer)
        val lines = munged.split("\r\n")
        assertEquals("m=video 9 UDP/TLS/RTP/SAVPF 98 99 100 101", lines.first { it.startsWith("m=video") })
        assertFalse(lines.any { it.contains("VP8") || it == "a=fmtp:97 apt=96" || it == "a=rtcp-fb:96 nack" })
        assertTrue(lines.contains("a=rtpmap:98 H264/90000"))
        assertTrue(lines.contains("a=fmtp:99 apt=98"))
        assertTrue(lines.contains("a=sendonly"))
        assertTrue(munged.contains("\r\n"))
        assertTrue(logs.single().startsWith("Forced H264-only"))
    }

    @Test
    fun `an offer without h264 is left unchanged so vp8 can still be negotiated`() {
        val vp8Only = offer.split("\r\n")
            .map { if (it.startsWith("m=video")) "m=video 9 UDP/TLS/RTP/SAVPF 96 97 100 101" else it }
            .filterNot { it.startsWith("a=rtpmap:98") || it.startsWith("a=rtcp-fb:98") || it.startsWith("a=fmtp:98") || it.startsWith("a=rtpmap:99") || it.startsWith("a=fmtp:99") }
            .joinToString("\r\n")
        val logs = ArrayList<String>()
        val munger = SdpMunger { logs += it }
        assertEquals(vp8Only, munger.forceH264Only(vp8Only))
        assertEquals(vp8Only, munger.mungeForH264(vp8Only))
        assertEquals(2, logs.size)
        assertTrue(logs.all { it.startsWith("No H264 codec found") && it.contains("VP8") }, logs.toString())
        assertEquals(listOf("VP8", "rtx", "red", "ulpfec"), SdpMunger.videoCodecs(vp8Only))
    }

    @Test
    fun `keyframe interval is appended once to every h264 fmtp line`() {
        val munger = SdpMunger()
        val once = munger.setKeyframeInterval(offer, 2000)
        val twice = munger.setKeyframeInterval(once, 2000)
        assertEquals(once, twice)
        assertTrue(once.split("\r\n").contains("a=fmtp:98 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f;x-google-max-keyframe-interval=2000"))
        assertEquals(1, once.split("x-google-max-keyframe-interval").size - 1)
        assertEquals(offer, munger.setKeyframeInterval(offer, 0))
    }

    @Test
    fun `mungeForH264 combines both steps and copes with bare newlines`() {
        val lf = offer.replace("\r\n", "\n")
        val munged = SdpMunger().mungeForH264(lf, 1500)
        assertFalse(munged.contains("\r\n"))
        assertTrue(munged.contains("x-google-max-keyframe-interval=1500"))
        assertEquals(listOf("H264", "rtx", "red", "ulpfec"), SdpMunger.videoCodecs(munged))
    }

    @Test
    fun `an sdp without a video section is returned as is`() {
        val audioOnly = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n"
        assertEquals(audioOnly, SdpMunger().mungeForH264(audioOnly))
        assertTrue(SdpMunger.videoCodecs(audioOnly).isEmpty())
        assertNull(SdpMunger.negotiatedVideoCodec(audioOnly))
    }

    @Test
    fun `the negotiated codec is the answer's first real video codec`() {
        val answer = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 99 98\r\na=rtpmap:99 rtx/90000\r\na=fmtp:99 apt=98\r\na=rtpmap:98 H264/90000\r\n"
        assertEquals("H264", SdpMunger.negotiatedVideoCodec(answer))
        assertEquals("VP8", SdpMunger.negotiatedVideoCodec(offer))
    }
}
