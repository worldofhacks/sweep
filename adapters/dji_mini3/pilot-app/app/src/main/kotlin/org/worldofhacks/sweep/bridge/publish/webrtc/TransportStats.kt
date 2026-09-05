package org.worldofhacks.sweep.bridge.publish.webrtc

import org.webrtc.RTCStats
import org.webrtc.RTCStatsReport
import org.worldofhacks.sweep.bridge.publish.metrics.TransportSample

/** Reads the sender's `outbound-rtp`, `transport`, and `candidate-pair` stats into one [TransportSample]. */
object TransportStats {
    fun read(report: RTCStatsReport, nowMs: Long, iceStateHint: String): TransportSample {
        val stats = report.statsMap.values
        val outbound = stats.firstOrNull { it.type == "outbound-rtp" && it.string("kind") == "video" }
            ?: stats.firstOrNull { it.type == "outbound-rtp" }
        val transport = stats.firstOrNull { it.type == "transport" }
        val pairId = transport?.string("selectedCandidatePairId")
        val pair = stats.firstOrNull { it.type == "candidate-pair" && it.id == pairId }
            ?: stats.firstOrNull { it.type == "candidate-pair" && it.bool("nominated") == true && it.string("state") == "succeeded" }
        val codecId = outbound?.string("codecId")
        val codec = stats.firstOrNull { it.type == "codec" && it.id == codecId }?.string("mimeType")?.substringAfter('/')
        return TransportSample(
            atMs = nowMs,
            bytesSent = outbound?.long("bytesSent") ?: 0,
            framesSent = outbound?.long("framesSent") ?: 0,
            framesEncoded = outbound?.long("framesEncoded") ?: 0,
            keyFramesEncoded = outbound?.long("keyFramesEncoded") ?: 0,
            rttMs = pair?.double("currentRoundTripTime")?.let { it * 1000.0 },
            iceState = transport?.string("iceState") ?: iceStateHint,
            codec = codec,
            qualityLimitation = outbound?.string("qualityLimitationReason"),
        )
    }

    private fun RTCStats.string(key: String): String? = members[key]?.toString()

    private fun RTCStats.long(key: String): Long? = (members[key] as? Number)?.toLong()

    private fun RTCStats.double(key: String): Double? = (members[key] as? Number)?.toDouble()

    private fun RTCStats.bool(key: String): Boolean? = members[key] as? Boolean
}
