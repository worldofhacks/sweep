package org.worldofhacks.sweep.bridge.bench

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

private class StepClock(private var nowMs: Long) : Clock {
    override fun nowMs(): Long = nowMs

    fun advance(deltaMs: Long) {
        nowMs += deltaMs
    }
}

class BenchRecorderTest {
    @Test
    fun `writes one canonical json line per record with the injected clock`() {
        val clock = StepClock(1_000)
        val out = StringBuilder()
        val recorder = BenchRecorder(out, clock)
        recorder.commandSent("cmd-1", seq = 1, operation = "hover")
        clock.advance(40)
        assertEquals(40L, recorder.commandAcked("cmd-1", "accepted"))
        recorder.stickSent(seq = 1)
        recorder.telemetry(droneId = 1, eventId = "evt-1")
        recorder.videoFrame(sizeBytes = 1200, keyframe = true, decodeMs = 7)
        recorder.note("bench start")
        assertEquals(
            listOf(
                """{"command_id":"cmd-1","kind":"command_sent","operation":"hover","seq":1,"t_ms":1000}""",
                """{"command_id":"cmd-1","kind":"command_acked","rtt_ms":40,"status":"accepted","t_ms":1040}""",
                """{"kind":"stick_sent","seq":1,"t_ms":1040}""",
                """{"drone_id":1,"event_id":"evt-1","kind":"telemetry","t_ms":1040}""",
                """{"decode_ms":7,"dropped":false,"keyframe":true,"kind":"video_frame","size_bytes":1200,"t_ms":1040}""",
                """{"kind":"note","t_ms":1040,"text":"bench start"}""",
            ),
            out.toString().trimEnd().lines(),
        )
    }

    @Test
    fun `acks without a recorded send have no rtt and drops clear the pending set`() {
        val clock = StepClock(0)
        val out = StringBuilder()
        val recorder = BenchRecorder(out, clock)
        assertNull(recorder.commandAcked("unknown", "completed"))
        recorder.commandSent("cmd-2", seq = 2, operation = "goto")
        assertEquals(setOf("cmd-2"), recorder.pendingCommands)
        clock.advance(2_000)
        recorder.commandDropped("cmd-2", "timeout")
        assertEquals(emptySet<String>(), recorder.pendingCommands)
        assertTrue(out.toString().contains(""""rtt_ms":null"""))
        assertTrue(out.toString().contains(""""waited_ms":2000"""))
    }
}

class BenchAnalysisTest {
    private fun log(): String {
        val clock = StepClock(10_000)
        val out = StringBuilder()
        val recorder = BenchRecorder(out, clock)
        recorder.note("guarded hover")
        // Five commands: rtts 10, 30, 20, 100 and one dropped, one never acknowledged.
        val rtts = listOf(10L, 30L, 20L, 100L)
        rtts.forEachIndexed { index, rtt ->
            recorder.commandSent("cmd-$index", seq = index.toLong(), operation = "hover")
            clock.advance(rtt)
            recorder.commandAcked("cmd-$index", "completed")
        }
        recorder.commandSent("cmd-drop", seq = 10, operation = "goto")
        clock.advance(1_500)
        recorder.commandDropped("cmd-drop", "timeout")
        recorder.commandSent("cmd-lost", seq = 11, operation = "goto")
        // Sticks at 10 Hz for one second (11 sends), telemetry at 5 Hz (6 sends).
        val stickStart = clock.nowMs()
        repeat(11) { i ->
            recorder.stickSent(seq = i.toLong())
            if (i % 2 == 0) recorder.telemetry(droneId = 1, eventId = "evt-$i")
            if (i < 10) clock.advance(100)
        }
        check(clock.nowMs() - stickStart == 1_000L)
        // Video: 4 frames over 100 ms (30 fps cadence), one keyframe, one dropped, decode 5,6,7,8.
        repeat(4) { i ->
            recorder.videoFrame(sizeBytes = 1_000, keyframe = i == 0, decodeMs = 5L + i)
            if (i < 3) clock.advance(33)
        }
        recorder.videoFrame(sizeBytes = 0, keyframe = false, dropped = true)
        return out.toString()
    }

