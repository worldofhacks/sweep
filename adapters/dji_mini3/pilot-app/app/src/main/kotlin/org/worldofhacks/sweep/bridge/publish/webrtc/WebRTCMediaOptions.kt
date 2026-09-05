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
package org.worldofhacks.sweep.bridge.publish.webrtc

/**
 * Configuration options for WebRTC media streaming.
 *
 * Sweep changes: the default is [mini3LiveView], 1280x720 at 30 fps (the Mini 3's O2 live
 * view), and the fleet presets of the original are kept for reference; [senderBitrateBps]
 * caps at the preset's bitrate rather than WildBridge's six-stream Wi-Fi ceilings.
 */
data class WebRTCMediaOptions(
    val mediaStreamId: String = "SWEEP_DRONE_STREAM",
    val videoTrackId: String = "SWEEP_VIDEO_TRACK",
    val videoResolutionWidth: Int = 1280,
    val videoResolutionHeight: Int = 720,
    val fps: Int = 30,
    val videoBitrate: Int = 4_000_000,
    val videoCodec: String = "H264",
) {
    val usesSourceResolution: Boolean
        get() = videoResolutionWidth <= 0 || videoResolutionHeight <= 0

    /** The sender bitrate asked of the encoder and the pacer. */
    fun senderBitrateBps(): Int = videoBitrate

    companion object {
        /** 1280x720 at 30 fps: the Mini 3 live view over O2; 4 Mbps re-encode target. */
        fun mini3LiveView() = WebRTCMediaOptions()

        /** Preserve the source frame size and skip app-side scaling. */
        fun native() = WebRTCMediaOptions(
            videoResolutionWidth = 0,
            videoResolutionHeight = 0,
            fps = 30,
            videoBitrate = 4_000_000,
            videoCodec = "H264",
        )

        /** 1920x1080 @ 8 Mbps — best quality for detection */
        fun fullHD() = WebRTCMediaOptions(
            videoResolutionWidth = 1920,
            videoResolutionHeight = 1080,
            fps = 30,
            videoBitrate = 8_000_000,
            videoCodec = "H264",
        )

        /** 1280x720 @ 2 Mbps — lighter on bandwidth for fleet testing */
        fun hd() = WebRTCMediaOptions(
            videoResolutionWidth = 1280,
            videoResolutionHeight = 720,
            fps = 10,
            videoBitrate = 2_000_000,
            videoCodec = "H264",
        )

        /** 640x480 @ 1.5 Mbps — low bandwidth fallback */
        fun sd() = WebRTCMediaOptions(
            videoResolutionWidth = 640,
            videoResolutionHeight = 480,
            fps = 10,
            videoBitrate = 1_500_000,
            videoCodec = "H264",
        )
    }
}
