package org.worldofhacks.sweep.bridge.bench

import kotlin.math.abs
import kotlin.math.ceil
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonParseException
import org.worldofhacks.sweep.bridge.core.json.JsonString

/** Nearest-rank percentiles over a set of millisecond samples. */
data class LatencyStats(
    val count: Int,
    val minMs: Long,
    val meanMs: Double,
    val p50Ms: Long,
    val p95Ms: Long,
    val maxMs: Long,
) {
    fun toJson(): JsonObject = Json.json(
        "count" to count,
        "min_ms" to minMs,
        "mean_ms" to meanMs,
        "p50_ms" to p50Ms,
        "p95_ms" to p95Ms,
        "max_ms" to maxMs,
    )

    companion object {
        fun of(samples: List<Long>): LatencyStats? {
            if (samples.isEmpty()) return null
            val sorted = samples.sorted()
            return LatencyStats(
                count = sorted.size,
                minMs = sorted.first(),
                meanMs = sorted.sum().toDouble() / sorted.size,
                p50Ms = percentile(sorted, 0.50),
                p95Ms = percentile(sorted, 0.95),
                maxMs = sorted.last(),
            )
        }

        /** Nearest-rank: the value at ceil(q * n), 1-based, over ascending samples. */
        fun percentile(sorted: List<Long>, q: Double): Long {
            val rank = ceil(q * sorted.size).toInt().coerceIn(1, sorted.size)
            return sorted[rank - 1]
        }
    }
}

/** Count and steady-state rate of a periodic record kind. */
data class RateStats(val count: Int, val durationMs: Long, val rateHz: Double?) {
    fun toJson(): JsonObject = Json.json("count" to count, "duration_ms" to durationMs, "rate_hz" to rateHz)

    companion object {
        fun of(timestamps: List<Long>): RateStats {
            if (timestamps.size < 2) return RateStats(timestamps.size, 0, null)
            val duration = timestamps.last() - timestamps.first()
            val rate = if (duration > 0) (timestamps.size - 1) * 1000.0 / duration else null
            return RateStats(timestamps.size, duration, rate)
        }
    }
}

data class CommandStats(
    val sent: Int,
    val acked: Int,
    val dropped: Int,
    val unacknowledged: Int,
    val rtt: LatencyStats?,
    /** Mean absolute difference between consecutive round-trip times, in acknowledgement order. */
    val jitterMs: Double?,
) {
    fun toJson(): JsonObject = Json.json(
        "sent" to sent,
        "acked" to acked,
        "dropped" to dropped,
        "unacknowledged" to unacknowledged,
        "rtt" to rtt?.toJson(),
        "jitter_ms" to jitterMs,
    )
}

/** The codec evidence as the last `stream_info` record of the run left it (Phase D). */
data class StreamSummary(
    val mimeType: String?,
    val codec: String?,
    val width: Long?,
    val height: Long?,
    val nominalFrameRateHz: Long?,
    val measuredFrameRateHz: Double?,
    val keyframeIntervalMs: Long?,
    val keyframeIntervalFrames: Long?,
    val profile: String?,
    val level: String?,
    val tier: String?,
    val spsError: String?,
    val phoneThermalState: String?,
    /** How many `stream_info` records the run holds. */
    val samples: Int,
) {
    fun toJson(): JsonObject = Json.json(
        "mime_type" to mimeType,
        "codec" to codec,
        "width" to width,
        "height" to height,
        "nominal_frame_rate_hz" to nominalFrameRateHz,
        "measured_frame_rate_hz" to measuredFrameRateHz,
        "keyframe_interval_ms" to keyframeIntervalMs,
        "keyframe_interval_frames" to keyframeIntervalFrames,
        "profile" to profile,
        "level" to level,
        "tier" to tier,
        "sps_error" to spsError,
        "phone_thermal_state" to phoneThermalState,
        "samples" to samples,
    )
}

data class VideoStats(
    val frames: Int,
    val keyframes: Int,
    val dropped: Int,
    val bytes: Long,
    val rate: RateStats,
    val decode: LatencyStats?,
    val stream: StreamSummary? = null,
) {
    fun toJson(): JsonObject = Json.json(
        "frames" to frames,
        "keyframes" to keyframes,
        "dropped" to dropped,
        "bytes" to bytes,
        "rate" to rate.toJson(),
        "decode" to decode?.toJson(),
        "stream" to stream?.toJson(),
    )
}

