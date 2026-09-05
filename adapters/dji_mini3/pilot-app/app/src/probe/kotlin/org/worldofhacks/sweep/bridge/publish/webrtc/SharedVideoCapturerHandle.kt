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

import android.content.Context
import org.webrtc.CapturerObserver
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer

/**
 * Lightweight [VideoCapturer] proxy that delegates to a [SharedDJIFrameSource]
 * so the expensive DJI frame listener and NV21 scaling run only once.
 *
 * Sweep changes: the per-frame telemetry metadata listener is not carried over.
 */
class SharedVideoCapturerHandle(
    private val clientId: String,
    private val source: SharedDJIFrameSource,
) : VideoCapturer {

    override fun initialize(
        surfaceTextureHelper: SurfaceTextureHelper?,
        applicationContext: Context,
        capturerObserver: CapturerObserver,
    ) {
        source.registerObserver(clientId, capturerObserver)
    }

    override fun startCapture(width: Int, height: Int, framerate: Int) {
        source.startClient(clientId, width, height, framerate)
    }

    override fun stopCapture() {
        source.stopClient(clientId)
    }

    fun changeResolution(width: Int, height: Int) {
        source.changeResolution(width, height)
    }

    fun changeFrameRate(fps: Int) {
        source.changeFrameRate(fps)
    }

    fun totalOutputFrames(): Long = source.totalOutputFrames()

    fun waitForOutputFrameAfter(frameCount: Long, timeoutMs: Long): Boolean {
        return source.waitForOutputFrameAfter(frameCount, timeoutMs)
    }

    fun recoverCapture(reason: String) {
        source.recoverCapture(reason)
    }

    override fun changeCaptureFormat(width: Int, height: Int, framerate: Int) {
        source.changeResolution(width, height)
        source.changeFrameRate(framerate)
    }

    override fun dispose() {
        source.stopClient(clientId)
    }

    override fun isScreencast(): Boolean = false
}
