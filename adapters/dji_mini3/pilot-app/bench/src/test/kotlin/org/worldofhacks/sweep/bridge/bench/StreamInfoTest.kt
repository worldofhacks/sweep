package org.worldofhacks.sweep.bridge.bench

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.video.CadenceStats
import org.worldofhacks.sweep.bridge.core.video.SpsInfo
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence
import org.worldofhacks.sweep.bridge.core.video.VideoCodec

class StreamInfoTest {
    private fun evidence(sps: SpsInfo? = SpsInfo(VideoCodec.H264, 77, "Main", 31, "3.1", null, 0x40), error: String? = null) =
        StreamEvidence(
            mimeType = "video/avc",
            codec = VideoCodec.H264,
            width = 1280,
            height = 720,
            nominalFrameRateHz = 30,
            cadence = CadenceStats(
                frames = 300,
                keyframes = 10,
                measuredFrameRateHz = 29.97,
                keyframeIntervalMs = 1_000,
                keyframeIntervalMinMs = 990,
                keyframeIntervalMaxMs = 1_010,
                keyframeIntervalFrames = 30,
                lastFrameAtMs = 10_000,
                lastKeyframeAtMs = 9_000,
            ),
            sps = sps,
            spsError = error,
            bytes = 3_000_000,
            firstFrameAtMs = 0,
        )

    @Test
    fun `stream info is one flat record with the phone state beside the codec evidence`() {
        val out = StringBuilder()
        val recorder = BenchRecorder(out, Clock { 5_000 })
        recorder.streamInfo(evidence(), phoneBatteryPercent = 81, phoneThermalState = "light")
        val record = Json.parse(out.toString().trim()) as JsonObject
        assertEquals(JsonString("stream_info"), record["kind"])
        assertEquals(JsonInt(5_000), record["t_ms"])
        assertEquals(JsonString("video/avc"), record["mime_type"])
        assertEquals(JsonString("H264"), record["codec"])
        assertEquals(JsonInt(1280), record["width"])
        assertEquals(JsonInt(720), record["height"])
        assertEquals(JsonInt(30), record["nominal_frame_rate_hz"])
        assertEquals(JsonFloat(29.97), record["measured_frame_rate_hz"])
        assertEquals(JsonInt(300), record["frames"])
        assertEquals(JsonInt(10), record["keyframes"])
        assertEquals(JsonInt(1_000), record["keyframe_interval_ms"])
        assertEquals(JsonInt(990), record["keyframe_interval_min_ms"])
        assertEquals(JsonInt(1_010), record["keyframe_interval_max_ms"])
        assertEquals(JsonInt(30), record["keyframe_interval_frames"])
        assertEquals(JsonString("Main"), record["profile"])
        assertEquals(JsonInt(77), record["profile_idc"])
        assertEquals(JsonString("3.1"), record["level"])
        assertEquals(JsonInt(31), record["level_idc"])
        assertEquals(JsonNull, record["tier"])
        assertEquals(JsonNull, record["sps_error"])
        assertEquals(JsonInt(3_000_000), record["bytes"])
        assertEquals(JsonInt(81), record["phone_battery_percent"])
        assertEquals(JsonString("light"), record["phone_thermal_state"])
    }

    @Test
    fun `analysis keeps the last stream info and counts the samples`() {
        val out = StringBuilder()
        var now = 0L
        val recorder = BenchRecorder(out, Clock { now })
        recorder.streamInfo(evidence(sps = null, error = "no H264 SPS NAL unit found in 8 bytes"))
        now = 1_000
        recorder.streamInfo(evidence(), phoneThermalState = "none")
        val report = BenchAnalysis.analyze(out.toString())
        val stream = requireNotNull(report.video.stream)
        assertEquals(2, stream.samples)
        assertEquals("video/avc", stream.mimeType)
        assertEquals("H264", stream.codec)
        assertEquals(1280L, stream.width)
        assertEquals(720L, stream.height)
        assertEquals(30L, stream.nominalFrameRateHz)
        assertEquals(29.97, stream.measuredFrameRateHz!!, 1e-9)
        assertEquals(1_000L, stream.keyframeIntervalMs)
        assertEquals(30L, stream.keyframeIntervalFrames)
        assertEquals("Main", stream.profile)
        assertEquals("3.1", stream.level)
        assertNull(stream.tier)
        assertNull(stream.spsError)
        assertEquals("none", stream.phoneThermalState)
        assertEquals(2, report.records)
        assertEquals(0, report.video.frames)
    }

    @Test
    fun `report writers carry the stream evidence`() {
        val out = StringBuilder()
        val recorder = BenchRecorder(out, Clock { 0 })
        recorder.videoFrame(sizeBytes = 60_000, keyframe = true)
        recorder.streamInfo(evidence(), phoneThermalState = "moderate")
        val report = BenchAnalysis.analyze(out.toString())
        val json = Json.parse(ReportWriter.json(report)) as JsonObject
        val video = json["video"] as JsonObject
        val stream = video["stream"] as JsonObject
        assertEquals(JsonString("Main"), stream["profile"])
        assertEquals(JsonInt(1), stream["samples"])
        val text = ReportWriter.text(report)
        assertTrue(text.contains("stream_mime: video/avc (H264)"))
        assertTrue(text.contains("stream_size: 1280x720"))
        assertTrue(text.contains("stream_nominal_hz: 30"))
        assertTrue(text.contains("stream_measured_hz: 29.97"))
        assertTrue(text.contains("stream_keyframe_interval: 1000 ms / 30 frames"))
        assertTrue(text.contains("stream_profile: Main level 3.1"))
        assertTrue(text.contains("stream_phone_thermal: moderate"))
        assertTrue(text.contains("stream_samples: 1"))
        assertTrue(text.contains("keyframes: 1"))
    }

    @Test
    fun `a run without stream info reports none`() {
        val out = StringBuilder()
        BenchRecorder(out, Clock { 0 }).note("no video")
        val report = BenchAnalysis.analyze(out.toString())
        assertNull(report.video.stream)
        assertTrue(!ReportWriter.text(report).contains("stream_mime"))
        val video = (Json.parse(ReportWriter.json(report)) as JsonObject)["video"] as JsonObject
        assertEquals(JsonNull, video["stream"])
    }
}
