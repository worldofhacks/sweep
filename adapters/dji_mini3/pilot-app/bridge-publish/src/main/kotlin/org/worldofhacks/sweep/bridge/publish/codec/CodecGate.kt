package org.worldofhacks.sweep.bridge.publish.codec

import org.worldofhacks.sweep.bridge.core.video.SpsInfo
import org.worldofhacks.sweep.bridge.core.video.SpsParseException
import org.worldofhacks.sweep.bridge.core.video.SpsParser
import org.worldofhacks.sweep.bridge.core.video.VideoCodec
import org.worldofhacks.sweep.bridge.publish.PublishReasons

/** What the SDK's encoded stream turned out to be; the Phase D codec evidence in one record. */
data class CodecEvidence(
    val mime: String,
    val codec: VideoCodec?,
    val sps: SpsInfo?,
    val width: Int,
    val height: Int,
    val frameRate: Int,
    val bSlices: Boolean,
    val keyframeIntervalMs: Long?,
) {
    /** Short label for the screen and the bench log, for example `H264 High 4.0 1280x720 30fps`. */
    fun label(): String = buildString {
        append(codec?.name ?: mime)
        sps?.let { append(' ').append(it.profileName).append(' ').append(it.level) }
        if (width > 0 && height > 0) append(' ').append(width).append('x').append(height)
        if (frameRate > 0) append(' ').append(frameRate).append("fps")
        if (bSlices) append(" B-slices")
        keyframeIntervalMs?.let { append(" keyframe/").append(it).append("ms") }
    }
}

data class CodecDecision(val supported: Boolean, val reason: String?, val detail: String)

/**
 * Decides whether the aircraft's encoded stream can go to the browser unchanged. MediaMTX's
 * WebRTC output hands browsers H.264 baseline (incl. constrained), main, or high without
 * B-frames; anything else (H.265, High 10, 4:2:2, B slices) is reported as
 * `codec_unsupported` with the profile spelled out, never transcoded silently.
 */
object CodecGate {
    /** `profile_idc` values browsers decode over WebRTC: baseline 66, main 77, high 100. */
    val BROWSER_H264_PROFILES: Set<Int> = setOf(66, 77, 100)

    fun evaluate(evidence: CodecEvidence): CodecDecision {
        val label = evidence.label()
        return when (evidence.codec) {
            null -> CodecDecision(false, PublishReasons.CODEC_UNSUPPORTED, "unknown stream mime type ${evidence.mime}; $label")
            VideoCodec.H265 -> CodecDecision(
                false,
                PublishReasons.CODEC_UNSUPPORTED,
                "aircraft emits H.265 ($label); the console's browser WebRTC path takes H.264 only",
            )
            VideoCodec.H264 -> {
                val sps = evidence.sps
                when {
                    sps == null -> CodecDecision(false, PublishReasons.CODEC_UNSUPPORTED, "H.264 stream without a readable SPS; $label")
                    sps.profileIdc !in BROWSER_H264_PROFILES -> CodecDecision(
                        false,
                        PublishReasons.CODEC_UNSUPPORTED,
                        "H.264 ${sps.profileName} (profile_idc ${sps.profileIdc}) is not browser-decodable over WebRTC; $label",
                    )
                    evidence.bSlices -> CodecDecision(
                        false,
                        PublishReasons.CODEC_UNSUPPORTED,
                        "H.264 ${sps.profileName} with B slices; browsers' WebRTC decoders do not reorder frames; $label",
                    )
                    else -> CodecDecision(true, null, label)
                }
            }
        }
    }

    /** Builds the evidence for an Annex B keyframe; a missing or malformed SPS leaves [CodecEvidence.sps] null. */
    fun evidence(
        mime: String,
        annexB: ByteArray,
        width: Int,
        height: Int,
        frameRate: Int,
        bSlices: Boolean,
        keyframeIntervalMs: Long?,
    ): CodecEvidence {
        val codec = codecOf(mime)
        val sps = try {
            SpsParser.parse(annexB, codec)
        } catch (_: SpsParseException) {
            null
        }
        return CodecEvidence(mime, codec, sps, width, height, frameRate, bSlices, keyframeIntervalMs)
    }

    /** Accepts the SDK's `H264` / `H265` names as well as MIME types. */
    fun codecOf(mime: String): VideoCodec? = when (mime.trim().uppercase()) {
        "H264", "AVC", "VIDEO/AVC" -> VideoCodec.H264
        "H265", "HEVC", "VIDEO/HEVC" -> VideoCodec.H265
        else -> VideoCodec.fromMime(mime)
    }
}
