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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import org.webrtc.CapturerObserver
import org.webrtc.DataChannel
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.RtpParameters
import org.webrtc.RtpReceiver
import org.webrtc.RtpSender
import org.webrtc.SessionDescription
import org.webrtc.VideoCapturer
import org.webrtc.VideoEncoderFactory
import org.webrtc.VideoFrame
import org.webrtc.VideoSource
import org.webrtc.VideoTrack
import org.worldofhacks.sweep.bridge.publish.PublishReasons
import org.worldofhacks.sweep.bridge.publish.metrics.TransportSample
import org.worldofhacks.sweep.bridge.publish.sdp.SdpMunger
import org.worldofhacks.sweep.bridge.publish.whip.WhipClient
import org.worldofhacks.sweep.bridge.publish.whip.WhipException

/**
 * Publishes a WebRTC video stream to a mediamtx server via WHIP
 * (WebRTC HTTP Ingest Protocol).
 *
 * Flow:
 *  1. Create PeerConnection with a sendonly video track
 *  2. Create SDP offer and gather all ICE candidates
 *  3. POST the offer to the WHIP endpoint
 *  4. Set the SDP answer from the response
 *  5. Video flows through mediamtx to all WHEP consumers
 *
 * Reconnects automatically if the connection drops.
 *
 * Sweep changes against upstream:
 *  - the HTTP POST and DELETE moved to [WhipClient] (OkHttp, bound to the Wi-Fi network and
 *    tested against MockWebServer); failures are typed with `PublishReasons`;
 *  - the reconnect delay and the decision to retry come from [Listener.onFailure] (the
 *    publish state machine's bounded backoff) instead of a fixed schedule;
 *  - no STUN server: the ground station is on the LAN, host candidates suffice, and an
 *    unreachable STUN server on an internet-less AP would stall every ICE gathering;
 *  - the first-frame wait wraps the [CapturerObserver] so any capturer works, and the
 *    source's encoder factory (passthrough) builds the peer connection factory;
 *  - ICE FAILED/DISCONNECTED and a connect timeout end the attempt with a reason;
 *  - sender bitrate floors, `DegradationPreference.DISABLED` for passthrough, and
 *    [PeerConnection.setBitrate] keep the pacer from throttling pre-encoded frames;
 *  - the resolution and frame-rate switching of the original is not carried over.
 */
