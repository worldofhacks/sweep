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
import android.util.Log
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.PeerConnectionFactory
import org.webrtc.VideoEncoderFactory

/**
 * Sweep changes: the global `PeerConnectionFactory.initialize` runs once with the field
 * trials the publish path needs (`WebRTC-H264HighProfile` as upstream, plus
 * `WebRTC-FrameDropper/Disabled` so the passthrough path's pre-encoded access units are never
 * dropped for bitrate overshoot); [create] builds one factory per publish session with the
 * source's encoder factory (the passthrough factory, or the platform default) and
 * [supportedEncoders] reports what the phone can encode for the log.
 */
object WebRTCPeerFactory {
    private const val TAG = "WebRTCPeerFactory"
    private const val FIELD_TRIALS = "WebRTC-H264HighProfile/Enabled/WebRTC-FrameDropper/Disabled/"

    private val factoryLock = Any()
    private var initialized = false
    private var eglBase: EglBase? = null

    fun getEglBase(): EglBase {
        synchronized(factoryLock) {
            if (eglBase == null) {
                eglBase = EglBase.create()
            }
            return eglBase!!
        }
    }

    fun initialize(context: Context) {
        synchronized(factoryLock) {
            if (initialized) return
            val initOptions = PeerConnectionFactory.InitializationOptions.builder(context.applicationContext)
                .setEnableInternalTracer(false)
                .setFieldTrials(FIELD_TRIALS)
                .createInitializationOptions()
            PeerConnectionFactory.initialize(initOptions)
            initialized = true
            Log.d(TAG, "PeerConnectionFactory initialized with field trials $FIELD_TRIALS")
        }
    }

    /** The platform encoder factory: hardware H.264 where libwebrtc allows it, software VP8/VP9 otherwise. */
    fun defaultEncoderFactory(): VideoEncoderFactory =
        DefaultVideoEncoderFactory(getEglBase().eglBaseContext, false, true)

    fun supportedEncoders(factory: VideoEncoderFactory): List<String> =
        factory.supportedCodecs.map { info -> info.name + info.params[PROFILE_LEVEL_ID]?.let { " ($it)" }.orEmpty() }

    fun create(context: Context, encoderFactory: VideoEncoderFactory?): PeerConnectionFactory {
        initialize(context)
        val rootEglBase = getEglBase()
        return PeerConnectionFactory.builder()
            .setVideoDecoderFactory(DefaultVideoDecoderFactory(rootEglBase.eglBaseContext))
            .setVideoEncoderFactory(encoderFactory ?: defaultEncoderFactory())
            .setOptions(PeerConnectionFactory.Options())
            .createPeerConnectionFactory()
    }

    private const val PROFILE_LEVEL_ID = "profile-level-id"
}
