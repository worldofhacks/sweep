package org.worldofhacks.sweep.bridge.publish

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import org.webrtc.CapturerObserver
import org.webrtc.JavaI420Buffer
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoFrame
import org.webrtc.YuvHelper
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics

/**
 * The fake flavor's frame source: colour bars, a block sweeping along the bottom, and a
 * wall-clock timestamp with the frame counter, drawn with `Canvas` and converted to I420.
 * The moving elements make frozen video and latency visible on the console without an
 * aircraft (the wall clock next to the console's clock is the glass-to-glass estimate).
 */
class TestPatternCapturer(private val droneId: Int, private val metrics: (WebRTCStreamMetrics) -> Unit) : VideoCapturer {
    private var observer: CapturerObserver? = null
    private var executor: ScheduledExecutorService? = null
    private val running = AtomicBoolean(false)
    private val frames = AtomicLong()
    private var width = 1280
    private var height = 720
    private var fps = 30
    private var bitmap: Bitmap? = null
    private var canvas: Canvas? = null
    private var rgba: ByteBuffer? = null
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val timeFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private var windowFrames = 0L
    private var windowProcessingNs = 0L
    private var windowStartedNs = System.nanoTime()
    private var lastError: String? = null

    override fun initialize(surfaceTextureHelper: SurfaceTextureHelper?, applicationContext: Context, capturerObserver: CapturerObserver) {
        observer = capturerObserver
    }

    override fun startCapture(width: Int, height: Int, framerate: Int) {
        this.width = even(width.coerceAtLeast(2))
        this.height = even(height.coerceAtLeast(2))
        fps = framerate.coerceIn(1, 60)
        if (!running.compareAndSet(false, true)) return
        bitmap = Bitmap.createBitmap(this.width, this.height, Bitmap.Config.ARGB_8888).also { canvas = Canvas(it) }
        rgba = ByteBuffer.allocateDirect(this.width * this.height * 4)
        windowStartedNs = System.nanoTime()
        observer?.onCapturerStarted(true)
        executor = Executors.newSingleThreadScheduledExecutor { runnable -> Thread(runnable, "test-pattern").apply { isDaemon = true } }.also {
            it.scheduleAtFixedRate(::emit, 0, (1_000_000L / fps), TimeUnit.MICROSECONDS)
        }
    }

    override fun stopCapture() {
        if (!running.compareAndSet(true, false)) return
        executor?.shutdownNow()
        executor = null
        observer?.onCapturerStopped()
    }

    override fun changeCaptureFormat(width: Int, height: Int, framerate: Int) = Unit

    override fun dispose() {
        stopCapture()
        bitmap?.recycle()
        bitmap = null
        canvas = null
        rgba = null
        observer = null
    }

    override fun isScreencast(): Boolean = false

    private fun emit() {
        if (!running.get()) return
        val target = observer ?: return
        val startedNs = System.nanoTime()
        try {
            val frame = frames.getAndIncrement()
            draw(frame)
            val buffer = toI420()
            val videoFrame = VideoFrame(buffer, 0, startedNs)
            target.onFrameCaptured(videoFrame)
            videoFrame.release()
            windowFrames++
            windowProcessingNs += System.nanoTime() - startedNs
        } catch (error: RuntimeException) {
            lastError = error.message ?: error.javaClass.simpleName
        }
        maybeEmitMetrics(System.nanoTime())
    }

    private fun draw(frame: Long) {
        val canvas = canvas ?: return
        val barWidth = width / BARS.size.toFloat()
        for ((index, colour) in BARS.withIndex()) {
            paint.color = colour
            canvas.drawRect(index * barWidth, 0f, (index + 1) * barWidth, height * 0.55f, paint)
        }
        paint.color = Color.BLACK
        canvas.drawRect(0f, height * 0.55f, width.toFloat(), height.toFloat(), paint)
        // A block sweeping left to right once every two seconds at the nominal frame rate.
        val block = height / 12f
        val travel = width - block
        val x = ((frame % (2L * fps)) / (2f * fps)) * travel
        paint.color = Color.WHITE
        canvas.drawRect(x, height - block * 1.5f, x + block, height - block * 0.5f, paint)
        paint.typeface = Typeface.MONOSPACE
        paint.textSize = height / 9f
        paint.color = Color.WHITE
        canvas.drawText(timeFormat.format(Date()), width * 0.04f, height * 0.72f, paint)
        paint.textSize = height / 18f
        canvas.drawText("SWEEP drone$droneId test pattern  frame $frame  ${width}x$height@$fps", width * 0.04f, height * 0.82f, paint)
    }

    private fun toI420(): JavaI420Buffer {
        val bitmap = checkNotNull(bitmap)
        val rgba = checkNotNull(rgba)
        rgba.rewind()
        bitmap.copyPixelsToBuffer(rgba)
        rgba.rewind()
        val i420 = JavaI420Buffer.allocate(width, height)
        // ARGB_8888 is R,G,B,A in memory, which libyuv calls ABGR.
        YuvHelper.ABGRToI420(rgba, width * 4, i420.dataY, i420.strideY, i420.dataU, i420.strideU, i420.dataV, i420.strideV, width, height)
        return i420
    }

    private fun maybeEmitMetrics(nowNs: Long) {
        val elapsedNs = nowNs - windowStartedNs
        if (elapsedNs < 1_000_000_000L) return
        val seconds = elapsedNs / 1_000_000_000.0
        val sent = windowFrames
        val processingMs = if (sent > 0) windowProcessingNs / sent / 1_000_000.0 else 0.0
        windowFrames = 0
        windowProcessingNs = 0
        windowStartedNs = nowNs
        metrics(
            WebRTCStreamMetrics(
                sourceWidth = width,
                sourceHeight = height,
                outputWidth = width,
                outputHeight = height,
                requestedWidth = width,
                requestedHeight = height,
                targetFps = fps,
                inputFps = sent / seconds,
                outputFps = sent / seconds,
                averageFrameProcessingMs = processingMs,
                totalFrames = frames.get(),
                observerCount = 1,
                activeCamera = "test_pattern",
                status = if (running.get()) "running" else "idle",
                configuredFps = fps,
                lastError = lastError,
            ),
        )
    }

    private fun even(value: Int): Int = value - value % 2

    private companion object {
        val BARS = intArrayOf(
            Color.rgb(192, 192, 192),
            Color.rgb(192, 192, 0),
            Color.rgb(0, 192, 192),
            Color.rgb(0, 192, 0),
            Color.rgb(192, 0, 192),
            Color.rgb(192, 0, 0),
            Color.rgb(0, 0, 192),
            Color.rgb(16, 16, 16),
        )
    }
}
