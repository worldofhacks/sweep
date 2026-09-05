package org.worldofhacks.sweep.bridge.bench

import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

/** One JSONL record kind per measured quantity; the wire names are stable for analysis tools. */
enum class RecordKind(val wire: String) {
    COMMAND_SENT("command_sent"),
    COMMAND_ACKED("command_acked"),
    COMMAND_DROPPED("command_dropped"),
    STICK_SENT("stick_sent"),
    TELEMETRY("telemetry"),
    VIDEO_PUBLISH("video_publish"),
    VIDEO_FRAME("video_frame"),
    NOTE("note");

    companion object {
        fun fromWire(value: String): RecordKind? = entries.firstOrNull { it.wire == value }
    }
}

/**
 * Appends one canonical-JSON line per event to [sink]. Every line carries `kind` and
 * `t_ms` from the injected [clock]; command acknowledgements also carry the round-trip
 * time measured from the matching `command_sent` record.
 */
class BenchRecorder(private val sink: Appendable, private val clock: Clock) {
    private val inFlight = LinkedHashMap<String, Long>()

    fun commandSent(commandId: String, seq: Long, operation: String) {
        val now = clock.nowMs()
        inFlight[commandId] = now
        write(RecordKind.COMMAND_SENT, now, "command_id" to commandId, "seq" to seq, "operation" to operation)
    }

    /** Records the acknowledgement and returns the round-trip time, or null when the send was not recorded. */
    fun commandAcked(commandId: String, status: String): Long? {
        val now = clock.nowMs()
        val rtt = inFlight.remove(commandId)?.let { now - it }
        write(RecordKind.COMMAND_ACKED, now, "command_id" to commandId, "status" to status, "rtt_ms" to rtt)
        return rtt
    }

    fun commandDropped(commandId: String, reason: String) {
        val now = clock.nowMs()
        val waited = inFlight.remove(commandId)?.let { now - it }
        write(RecordKind.COMMAND_DROPPED, now, "command_id" to commandId, "reason" to reason, "waited_ms" to waited)
    }

    fun stickSent(seq: Long) {
        write(RecordKind.STICK_SENT, clock.nowMs(), "seq" to seq)
    }

    fun telemetry(droneId: Int, eventId: String) {
        write(RecordKind.TELEMETRY, clock.nowMs(), "drone_id" to droneId, "event_id" to eventId)
    }

    fun videoFrame(sizeBytes: Int, keyframe: Boolean, decodeMs: Long? = null, dropped: Boolean = false) {
        write(
            RecordKind.VIDEO_FRAME,
            clock.nowMs(),
            "size_bytes" to sizeBytes,
            "keyframe" to keyframe,
            "decode_ms" to decodeMs,
            "dropped" to dropped,
        )
    }

    /**
     * One-second window of the WHIP publisher (Phase F): sender bitrate and frame rate,
     * cumulative frames sent and dropped, ICE state, the selected candidate pair's round trip
     * (the LAN leg), and the Android processing time per frame (the phone leg).
     */
    fun videoPublish(
        source: String,
        bitrateKbps: Double?,
        fps: Double?,
        framesSent: Long,
        droppedFrames: Long,
        iceState: String,
        rttMs: Double?,
        processingMs: Double? = null,
        codec: String? = null,
        width: Int = 0,
        height: Int = 0,
        keyframeIntervalMs: Long? = null,
    ) {
        write(
            RecordKind.VIDEO_PUBLISH,
            clock.nowMs(),
            "source" to source,
            "bitrate_kbps" to bitrateKbps,
            "fps" to fps,
            "frames_sent" to framesSent,
            "dropped_frames" to droppedFrames,
            "ice_state" to iceState,
            "rtt_ms" to rttMs,
            "processing_ms" to processingMs,
            "codec" to codec,
            "width" to width,
            "height" to height,
            "keyframe_interval_ms" to keyframeIntervalMs,
        )
    }

    fun note(text: String) {
        write(RecordKind.NOTE, clock.nowMs(), "text" to text)
    }

    val pendingCommands: Set<String>
        get() = inFlight.keys.toSet()

    private fun write(kind: RecordKind, tMs: Long, vararg fields: Pair<String, Any?>) {
        val record: JsonObject = Json.json("kind" to kind.wire, "t_ms" to tMs, *fields)
        sink.append(Json.canonical(record)).append('\n')
    }
}
