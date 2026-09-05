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
package org.worldofhacks.sweep.bridge.publish.metrics

/**
 * Sender-side health of the frame source, emitted once a second by the shared DJI frame source
 * and the test-pattern capturer. Sweep changes: moved to the pure-JVM module unchanged apart
 * from the package; the transport side (bitrate, RTT, ICE) is [PublishMetrics].
 */
data class WebRTCStreamMetrics(
    val sourceWidth: Int = 0,
    val sourceHeight: Int = 0,
    val outputWidth: Int = 0,
    val outputHeight: Int = 0,
    val requestedWidth: Int = 0,
    val requestedHeight: Int = 0,
    val targetFps: Int = 0,
    val inputFps: Double = 0.0,
    val outputFps: Double = 0.0,
    val droppedFps: Double = 0.0,
    val averageFrameProcessingMs: Double = 0.0,
    val totalFrames: Long = 0,
    val totalDroppedFrames: Long = 0,
    val processingErrors: Long = 0,
    val observerCount: Int = 0,
    val activeCamera: String = "unknown",
    val status: String = "idle",
    val configuredFps: Int = 0,
    val saturationState: String = "ok",
    val scaleMode: String = "fixed",
    val recoveryCount: Int = 0,
    val lastError: String? = null,
) {
    val resolutionLabel: String
        get() = if (outputWidth > 0 && outputHeight > 0) {
            "${outputWidth}x$outputHeight"
        } else {
            "waiting"
        }

    fun compactLabel(): String {
        val source = if (sourceWidth > 0 && sourceHeight > 0) "${sourceWidth}x$sourceHeight" else "waiting"
        val requested = if (requestedWidth > 0 && requestedHeight > 0) "${requestedWidth}x$requestedHeight" else "native"
        val saturationLabel = if (saturationState != "ok") " sat $saturationState" else ""
        val fpsLabel = if (configuredFps > 0 && configuredFps != targetFps) {
            "${outputFps.format1()}/$targetFps cfg $configuredFps"
        } else {
            "${outputFps.format1()}/$targetFps"
        }
        val errorLabel = if (processingErrors > 0) " err $processingErrors" else ""
        val recoveryLabel = if (recoveryCount > 0) " fix $recoveryCount" else ""
        return "WEBRTC $status$saturationLabel out $resolutionLabel req $requested src $source fps $fpsLabel drop ${droppedFps.format1()} resize ${averageFrameProcessingMs.format1()}ms scale $scaleMode clients $observerCount$errorLabel$recoveryLabel"
    }

    private fun Double.format1(): String = String.format(java.util.Locale.US, "%.1f", this)
}
