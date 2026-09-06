package org.worldofhacks.sweep.bridge.video

import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import android.os.Build
import android.view.Surface
import java.io.File
import kotlin.math.PI
import kotlin.math.sin
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.video.FlightOverlay
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence
import org.worldofhacks.sweep.bridge.core.video.StreamFrame
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource

/**
 * The fake flavor's FPV: a synthetic 720p30 scene drawn straight onto the flight display's
 * Surface, with wall markers that scroll as the fake yaw sweeps so the picture and the
 * overlay compass agree, plus a synthetic encoded-frame descriptor per drawn frame (keyframe
 * every 30 frames carrying a real H.264 SPS) so the evidence path, the keyframe cadence, and
 * the bench log are exercised without hardware. Disconnect stops the frames; the display
 * then shows the same "No video" and Disconnected states the aircraft would cause.
 */
class FakeFpv(
    filesDir: File,
    phone: PhoneStatusSource?,
    private val commandedYawDeg: () -> Double,
    override val captureProgress: CaptureProgressSource = IdleCaptureProgress,
) : FpvSession {
    private val tracker = StreamEvidenceTracker(filesDir, phone)
    private val _attitude = MutableStateFlow(AircraftAttitude())
    override val attitude: StateFlow<AircraftAttitude> = _attitude.asStateFlow()

    @Volatile
    private var connected = false

    @Volatile
    private var connectedAtMs = 0L

    override val cameraStream: CameraStream = FakeCameraStream(tracker, ::yawNow, ::connected)

    /** Fake session hook: Connect and Disconnect stand in for the aircraft link. */
    fun setConnected(connected: Boolean) {
        this.connected = connected
        if (connected) {
            connectedAtMs = System.currentTimeMillis()
            tracker.note("fake aircraft connected")
        } else {
            tracker.note("fake aircraft disconnected; stream released")
            tracker.reset()
            _attitude.value = AircraftAttitude()
        }
    }

    /** Commanded yaw plus a slow ±60° sweep so the compass moves; null while disconnected. */
    private fun yawNow(): Double? {
        if (!connected) return null
        val elapsed = (System.currentTimeMillis() - connectedAtMs) / 1000.0
        val yaw = FlightOverlay.heading(commandedYawDeg() + SWEEP_AMPLITUDE_DEG * sin(2 * PI * elapsed / SWEEP_PERIOD_S))
        _attitude.value = AircraftAttitude(yawDeg = yaw, pitchDeg = 0.0, rollDeg = 0.0, atMs = System.currentTimeMillis())
        return yaw
    }

    private companion object {
        const val SWEEP_AMPLITUDE_DEG = 60.0
        const val SWEEP_PERIOD_S = 40.0
    }
}

