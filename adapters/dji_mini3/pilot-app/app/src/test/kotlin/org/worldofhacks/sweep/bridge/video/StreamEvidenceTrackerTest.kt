package org.worldofhacks.sweep.bridge.video

import java.io.File
import java.nio.file.Files
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.video.StreamFrame

/** Plain JVM: the tracker touches no Android class, only files, flows, and the bench recorder. */
class StreamEvidenceTrackerTest {
    @Volatile
    private var now = 1_000L
    private val clock = Clock { now }

    private fun frame(key: Boolean = false) = StreamFrame("video/avc", 1280, 720, 30, key, 0, 1_000)

    private fun tracker(directory: File? = null, publishIntervalMs: Long = 250, logIntervalMs: Long = 1_000) =
        StreamEvidenceTracker(
            directory,
            phone = null,
            clock = clock,
            publishIntervalMs = publishIntervalMs,
            logIntervalMs = logIntervalMs,
            receivedAtMonotonicMs = { now + 10_000 },
        )

    @Test
    fun `reset clears the evidence but keeps the last frame time`() {
        val tracker = tracker()
        assertNull(tracker.evidence.value)
        assertNull(tracker.lastFrameAtMs.value)
        tracker.frame(frame(key = true), data = null, offset = 0, length = 0)
        assertEquals(1L, tracker.evidence.value?.cadence?.frames)
        assertEquals(1_000L, tracker.lastFrameAtMs.value)
        tracker.reset()
        assertNull(tracker.evidence.value)
        assertEquals(1_000L, tracker.lastFrameAtMs.value)
        now = 5_000
        tracker.frame(frame(), data = null, offset = 0, length = 0)
        assertEquals(1L, tracker.evidence.value?.cadence?.frames)
        assertEquals(5_000L, tracker.lastFrameAtMs.value)
    }

    @Test
    fun `evidence reaches the screen on a change and then once per publish interval`() {
        val tracker = tracker(publishIntervalMs = 250)
        tracker.frame(frame(), null, 0, 0)
        now = 1_100
        tracker.frame(frame(), null, 0, 0)
        assertEquals(1L, tracker.evidence.value?.cadence?.frames)
        assertEquals(1_000L, tracker.lastFrameAtMs.value)
        now = 1_300
        tracker.frame(frame(), null, 0, 0)
        assertEquals(3L, tracker.evidence.value?.cadence?.frames)
        assertEquals(1_300L, tracker.lastFrameAtMs.value)
    }

    @Test
    fun `the bench log holds video_frame and stream_info records between start and stop`() {
        val directory = Files.createTempDirectory("stream-evidence").toFile()
        try {
            val tracker = tracker(directory)
            tracker.start()
            tracker.frame(frame(key = true), SPS_AND_PPS, 0, SPS_AND_PPS.size)
            now = 1_033
            tracker.frame(frame(), null, 0, 0)
            tracker.stop("surface detached")
            val file = awaitClosedLog(directory)
            val records = file.readLines().filter { it.isNotBlank() }.map { Json.parse(it) as JsonObject }
            assertEquals(
                listOf("note", "video_frame", "stream_info", "video_frame", "note"),
                records.map { (it["kind"] as JsonString).value },
            )
            val info = records[2]
            assertEquals(JsonString("video/avc"), info["mime_type"])
            assertEquals(JsonString("Main"), info["profile"])
            assertEquals(JsonString("3.1"), info["level"])
            assertEquals(JsonString("not_exposed_by_receive_stream_listener"), records[1]["decode_time_status"])
            assertEquals(1L, records[1].integer("frame_sequence"))
            assertEquals(0L, records[1].integer("sdk_presentation_time_ms"))
            assertEquals(11_000L, records[1].integer("received_at_android_elapsed_realtime_ms"))
            assertEquals(JsonString("surface detached"), records.last()["text"])
            assertEquals(file.absolutePath, tracker.logPath.value)
        } finally {
            directory.deleteRecursively()
        }
    }

    /** The worker thread writes asynchronously; wait, bounded, for the closing note to land. */
    private fun awaitClosedLog(directory: File): File {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            val file = File(directory, StreamEvidenceTracker.LOG_DIRECTORY).listFiles()?.firstOrNull { it.name.endsWith(".jsonl") }
            if (file != null && file.readText().contains("surface detached")) return file
            Thread.sleep(20)
        }
        throw AssertionError("the stream evidence log was not closed within 5 s")
    }

    private fun JsonObject.integer(name: String) = (get(name) as org.worldofhacks.sweep.bridge.core.json.JsonInt).value

    private companion object {
        /** Annex B H.264 SPS (Main, level 3.1) and PPS, as an IDR access unit begins. */
        val SPS_AND_PPS: ByteArray = intArrayOf(
            0x00, 0x00, 0x00, 0x01, 0x67, 0x4D, 0x40, 0x1F, 0xE8, 0x80, 0x50, 0x17, 0xFC, 0xB0, 0x80,
            0x00, 0x00, 0x03, 0x00, 0x80, 0x00, 0x00, 0x19, 0x07, 0x8B, 0x16, 0xCB,
            0x00, 0x00, 0x00, 0x01, 0x68, 0xEE, 0x3C, 0xB0,
        ).map { it.toByte() }.toByteArray()
    }
}
