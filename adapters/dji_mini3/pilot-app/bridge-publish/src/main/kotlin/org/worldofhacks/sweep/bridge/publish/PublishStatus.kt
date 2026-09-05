package org.worldofhacks.sweep.bridge.publish

import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState

/**
 * Where the published frames come from. The wire word goes into the bench log and the screen;
 * [requiresAircraft] decides whether the automatic start waits for a connected aircraft.
 */
enum class PublishSource(val wire: String, val label: String, val requiresAircraft: Boolean) {
    /** The SDK's encoded H.264 access units handed to WebRTC's packetizer unchanged. */
    PASSTHROUGH("passthrough", "SDK encoded frames, no re-encode", true),

    /** The SDK's decoded NV21 frames re-encoded by the phone (WildBridge's path); never chosen silently. */
    REENCODE("reencode", "Re-encode on the phone (adds latency)", true),

    /** The fake flavor's generated test pattern with a moving timestamp. */
    TEST_PATTERN("test_pattern", "Generated test pattern", false),
    ;

    companion object {
        fun fromWire(value: String): PublishSource? = entries.firstOrNull { it.wire == value }
    }
}

/** Machine-readable failure reasons in the acknowledgement style (snake_case). */
object PublishReasons {
    const val CODEC_UNSUPPORTED = "codec_unsupported"
    const val SOURCE_UNAVAILABLE = "source_unavailable"
    const val NO_FRAMES = "no_frames"
    const val SDP_FAILED = "sdp_failed"
    const val HTTP_ERROR = "http_error"
    const val NETWORK_ERROR = "network_error"
    const val ICE_FAILED = "ice_failed"
    const val ICE_DISCONNECTED = "ice_disconnected"
    const val AIRCRAFT_DISCONNECTED = "aircraft_disconnected"
    const val MEDIA_AUTH_UNAVAILABLE = "media_auth_unavailable"
    const val INTERNAL_ERROR = "internal_error"

    /** Reasons a retry cannot clear: the operator must change the source, the aircraft, or the setup. */
    val TERMINAL: Set<String> = setOf(CODEC_UNSUPPORTED, SOURCE_UNAVAILABLE, MEDIA_AUTH_UNAVAILABLE)

    fun isTerminal(reason: String): Boolean = reason in TERMINAL
}

/** Everything the screen, the bench log, and `node_status` read about the publisher. */
data class PublishStatus(
    val state: VideoPublishState = VideoPublishState.STOPPED,
    /** Machine-readable reason while [state] is `failed`; null otherwise. */
    val reason: String? = null,
    val detail: String? = null,
    val source: PublishSource? = null,
    val whipUrl: String? = null,
    /** The WHIP resource (`Location`) that the DELETE on stop targets. */
    val resourceUrl: String? = null,
    val attempts: Int = 0,
    val consecutiveFailures: Int = 0,
    val nextAttemptAtMs: Long? = null,
    val publishingSinceMs: Long? = null,
    /** Codec evidence label, for example `H264 High 4.0 passthrough` or `VP8 (no H.264 encoder)`. */
    val codec: String? = null,
    val lastChangeAtMs: Long = 0,
) {
    val retryPending: Boolean
        get() = state == VideoPublishState.FAILED && nextAttemptAtMs != null
}