class FakeCameraStream(
    private val tracker: StreamEvidenceTracker,
    private val yaw: () -> Double?,
    private val live: () -> Boolean,
) : CameraStream {
    override val evidence: StateFlow<StreamEvidence?>
        get() = tracker.evidence

    override val logPath: StateFlow<String?>
        get() = tracker.logPath

    override val lastFrameAtMs: StateFlow<Long?>
        get() = tracker.lastFrameAtMs

    private var surface: Surface? = null
    private var drawer: Thread? = null

    @Synchronized
    override fun attachSurface(surface: Surface, width: Int, height: Int) {
        if (this.surface === surface && drawer?.isAlive == true) return
        stopDrawer()
        this.surface = surface
        tracker.start()
        val thread = Thread({ loop(surface) }, "fake-fpv").apply { isDaemon = true }
        drawer = thread
        thread.start()
    }

    @Synchronized
    override fun detachSurface(surface: Surface) {
        if (this.surface !== surface) return
        stopDrawer()
        this.surface = null
        tracker.stop("surface detached")
        tracker.reset()
    }

    private fun stopDrawer() {
        drawer?.let { thread ->
            thread.interrupt()
            runCatching { thread.join(JOIN_MS) }
        }
        drawer = null
    }

    private fun loop(surface: Surface) {
        var frame = 0L
        val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        while (!Thread.currentThread().isInterrupted) {
            val started = System.nanoTime()
            val connected = live()
            val canvas = try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) surface.lockHardwareCanvas() else surface.lockCanvas(null)
            } catch (_: Exception) {
                return
            }
            try {
                draw(canvas, paint, frame, if (connected) yaw() else null)
            } finally {
                runCatching { surface.unlockCanvasAndPost(canvas) }
            }
            if (connected) {
                val keyframe = frame % GOP_FRAMES == 0L
                tracker.frame(
                    StreamFrame(
                        mimeType = "video/avc",
                        width = WIDTH,
                        height = HEIGHT,
                        nominalFrameRateHz = FRAME_RATE,
                        keyFrame = keyframe,
                        presentationTimeMs = frame * 1000 / FRAME_RATE,
                        sizeBytes = if (keyframe) KEYFRAME_BYTES else FRAME_BYTES,
                    ),
                    data = if (keyframe) FAKE_IDR_PREFIX else null,
                    offset = 0,
                    length = if (keyframe) FAKE_IDR_PREFIX.size else 0,
                )
                frame++
            }
            val elapsedMs = (System.nanoTime() - started) / 1_000_000
            val sleep = (1000L / FRAME_RATE) - elapsedMs
            if (sleep > 0) {
                try {
                    Thread.sleep(sleep)
                } catch (_: InterruptedException) {
                    return
                }
            }
        }
    }

    private fun draw(canvas: Canvas, paint: Paint, frame: Long, yawDeg: Double?) {
        val w = canvas.width.toFloat()
        val h = canvas.height.toFloat()
        val horizon = h * 0.55f
        paint.shader = LinearGradient(0f, 0f, 0f, horizon, Color.rgb(46, 58, 72), Color.rgb(120, 132, 146), Shader.TileMode.CLAMP)
        canvas.drawRect(0f, 0f, w, horizon, paint)
        paint.shader = LinearGradient(0f, horizon, 0f, h, Color.rgb(74, 70, 64), Color.rgb(34, 32, 30), Shader.TileMode.CLAMP)
        canvas.drawRect(0f, horizon, w, h, paint)
        paint.shader = null
        if (yawDeg == null) {
            paint.color = Color.rgb(20, 20, 22)
            canvas.drawRect(0f, 0f, w, h, paint)
            paint.color = Color.WHITE
            paint.textSize = h * 0.06f
            canvas.drawText("No aircraft: fake feed stopped", w * 0.08f, h * 0.5f, paint)
            return
        }
        paint.color = Color.rgb(200, 205, 210)
        paint.strokeWidth = 3f
        canvas.drawLine(0f, horizon, w, horizon, paint)
        // Wall markers every 30° of azimuth, placed by their angle from the current yaw across
        // the drawn field of view, so a yaw change scrolls the scene like a real camera pan.
        paint.textSize = h * 0.05f
        for (azimuth in 0 until 360 step 30) {
            val delta = FlightOverlay.wrapDelta(yawDeg, azimuth.toDouble())
            if (delta < -DRAW_HFOV_DEG / 2 || delta > DRAW_HFOV_DEG / 2) continue
            val x = (w / 2f + (delta / DRAW_HFOV_DEG) * w).toFloat()
            paint.color = if (azimuth == 0) Color.rgb(255, 179, 0) else Color.rgb(230, 230, 230)
            paint.strokeWidth = 6f
            canvas.drawLine(x, horizon - h * 0.28f, x, horizon, paint)
            canvas.drawText("%03d".format(azimuth), x + 8f, horizon - h * 0.3f, paint)
        }
        // A moving element so consecutive frames differ even at a steady yaw.
        val bounce = (sin(frame / 15.0) * 0.5 + 0.5).toFloat()
        paint.color = Color.rgb(255, 255, 255)
        canvas.drawCircle(w * (0.15f + 0.7f * bounce), horizon + h * 0.2f, h * 0.02f, paint)
        paint.color = if (frame % GOP_FRAMES == 0L) Color.rgb(255, 179, 0) else Color.rgb(160, 160, 160)
        canvas.drawRect(w - 40f, 24f, w - 16f, 48f, paint)
        paint.color = Color.rgb(220, 220, 220)
        paint.textSize = h * 0.035f
        canvas.drawText("FAKE FEED ${WIDTH}×$HEIGHT · frame $frame · yaw ${yawDeg.toInt()}°", 24f, h - 24f - h * 0.12f, paint)
    }

    companion object {
        const val WIDTH = 1280
        const val HEIGHT = 720
        const val FRAME_RATE = 30
        const val GOP_FRAMES = 30L
        const val KEYFRAME_BYTES = 60_000
        const val FRAME_BYTES = 12_000
        const val DRAW_HFOV_DEG = 90.0
        private const val JOIN_MS = 500L

        /** Annex B SPS (H.264 Main, level 3.1) and PPS, as an IDR access unit begins. */
        val FAKE_IDR_PREFIX: ByteArray = intArrayOf(
            0x00, 0x00, 0x00, 0x01, 0x67, 0x4D, 0x40, 0x1F, 0xE8, 0x80, 0x50, 0x17, 0xFC, 0xB0, 0x80,
            0x00, 0x00, 0x03, 0x00, 0x80, 0x00, 0x00, 0x19, 0x07, 0x8B, 0x16, 0xCB,
            0x00, 0x00, 0x00, 0x01, 0x68, 0xEE, 0x3C, 0xB0,
        ).map { it.toByte() }.toByteArray()
    }
}