/** Publish windows (Phase F): the LAN leg is the RTT percentile, the phone leg the processing time. */
data class PublishStats(
    val windows: Int,
    val connectedWindows: Int,
    val meanBitrateKbps: Double?,
    val meanFps: Double?,
    val droppedFrames: Long,
    val rtt: LatencyStats?,
    val meanProcessingMs: Double?,
    val sources: List<String>,
    val codecs: List<String>,
) {
    fun toJson(): JsonObject = Json.json(
        "windows" to windows,
        "connected_windows" to connectedWindows,
        "mean_bitrate_kbps" to meanBitrateKbps,
        "mean_fps" to meanFps,
        "dropped_frames" to droppedFrames,
        "rtt" to rtt?.toJson(),
        "mean_processing_ms" to meanProcessingMs,
        "sources" to sources,
        "codecs" to codecs,
    )
}

/** A JSON number as a double; the publish windows carry integers and floats in the same fields. */
private fun JsonObject.doubleOf(key: String): Double? = when (val value = this[key]) {
    is JsonFloat -> value.value
    is JsonInt -> value.value.toDouble()
    else -> null
}

data class BenchReport(
    val commands: CommandStats,
    val sticks: RateStats,
    val telemetry: RateStats,
    val video: VideoStats,
    val publish: PublishStats,
    val notes: List<String>,
    val records: Int,
    val skippedLines: Int,
    val firstTMs: Long?,
    val lastTMs: Long?,
) {
    fun toJson(): JsonObject = Json.json(
        "commands" to commands.toJson(),
        "sticks" to sticks.toJson(),
        "telemetry" to telemetry.toJson(),
        "video" to video.toJson(),
        "video_publish" to publish.toJson(),
        "notes" to notes,
        "records" to records,
        "skipped_lines" to skippedLines,
        "first_t_ms" to firstTMs,
        "last_t_ms" to lastTMs,
    )
}

/** Folds a bench JSONL log into a [BenchReport]. Unknown kinds and malformed lines are counted, not fatal. */
object BenchAnalysis {
    fun analyze(text: String): BenchReport = analyze(text.lineSequence())