    @Test
    fun `command rtt jitter and drops`() {
        val report = BenchAnalysis.analyze(log())
        assertEquals(6, report.commands.sent)
        assertEquals(4, report.commands.acked)
        assertEquals(1, report.commands.dropped)
        assertEquals(1, report.commands.unacknowledged)
        val rtt = requireNotNull(report.commands.rtt)
        assertEquals(4, rtt.count)
        assertEquals(10L, rtt.minMs)
        assertEquals(100L, rtt.maxMs)
        assertEquals(40.0, rtt.meanMs)
        assertEquals(20L, rtt.p50Ms) // nearest rank: ceil(0.5 * 4) = 2nd of [10, 20, 30, 100]
        assertEquals(100L, rtt.p95Ms) // ceil(0.95 * 4) = 4th
        // consecutive differences in ack order: |30-10| + |20-30| + |100-20| = 20 + 10 + 80
        assertEquals((20 + 10 + 80) / 3.0, report.commands.jitterMs!!, 1e-9)
    }

    @Test
    fun `stick and telemetry rates`() {
        val report = BenchAnalysis.analyze(log())
        assertEquals(11, report.sticks.count)
        assertEquals(1_000L, report.sticks.durationMs)
        assertEquals(10.0, report.sticks.rateHz!!, 1e-9)
        assertEquals(6, report.telemetry.count)
        assertEquals(5.0, report.telemetry.rateHz!!, 1e-9)
    }

    @Test
    fun `video frame stats`() {
        val report = BenchAnalysis.analyze(log())
        assertEquals(4, report.video.frames)
        assertEquals(1, report.video.keyframes)
        assertEquals(1, report.video.dropped)
        assertEquals(4_000L, report.video.bytes)
        assertEquals(3 * 1000.0 / 99, report.video.rate.rateHz!!, 1e-9)
        val decode = requireNotNull(report.video.decode)
        assertEquals(5L, decode.minMs)
        assertEquals(8L, decode.p95Ms)
    }

    @Test
    fun `percentiles use nearest rank`() {
        val samples = (1L..20L).toList()
        assertEquals(10L, LatencyStats.percentile(samples, 0.50))
        assertEquals(19L, LatencyStats.percentile(samples, 0.95))
        assertEquals(20L, LatencyStats.percentile(samples, 1.0))
        assertEquals(1L, LatencyStats.percentile(listOf(1L), 0.95))
    }

    @Test
    fun `malformed and unknown lines are counted not fatal`() {
        val report = BenchAnalysis.analyze(
            """
            {"kind":"note","t_ms":1,"text":"ok"}
            not json
            {"kind":"mystery","t_ms":2}
            {"kind":"telemetry"}

            """.trimIndent(),
        )
        assertEquals(1, report.records)
        assertEquals(3, report.skippedLines)
        assertEquals(listOf("ok"), report.notes)
        assertNull(report.sticks.rateHz)
        assertNull(report.commands.rtt)
    }

    @Test
    fun `report writers produce json and text`() {
        val report = BenchAnalysis.analyze(log())
        val json = Json.parse(ReportWriter.json(report)) as JsonObject
        assertEquals(setOf("commands", "sticks", "telemetry", "video", "notes", "records", "skipped_lines", "first_t_ms", "last_t_ms"), json.keys)
        val text = ReportWriter.text(report)
        assertTrue(text.contains("sent: 6"))
        assertTrue(text.contains("p95=100"))
        assertTrue(text.contains("jitter_ms: 36.67"))
        assertTrue(text.contains("rate_hz: 10.00"))
        assertTrue(text.contains("guarded hover"))
    }
}
