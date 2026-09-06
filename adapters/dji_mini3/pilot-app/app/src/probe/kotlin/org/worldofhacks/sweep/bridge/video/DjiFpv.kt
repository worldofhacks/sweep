package org.worldofhacks.sweep.bridge.video

import android.view.Surface
import android.os.SystemClock
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.common.Attitude
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.common.callback.CommonCallbacks
import dji.v5.manager.KeyManager
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.datacenter.camera.StreamInfo
import dji.v5.manager.interfaces.ICameraStreamManager
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence
import org.worldofhacks.sweep.bridge.core.video.StreamFrame
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource

/**
 * The probe flavor's FPV (Phase D1): the default camera's stream into the flight display's
 * Surface through `ICameraStreamManager.putCameraStreamSurface`, the encoded frames through
 * `addReceiveStreamListener` for the codec evidence, and `KeyAircraftAttitude` for the yaw
 * that drives the coverage compass. The Surface is put only while a product is connected and
 * released on disconnect and on detach, so the stream manager never holds a dead Surface.
 */
class DjiFpv(
    filesDir: File,
    phone: PhoneStatusSource?,
    private val log: (name: String, detail: String) -> Unit,
    override val captureProgress: CaptureProgressSource = IdleCaptureProgress,
) : FpvSession {
    private val tracker = StreamEvidenceTracker(
        filesDir,
        phone,
        receivedAtMonotonicMs = SystemClock::elapsedRealtime,
    )
    private val holder = Any()
    private val lock = Any()
    private var listening = false

    private val _attitude = MutableStateFlow(AircraftAttitude())
    override val attitude: StateFlow<AircraftAttitude> = _attitude.asStateFlow()

    private val camera = DjiCameraStream(tracker, log)
    override val cameraStream: CameraStream
        get() = camera

    /** SdkSession hook: after registration and on every product connect; safe to call again. */
    fun attach() {
        synchronized(lock) {
            if (listening) return
            listening = true
        }
        val manager = KeyManager.getInstance()
        val key = KeyTools.createKey(FlightControllerKey.KeyAircraftAttitude)
        if (!manager.isKeySupported(key)) {
            // Support is answered for the product connected right now, and registration
            // usually completes before the aircraft is there. The listener is registered
            // anyway; on a product that really lacks the key it simply never fires.
            log("Attitude key", "KeyAircraftAttitude not reported as supported yet; listening anyway")
        }
        manager.listen(
            key,
            holder,
            CommonCallbacks.KeyListener<Attitude> { _, newValue ->
                if (newValue != null) {
                    _attitude.value = AircraftAttitude(
                        yawDeg = newValue.yaw,
                        pitchDeg = newValue.pitch,
                        rollDeg = newValue.roll,
                        atMs = System.currentTimeMillis(),
                    )
                }
            },
        )
    }

    /** SdkSession hook: the SDK manager's product connect and disconnect callbacks. */
    fun productConnected(connected: Boolean) {
        camera.productConnected(connected)
        if (!connected) _attitude.value = AircraftAttitude()
    }
}

class DjiCameraStream(
    private val tracker: StreamEvidenceTracker,
    private val log: (name: String, detail: String) -> Unit,
) : CameraStream {
    override val evidence: StateFlow<StreamEvidence?>
        get() = tracker.evidence

    override val logPath: StateFlow<String?>
        get() = tracker.logPath

    override val lastFrameAtMs: StateFlow<Long?>
        get() = tracker.lastFrameAtMs

    private var surface: Surface? = null
    private var width = 0
    private var height = 0
    private var productConnected = false
    private var attached = false

    private val listener = ICameraStreamManager.ReceiveStreamListener { data, offset, length, info ->
        onReceiveStream(data, offset, length, info)
    }

    @Synchronized
    override fun attachSurface(surface: Surface, width: Int, height: Int) {
        val fresh = this.surface !== surface
        this.surface = surface
        this.width = width
        this.height = height
        if (fresh) tracker.start()
        if (productConnected) put()
    }

    @Synchronized
    override fun detachSurface(surface: Surface) {
        if (this.surface !== surface) return
        release("surface detached")
        this.surface = null
        tracker.stop("surface detached")
        tracker.reset()
    }

    @Synchronized
    fun productConnected(connected: Boolean) {
        productConnected = connected
        if (connected) {
            if (surface != null) put()
        } else {
            release("aircraft disconnected")
            tracker.note("aircraft disconnected; stream released")
            tracker.reset()
        }
    }

    private fun put() {
        val current = surface ?: return
        runCatching {
            val manager = MediaDataCenter.getInstance().cameraStreamManager
            manager.putCameraStreamSurface(CAMERA, current, width, height, ICameraStreamManager.ScaleType.CENTER_INSIDE)
            if (!attached) {
                manager.addReceiveStreamListener(CAMERA, listener)
                attached = true
            }
            tracker.note("receive-stream listener attached")
            log("Camera stream", "surface attached ${width}x$height; receive-stream listener on")
        }.onFailure { error ->
            log("Camera stream", "attach failed: ${error.message ?: error.javaClass.simpleName}")
        }
    }

    private fun release(reason: String) {
        val current = surface
        if (current == null && !attached) return
        runCatching {
            val manager = MediaDataCenter.getInstance().cameraStreamManager
            if (current != null) manager.removeCameraStreamSurface(current)
            if (attached) manager.removeReceiveStreamListener(listener)
        }.onFailure { error ->
            log("Camera stream", "release failed: ${error.message ?: error.javaClass.simpleName}")
        }
        attached = false
        tracker.note("receive-stream listener released: $reason")
        log("Camera stream", "surface released: $reason")
    }

    private fun onReceiveStream(data: ByteArray, offset: Int, length: Int, info: StreamInfo) {
        val keyframe = info.isKeyFrame
        tracker.frame(
            StreamFrame(
                mimeType = mimeOf(info.mimeType),
                width = info.width,
                height = info.height,
                nominalFrameRateHz = info.frameRate,
                keyFrame = keyframe,
                presentationTimeMs = info.presentationTimeMs,
                sizeBytes = length,
            ),
            data = if (keyframe) data else null,
            offset = offset,
            length = length,
        )
    }

    private fun mimeOf(type: ICameraStreamManager.MimeType?): String = when (type) {
        ICameraStreamManager.MimeType.H264 -> "video/avc"
        ICameraStreamManager.MimeType.H265 -> "video/hevc"
        null -> "unknown"
    }

    private companion object {
        val CAMERA: ComponentIndexType = ComponentIndexType.LEFT_OR_MAIN
    }
}