    fun analyze(lines: Sequence<String>): BenchReport {
        val sent = LinkedHashMap<String, Long>()
        val rtts = ArrayList<Long>()
        var acked = 0
        var dropped = 0
        val sticks = ArrayList<Long>()
        val telemetry = ArrayList<Long>()
        val frames = ArrayList<Long>()
        var keyframes = 0
        var droppedFrames = 0
        var bytes = 0L
        var publishWindows = 0
        var publishConnected = 0
        val publishBitrates = ArrayList<Double>()
        val publishFps = ArrayList<Double>()
        var publishDropped = 0L
        val publishRtts = ArrayList<Long>()
        val publishProcessing = ArrayList<Double>()
        val publishSources = LinkedHashSet<String>()
        val publishCodecs = LinkedHashSet<String>()
        val decode = ArrayList<Long>()
        var streamInfo: JsonObject? = null
        var streamSamples = 0
        val notes = ArrayList<String>()
        var records = 0
        var skipped = 0
        var first: Long? = null
        var last: Long? = null

        for (line in lines) {
            if (line.isBlank()) continue
            val record = try {
                Json.parse(line) as? JsonObject
            } catch (_: JsonParseException) {
                null
            }
            val kind = (record?.get("kind") as? JsonString)?.value?.let(RecordKind::fromWire)
            val t = (record?.get("t_ms") as? JsonInt)?.value
            if (record == null || kind == null || t == null) {
                skipped++
                continue
            }
            records++
            first = first?.let { minOf(it, t) } ?: t
            last = last?.let { maxOf(it, t) } ?: t
            when (kind) {
                RecordKind.COMMAND_SENT -> record.string("command_id")?.let { sent[it] = t }
                RecordKind.COMMAND_ACKED -> {
                    acked++
                    val rtt = (record["rtt_ms"] as? JsonInt)?.value
                        ?: record.string("command_id")?.let { sent[it] }?.let { t - it }
                    if (rtt != null) rtts.add(rtt)
                }
                RecordKind.COMMAND_DROPPED -> dropped++
                RecordKind.STICK_SENT -> sticks.add(t)
                RecordKind.TELEMETRY -> telemetry.add(t)
                RecordKind.VIDEO_PUBLISH -> {
                    publishWindows++
                    if (record.string("ice_state") == "connected") publishConnected++
                    record.doubleOf("bitrate_kbps")?.let(publishBitrates::add)
                    record.doubleOf("fps")?.let(publishFps::add)
                    (record["dropped_frames"] as? JsonInt)?.value?.let { publishDropped = maxOf(publishDropped, it) }
                    record.doubleOf("rtt_ms")?.let { publishRtts.add(Math.round(it)) }
                    record.doubleOf("processing_ms")?.let(publishProcessing::add)
                    record.string("source")?.let(publishSources::add)
                    record.string("codec")?.let(publishCodecs::add)
                }
                RecordKind.VIDEO_FRAME -> {
                    if ((record["dropped"] as? JsonBool)?.value == true) {
                        droppedFrames++
                    } else {
                        frames.add(t)
                        if ((record["keyframe"] as? JsonBool)?.value == true) keyframes++
                        bytes += (record["size_bytes"] as? JsonInt)?.value ?: 0
                        (record["decode_ms"] as? JsonInt)?.value?.let(decode::add)
                    }
                }
                RecordKind.STREAM_INFO -> {
                    streamInfo = record
                    streamSamples++
                }
                RecordKind.NOTE -> record.string("text")?.let(notes::add)
                RecordKind.PROBE -> notes.add("probe ${record.string("name") ?: "?"}: ${record.string("summary") ?: ""}")
            }
        }
        val jitter = if (rtts.size >= 2) rtts.zipWithNext { a, b -> abs(b - a).toDouble() }.average() else null
        return BenchReport(
            commands = CommandStats(
                sent = sent.size,
                acked = acked,
                dropped = dropped,
                unacknowledged = (sent.size - acked - dropped).coerceAtLeast(0),
                rtt = LatencyStats.of(rtts),
                jitterMs = jitter,
            ),
            sticks = RateStats.of(sticks),
            telemetry = RateStats.of(telemetry),
            publish = PublishStats(
                windows = publishWindows,
                connectedWindows = publishConnected,
                meanBitrateKbps = publishBitrates.takeIf { it.isNotEmpty() }?.average(),
                meanFps = publishFps.takeIf { it.isNotEmpty() }?.average(),
                droppedFrames = publishDropped,
                rtt = LatencyStats.of(publishRtts),
                meanProcessingMs = publishProcessing.takeIf { it.isNotEmpty() }?.average(),
                sources = publishSources.toList(),
                codecs = publishCodecs.toList(),
            ),
            video = VideoStats(
                frames = frames.size,
                keyframes = keyframes,
                dropped = droppedFrames,
                bytes = bytes,
                rate = RateStats.of(frames),
                decode = LatencyStats.of(decode),
                stream = streamInfo?.let { streamSummary(it, streamSamples) },
            ),
            notes = notes,
            records = records,
            skippedLines = skipped,
            firstTMs = first,
            lastTMs = last,
        )
    }

    private fun streamSummary(record: JsonObject, samples: Int): StreamSummary = StreamSummary(
        mimeType = record.string("mime_type"),
        codec = record.string("codec"),
        width = record.int("width"),
        height = record.int("height"),
        nominalFrameRateHz = record.int("nominal_frame_rate_hz"),
        measuredFrameRateHz = record.number("measured_frame_rate_hz"),
        keyframeIntervalMs = record.int("keyframe_interval_ms"),
        keyframeIntervalFrames = record.int("keyframe_interval_frames"),
        profile = record.string("profile"),
        level = record.string("level"),
        tier = record.string("tier"),
        spsError = record.string("sps_error"),
        phoneThermalState = record.string("phone_thermal_state"),
        samples = samples,
    )

    private fun JsonObject.string(key: String): String? = (this[key] as? JsonString)?.value

    private fun JsonObject.int(key: String): Long? = (this[key] as? JsonInt)?.value

    private fun JsonObject.number(key: String): Double? = when (val value = this[key]) {
        is JsonFloat -> value.value
        is JsonInt -> value.value.toDouble()
        else -> null
    }
}

/** Renders a [BenchReport] as canonical JSON or as the plain text that goes into the M1.9 evidence. */
object ReportWriter {
    fun json(report: BenchReport): String = Json.canonical(report.toJson())

