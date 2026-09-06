/*
 * MIT License
 *
 * Copyright (c) 2025 WildDrone
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
// Vendored from WildDrone/WildBridge (MIT)
package org.worldofhacks.sweep.bridge.publish.sdp

/**
 * SDP manipulation utilities for forcing H264 codec and tuning keyframe interval.
 *
 * The DefaultVideoEncoderFactory always registers a software VP8 encoder even when
 * the Intel VP8 hardware encoder is disabled. During SDP negotiation the remote peer
 * (e.g. MediaMTX) may prefer VP8, causing the stream to be encoded as VP8 instead of
 * H264. These helpers strip non-H264 video codecs from the SDP so only H264 can be
 * negotiated.
 *
 * Sweep changes: `SdpUtils` became `SdpMunger`, Android logging became the injectable
 * [log] sink so the pure-JVM tests can run it, and [videoCodecs] / [negotiatedVideoCodec]
 * were added to report what an offer carries and what an answer selected.
 */
class SdpMunger(private val log: (String) -> Unit = {}) {
    /**
     * Remove all video codecs except H264 (and their associated RTX/RED/ULPFEC)
     * from the SDP, forcing the peer connection to negotiate H264.
     *
     * If no H264 codec is found the original SDP is returned unchanged.
     */
    fun forceH264Only(sdp: String): String {
        val lineEnding = if ("\r\n" in sdp) "\r\n" else "\n"
        val lines = sdp.split(lineEnding)

        // --- Phase 1: locate the video m-line and collect codec metadata ---
        var videoLineIdx = -1
        var videoEndIdx = lines.size
        val allVideoPts = mutableListOf<Int>()
        val codecForPt = mutableMapOf<Int, String>()
        val rtxApt = mutableMapOf<Int, Int>() // rtx_pt -> associated_pt

        for ((i, line) in lines.withIndex()) {
            if (line.startsWith("m=video")) {
                videoLineIdx = i
                line.split(" ").drop(3).forEach { tok ->
                    tok.toIntOrNull()?.let { allVideoPts.add(it) }
                }
            } else if (videoLineIdx >= 0 && i > videoLineIdx && line.startsWith("m=")) {
                videoEndIdx = i
                break
            }

            if (videoLineIdx >= 0 && i > videoLineIdx) {
                RTPMAP.find(line)?.let { m ->
                    codecForPt[m.groupValues[1].toInt()] = m.groupValues[2]
                }
                FMTP_APT.find(line)?.let { m ->
                    rtxApt[m.groupValues[1].toInt()] = m.groupValues[2].toInt()
                }
            }
        }

        if (videoLineIdx < 0) return sdp

        val h264Pts = codecForPt.filter { it.value.equals("H264", ignoreCase = true) }.keys
        if (h264Pts.isEmpty()) {
            log("No H264 codec found in SDP; leaving unchanged (video codecs: ${codecForPt.values.distinct()})")
            return sdp
        }

        val h264Rtx = rtxApt.filter { it.value in h264Pts }.keys
        val redPts = codecForPt.filter { it.value.equals("red", ignoreCase = true) }.keys
        val ulpfecPts = codecForPt.filter { it.value.equals("ulpfec", ignoreCase = true) }.keys
        val allowed = h264Pts + h264Rtx + redPts + ulpfecPts

        // --- Phase 2: rebuild the SDP keeping only allowed payload types ---
        val result = mutableListOf<String>()

        for ((i, line) in lines.withIndex()) {
            if (i == videoLineIdx) {
                // Rewrite m=video line with only the allowed PTs
                val parts = line.split(" ")
                val prefix = parts.take(3).joinToString(" ")
                val kept = allVideoPts.filter { it in allowed }
                result.add("$prefix ${kept.joinToString(" ")}")
                continue
            }

            if (i in (videoLineIdx + 1) until videoEndIdx) {
                val ptMatch = PT_LINE.find(line)
                if (ptMatch != null && ptMatch.groupValues[1].toInt() !in allowed) {
                    continue // drop lines for disallowed codecs
                }
            }

            result.add(line)
        }

        log("Forced H264-only: kept PTs ${allowed.joinToString(",")}")
        return result.joinToString(lineEnding)
    }

    /**
     * Inject `x-google-max-keyframe-interval` into every H264 fmtp line.
     * This tells the libwebrtc encoder to insert a keyframe at most every
     * [intervalMs] milliseconds, improving recovery from packet loss.
     *
     * @param intervalMs max interval between keyframes in milliseconds (e.g. 2000)
     */
    fun setKeyframeInterval(sdp: String, intervalMs: Int): String {
        if (intervalMs <= 0) return sdp

        val lineEnding = if ("\r\n" in sdp) "\r\n" else "\n"
        val lines = sdp.split(lineEnding)

        // Collect H264 payload types
        val h264Pts = mutableSetOf<Int>()
        for (line in lines) {
            RTPMAP_H264.find(line)?.let {
                h264Pts.add(it.groupValues[1].toInt())
            }
        }
        if (h264Pts.isEmpty()) return sdp

        val result = lines.map { line ->
            val fmtpMatch = FMTP.find(line)
            if (fmtpMatch != null && fmtpMatch.groupValues[1].toInt() in h264Pts &&
                "x-google-max-keyframe-interval" !in line
            ) {
                "$line;x-google-max-keyframe-interval=$intervalMs"
            } else {
                line
            }
        }

        return result.joinToString(lineEnding)
    }

    /**
     * Apply both H264 enforcement and keyframe interval to an SDP string.
     */
    fun mungeForH264(sdp: String, keyframeIntervalMs: Int = DEFAULT_KEYFRAME_INTERVAL_MS): String =
        setKeyframeInterval(forceH264Only(sdp), keyframeIntervalMs)

    companion object {
        const val DEFAULT_KEYFRAME_INTERVAL_MS = 2000
        private val RTPMAP = Regex("""^a=rtpmap:(\d+)\s+(\S+)/""")
        private val RTPMAP_H264 = Regex("""^a=rtpmap:(\d+)\s+H264/""")
        private val FMTP = Regex("""^a=fmtp:(\d+)\s+""")
        private val FMTP_APT = Regex("""^a=fmtp:(\d+)\s+.*\bapt=(\d+)""")
        private val PT_LINE = Regex("""^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)\b""")

        /** Video codec names in the order the `m=video` line lists their payload types. */
        fun videoCodecs(sdp: String): List<String> {
            val lines = sdp.split("\r\n", "\n")
            val videoLine = lines.indexOfFirst { it.startsWith("m=video") }
            if (videoLine < 0) return emptyList()
            val payloadTypes = lines[videoLine].split(" ").drop(3).mapNotNull { it.toIntOrNull() }
            val codecForPt = HashMap<Int, String>()
            for (line in lines.drop(videoLine + 1)) {
                if (line.startsWith("m=")) break
                RTPMAP.find(line)?.let { codecForPt[it.groupValues[1].toInt()] = it.groupValues[2] }
            }
            return payloadTypes.mapNotNull { codecForPt[it] }
        }

        /** The codec an answer selected: the first real video codec (not rtx/red/ulpfec), or null. */
        fun negotiatedVideoCodec(answerSdp: String): String? =
            videoCodecs(answerSdp).firstOrNull { it.lowercase() !in AUXILIARY }

        private val AUXILIARY = setOf("rtx", "red", "ulpfec", "flexfec-03")
    }
}