class WhipPublisher(
    context: Context,
    private val videoCapturer: VideoCapturer,
    private val encoderFactory: VideoEncoderFactory?,
    private val options: WebRTCMediaOptions,
    private val whipUrl: String,
    private val whip: WhipClient,
    private val targetBitrateBps: Int,
    private val maxBitrateBps: Int,
    private val passthrough: Boolean,
    private val log: (String) -> Unit,
) {
    companion object {
        private const val TAG = "WhipPublisher"
        private const val ICE_GATHER_TIMEOUT_S = 5L
        private const val FIRST_FRAME_TIMEOUT_MS = 15_000L
        private const val CONNECT_TIMEOUT_MS = 10_000L
        private const val DISCONNECT_GRACE_MS = 3_000L
        private const val POLL_MS = 200L
    }

    /** Session events; called on the publisher's own thread. */
    interface Listener {
        fun onAttempt(attempt: Int)

        fun onOfferAccepted(resourceUrl: String?, negotiatedCodec: String?)

        fun onPublishing()

        fun onIceState(state: String)

        /** Returns the delay before the next attempt, or null to stop retrying. */
        fun onFailure(reason: String, detail: String): Long?

        fun onStopped()
    }

    private class PublishFailure(val reason: String, val detail: String) : RuntimeException(detail)

    private val appContext = context.applicationContext
    private val executor = Executors.newSingleThreadExecutor { runnable -> Thread(runnable, "whip-publisher").apply { isDaemon = true } }
    private val munger = SdpMunger(log)

    private var factory: PeerConnectionFactory? = null
    private var peerConnection: PeerConnection? = null
    private var videoSource: VideoSource? = null
    private var videoTrack: VideoTrack? = null
    private var whipResourceUrl: String? = null // Location header for DELETE on teardown

    private val isRunning = AtomicBoolean(false)
    private val isPublishing = AtomicBoolean(false)
    private val isTearingDown = AtomicBoolean(false)
    private val iceState = AtomicReference("new")
    private val capturedFrames = AtomicLong()

    @Volatile
    var listener: Listener? = null

    fun start() {
        if (isRunning.getAndSet(true)) return
        executor.execute { publishLoop() }
    }

    fun isRunning(): Boolean = isRunning.get()

    fun isPublishing(): Boolean = isPublishing.get()

    /** Ends the loop and waits for its teardown (the WHIP DELETE included); safe from any thread but the loop's. */
    fun stop() {
        if (!isRunning.getAndSet(false)) return
        executor.shutdownNow()
        try {
            if (!executor.awaitTermination(12, TimeUnit.SECONDS)) Log.w(TAG, "publish loop did not finish within 12 s")
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        Log.i(TAG, "WhipPublisher stopped")
    }

    /** Reads the transport stats asynchronously; nothing happens when no peer connection is open. */
    fun getStats(callback: (TransportSample) -> Unit) {
        val connection = peerConnection ?: return
        runCatching {
            connection.getStats { report -> callback(TransportStats.read(report, System.currentTimeMillis(), iceState.get())) }
        }
    }

    // ── internal ────────────────────────────────────────────────────

    private fun publishLoop() {
        var attempt = 0
        while (isRunning.get()) {
            attempt++
            listener?.onAttempt(attempt)
            var failure: PublishFailure? = null
            try {
                publish()
                // publish() blocks until disconnection
                if (isRunning.get()) failure = PublishFailure(PublishReasons.ICE_DISCONNECTED, "ICE state ${iceState.get()}")
            } catch (e: PublishFailure) {
                failure = e
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            } catch (e: Exception) {
                failure = PublishFailure(PublishReasons.INTERNAL_ERROR, "${e.javaClass.simpleName}: ${e.message}")
            } finally {
                teardown()
                isPublishing.set(false)
            }

            if (!isRunning.get() || Thread.currentThread().isInterrupted) break
            val delay = listener?.onFailure(failure?.reason ?: PublishReasons.ICE_DISCONNECTED, failure?.detail ?: "connection lost")
            if (delay == null) {
                isRunning.set(false)
                break
            }
            Log.i(TAG, "Reconnecting in ${delay}ms...")
            try {
                Thread.sleep(delay)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                break
            }
        }
        isRunning.set(false)
        listener?.onStopped()
    }

    /**
     * Single publish attempt. Blocks until the connection closes or
     * [isRunning] becomes false.
     */
    private fun publish() {
        Log.i(TAG, "Publishing to $whipUrl")
        iceState.set("new")
        val factory = WebRTCPeerFactory.create(appContext, encoderFactory).also { factory = it }
        log("encoders offered: ${WebRTCPeerFactory.supportedEncoders(encoderFactory ?: WebRTCPeerFactory.defaultEncoderFactory())}")

        // 1. Create video source & track; the observer wrapper counts frames for the first-frame wait.
        val source = factory.createVideoSource(false).also { videoSource = it }
        val frameLatch = CountDownLatch(1)
        val counting = object : CapturerObserver {
            private val inner = source.capturerObserver

            override fun onCapturerStarted(success: Boolean) = inner.onCapturerStarted(success)

            override fun onCapturerStopped() = inner.onCapturerStopped()

            override fun onFrameCaptured(frame: VideoFrame) {
                capturedFrames.incrementAndGet()
                frameLatch.countDown()
                inner.onFrameCaptured(frame)
            }
        }
        try {
            videoCapturer.initialize(null, appContext, counting)
            videoCapturer.startCapture(options.videoResolutionWidth, options.videoResolutionHeight, options.fps)
        } catch (e: Exception) {
            throw PublishFailure(PublishReasons.SOURCE_UNAVAILABLE, "capturer failed to start: ${e.message ?: e.javaClass.simpleName}")
        }
        if (!frameLatch.await(FIRST_FRAME_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
            throw PublishFailure(PublishReasons.NO_FRAMES, "no video frames within ${FIRST_FRAME_TIMEOUT_MS / 1000} s of starting the source")
        }
        checkRunning()

        videoTrack = factory.createVideoTrack(options.videoTrackId, source).apply { setEnabled(true) }

        // 2. Create PeerConnection (LAN only: no ICE servers, host candidates suffice)
        val rtcConfig = PeerConnection.RTCConfiguration(emptyList()).apply {
            sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
            // Disable CPU overuse detection so WebRTC doesn't auto-downscale resolution
            enableCpuOveruseDetection = false
        }

        val iceGatherLatch = CountDownLatch(1)
        val connected = AtomicBoolean(false)
        val lostAtMs = AtomicLong(0)

        val connection = factory.createPeerConnection(
            rtcConfig,
            object : PeerConnection.Observer {
                override fun onSignalingChange(s: PeerConnection.SignalingState) {}

                override fun onIceConnectionChange(s: PeerConnection.IceConnectionState) {
                    Log.d(TAG, "ICE connection: $s")
                    iceState.set(s.name.lowercase())
                    listener?.onIceState(s.name.lowercase())
                    when (s) {
                        PeerConnection.IceConnectionState.CONNECTED,
                        PeerConnection.IceConnectionState.COMPLETED,
                        -> {
                            lostAtMs.set(0)
                            if (!connected.getAndSet(true)) {
                                isPublishing.set(true)
                                listener?.onPublishing()
                            }
                        }
                        PeerConnection.IceConnectionState.DISCONNECTED -> lostAtMs.compareAndSet(0, System.currentTimeMillis())
                        PeerConnection.IceConnectionState.FAILED,
                        PeerConnection.IceConnectionState.CLOSED,
                        -> connected.set(false)
                        else -> {}
                    }
                }

                override fun onIceConnectionReceivingChange(b: Boolean) {}

                override fun onIceGatheringChange(s: PeerConnection.IceGatheringState) {
                    if (s == PeerConnection.IceGatheringState.COMPLETE) iceGatherLatch.countDown()
                }

                override fun onIceCandidate(c: IceCandidate) {}

                override fun onIceCandidatesRemoved(c: Array<out IceCandidate>) {}

                override fun onAddStream(s: MediaStream) {}

                override fun onRemoveStream(s: MediaStream) {}

                override fun onDataChannel(dc: DataChannel) {}

                override fun onRenegotiationNeeded() {}

                override fun onAddTrack(r: RtpReceiver, ss: Array<out MediaStream>) {}
            },
        ) ?: throw PublishFailure(PublishReasons.INTERNAL_ERROR, "PeerConnection could not be created")
        peerConnection = connection

        // Add video track (sendonly — mediamtx doesn't send back video)
        connection.addTrack(videoTrack, listOf(options.mediaStreamId))

        // Configure sender for stable resolution and a bitrate floor.
        connection.senders.firstOrNull()?.let { sender -> configureVideoSender(sender) }

        // 3. Create offer
        val offerLatch = CountDownLatch(1)
        var localSdp: SessionDescription? = null
        var sdpError: String? = null

        val constraints = MediaConstraints().apply {
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "false"))
            mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "false"))
        }

        connection.createOffer(
            SimpleSdpObserver(
                TAG,
                onSuccess = { sdp ->
                    if (sdp == null) return@SimpleSdpObserver
                    // Prefer H264 and set a short keyframe interval for loss recovery
                    val mungedSdp = SessionDescription(sdp.type, munger.mungeForH264(sdp.description))
                    connection.setLocalDescription(
                        SimpleSdpObserver(
                            TAG,
                            onSuccess = {
                                localSdp = mungedSdp
                                offerLatch.countDown()
                            },
                            onFailure = { error ->
                                sdpError = "setLocalDescription failed: $error"
                                offerLatch.countDown()
                            },
                        ),
                        mungedSdp,
                    )
                },
                onFailure = { error ->
                    sdpError = "createOffer failed: $error"
                    offerLatch.countDown()
                },
            ),
            constraints,
        )

        offerLatch.await(5, TimeUnit.SECONDS)
        if (localSdp == null) throw PublishFailure(PublishReasons.SDP_FAILED, sdpError ?: "no SDP offer within 5 s")

        // 4. Wait for ICE gathering to finish (full SDP needed for WHIP)
        if (!iceGatherLatch.await(ICE_GATHER_TIMEOUT_S, TimeUnit.SECONDS)) {
            Log.w(TAG, "ICE gathering timeout — proceeding with partial candidates")
            log("ICE gathering did not complete within $ICE_GATHER_TIMEOUT_S s; offering the candidates so far")
        }
        checkRunning()

        // Use the local description which now contains all gathered ICE candidates
        val offerSdp = connection.localDescription?.description
            ?: throw PublishFailure(PublishReasons.SDP_FAILED, "no local description after ICE gathering")
        log("offer codecs ${SdpMunger.videoCodecs(offerSdp)}; POST $whipUrl")

        // 5. POST offer to WHIP endpoint
        val session = try {
            whip.publish(whipUrl, offerSdp)
        } catch (e: WhipException) {
            val reason = if (e.status == null) PublishReasons.NETWORK_ERROR else PublishReasons.HTTP_ERROR
            throw PublishFailure(reason, e.message ?: "WHIP POST failed")
        }
        whipResourceUrl = session.resourceUrl
        val negotiated = SdpMunger.negotiatedVideoCodec(session.answerSdp)
        listener?.onOfferAccepted(session.resourceUrl, negotiated)
        log("WHIP answer accepted (codec ${negotiated ?: "unknown"}, resource ${session.resourceUrl ?: "none"})")

        // 6. Set remote description (answer from mediamtx)
        val answerLatch = CountDownLatch(1)
        var answerError: String? = null
        connection.setRemoteDescription(
            SimpleSdpObserver(
                TAG,
                onSuccess = { answerLatch.countDown() },
                onFailure = { error ->
                    answerError = error
                    answerLatch.countDown()
                },
            ),
            SessionDescription(SessionDescription.Type.ANSWER, session.answerSdp),
        )
        if (!answerLatch.await(5, TimeUnit.SECONDS)) throw PublishFailure(PublishReasons.SDP_FAILED, "setRemoteDescription did not complete within 5 s")
        answerError?.let { throw PublishFailure(PublishReasons.SDP_FAILED, "setRemoteDescription failed: $it") }
        runCatching { connection.setBitrate(targetBitrateBps, targetBitrateBps, maxBitrateBps) }

        Log.i(TAG, "WHIP publish started — waiting for connection")
        val startedAtMs = System.currentTimeMillis()

        // 7. Wait until connection drops or we're stopped
        while (isRunning.get()) {
            val state = connection.iceConnectionState()
            when (state) {
                PeerConnection.IceConnectionState.FAILED -> throw PublishFailure(PublishReasons.ICE_FAILED, iceFailureDetail())
                PeerConnection.IceConnectionState.CLOSED -> throw PublishFailure(PublishReasons.ICE_DISCONNECTED, "ICE connection closed")
                PeerConnection.IceConnectionState.DISCONNECTED -> {
                    val lost = lostAtMs.get()
                    if (lost != 0L && System.currentTimeMillis() - lost > DISCONNECT_GRACE_MS) {
                        throw PublishFailure(PublishReasons.ICE_DISCONNECTED, "ICE disconnected for more than ${DISCONNECT_GRACE_MS / 1000} s")
                    }
                }
                else -> {}
            }
            if (!connected.get() && System.currentTimeMillis() - startedAtMs > CONNECT_TIMEOUT_MS) {
                throw PublishFailure(PublishReasons.ICE_FAILED, "no ICE connection within ${CONNECT_TIMEOUT_MS / 1000} s (${iceFailureDetail()})")
            }
            Thread.sleep(POLL_MS)
        }
    }

    private fun iceFailureDetail(): String =
        "ICE state ${iceState.get()}; if the ground station runs MediaMTX in Docker, its LAN address must be in webrtcAdditionalHosts"

    private fun checkRunning() {
        if (!isRunning.get()) throw InterruptedException("stopped")
    }

    private fun teardown() {
        if (!isTearingDown.compareAndSet(false, true)) return
        try {
            deleteWhipResource()
            runCatching { videoCapturer.stopCapture() }
            runCatching { videoTrack?.dispose() }
            videoTrack = null
            runCatching { videoSource?.dispose() }
            videoSource = null
            runCatching { peerConnection?.dispose() }
                .onFailure { Log.d(TAG, "PeerConnection dispose ignored: ${it.message}") }
            peerConnection = null
            runCatching { factory?.dispose() }
            factory = null
        } finally {
            isTearingDown.set(false)
        }
    }

    private fun deleteWhipResource() {
        val resourceUrl = whipResourceUrl ?: return
        whipResourceUrl = null
        val status = whip.delete(resourceUrl)
        Log.d(TAG, "WHIP resource DELETE: ${status ?: "failed"}")
        log("WHIP resource released (DELETE ${status ?: "failed"})")
    }

    /**
     * Configure the RTP sender to maintain framerate under load:
     * - Set max bitrate and framerate
     * - Set DegradationPreference to MAINTAIN_FRAMERATE (scale before dropping FPS), or
     *   DISABLED for passthrough where nothing may adapt the pre-encoded frames.
     */
    private fun configureVideoSender(sender: RtpSender) {
        runCatching {
            val params = sender.parameters ?: return
            for (encoding in params.encodings) {
                encoding.maxBitrateBps = maxBitrateBps
                if (passthrough) encoding.minBitrateBps = targetBitrateBps
                encoding.maxFramerate = if (passthrough) null else options.fps
            }
            params.degradationPreference = if (passthrough) RtpParameters.DegradationPreference.DISABLED else RtpParameters.DegradationPreference.MAINTAIN_FRAMERATE
            sender.parameters = params
            Log.d(TAG, "Sender params tuned: maxBitrate=${maxBitrateBps}bps passthrough=$passthrough")
        }.onFailure { e ->
            Log.w(TAG, "Unable to fully apply sender tuning: ${e.message}")
        }
    }
}