    fun text(report: BenchReport): String = buildString {
        appendLine("Sweep bridge bench report")
        appendLine("records: ${report.records} (skipped lines: ${report.skippedLines})")
        appendLine("span_ms: ${span(report)}")
        appendLine()
        appendLine("commands")
        appendLine("  sent: ${report.commands.sent}")
        appendLine("  acked: ${report.commands.acked}")
        appendLine("  dropped: ${report.commands.dropped}")
        appendLine("  unacknowledged: ${report.commands.unacknowledged}")
        appendLine("  rtt: ${latency(report.commands.rtt)}")
        appendLine("  jitter_ms: ${report.commands.jitterMs?.let { format(it) } ?: "-"}")
        appendLine()
        appendLine("virtual stick")
        appendLine("  sent: ${report.sticks.count}")
        appendLine("  rate_hz: ${rate(report.sticks)}")
        appendLine()
        appendLine("telemetry")
        appendLine("  frames: ${report.telemetry.count}")
        appendLine("  rate_hz: ${rate(report.telemetry)}")
        appendLine()
        appendLine("video publish")
        appendLine("  windows: ${report.publish.windows} (ice connected: ${report.publish.connectedWindows})")
        appendLine("  mean_bitrate_kbps: ${report.publish.meanBitrateKbps?.let { format(it) } ?: "-"}")
        appendLine("  mean_fps: ${report.publish.meanFps?.let { format(it) } ?: "-"}")
        appendLine("  dropped_frames: ${report.publish.droppedFrames}")
        appendLine("  rtt_ms: ${latency(report.publish.rtt)}")
        appendLine("  mean_processing_ms: ${report.publish.meanProcessingMs?.let { format(it) } ?: "-"}")
        appendLine("  sources: ${report.publish.sources.ifEmpty { listOf("-") }.joinToString()}")
        appendLine("  codecs: ${report.publish.codecs.ifEmpty { listOf("-") }.joinToString()}")
        appendLine()
        appendLine("video")
        appendLine("  frames: ${report.video.frames}")
        appendLine("  keyframes: ${report.video.keyframes}")
        appendLine("  dropped: ${report.video.dropped}")
        appendLine("  bytes: ${report.video.bytes}")
        appendLine("  rate_hz: ${rate(report.video.rate)}")
        appendLine("  decode: ${latency(report.video.decode)}")
        report.video.stream?.let { stream ->
            appendLine("  stream_mime: ${stream.mimeType ?: "-"}" + (stream.codec?.let { " ($it)" } ?: ""))
            appendLine("  stream_size: ${stream.width ?: "-"}x${stream.height ?: "-"}")
            appendLine("  stream_nominal_hz: ${stream.nominalFrameRateHz ?: "-"}")
            appendLine("  stream_measured_hz: ${stream.measuredFrameRateHz?.let { format(it) } ?: "-"}")
            appendLine(
                "  stream_keyframe_interval: ${stream.keyframeIntervalMs?.let { "$it ms" } ?: "-"} / " +
                    "${stream.keyframeIntervalFrames?.let { "$it frames" } ?: "-"}",
            )
            appendLine("  stream_profile: ${stream.profile ?: "-"} level ${stream.level ?: "-"}" + (stream.tier?.let { " tier $it" } ?: ""))
            stream.spsError?.let { appendLine("  stream_sps_error: $it") }
            appendLine("  stream_phone_thermal: ${stream.phoneThermalState ?: "-"}")
            appendLine("  stream_samples: ${stream.samples}")
        }
        if (report.notes.isNotEmpty()) {
            appendLine()
            appendLine("notes")
            for (note in report.notes) appendLine("  $note")
        }
    }

    private fun span(report: BenchReport): String {
        val first = report.firstTMs ?: return "-"
        val last = report.lastTMs ?: return "-"
        return (last - first).toString()
    }

    private fun latency(stats: LatencyStats?): String = stats?.let {
        "n=${it.count} min=${it.minMs} mean=${format(it.meanMs)} p50=${it.p50Ms} p95=${it.p95Ms} max=${it.maxMs}"
    } ?: "-"

    private fun rate(stats: RateStats): String = stats.rateHz?.let { format(it) } ?: "-"

    private fun format(value: Double): String = String.format(java.util.Locale.ROOT, "%.2f", value)
}
