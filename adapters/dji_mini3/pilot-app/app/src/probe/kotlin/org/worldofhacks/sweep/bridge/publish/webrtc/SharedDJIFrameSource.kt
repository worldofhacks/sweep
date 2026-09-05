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

import android.util.Log
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.interfaces.ICameraStreamManager
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import org.webrtc.CapturerObserver
import org.webrtc.NV21Buffer
import org.webrtc.VideoFrame
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics

/**
 * Shared video frame source that registers a single DJI CameraFrameListener,
 * scales once, and broadcasts the resulting VideoFrame to all registered
 * WebRTC CapturerObservers.  This eliminates duplicate NV21→scale→encode work
 * when multiple viewers are connected.
 *
 * Sweep changes: this is the explicit "re-encode on the phone" source, never the default.
 * The per-frame telemetry metadata (`TelemetryProvider`) and the Matrice 400 payload-port
 * exclusion are not carried over; the default target is the source resolution (no scaling)
 * at 30 fps; a [log] sink mirrors the lifecycle lines onto the app's screen.
 */
class SharedDJIFrameSource(
    private val preferredCameraIndex: ComponentIndexType,
    private val log: (String) -> Unit = {},
) {
    companion object {
        private const val TAG = "SharedDJIFrameSource"

        // Order in which we fall back when the preferred camera index is not
        // exposed by the connected aircraft.
        private val CAMERA_PREFERENCE = listOf(
            ComponentIndexType.LEFT_OR_MAIN,
            ComponentIndexType.FPV,
            ComponentIndexType.RIGHT,
            ComponentIndexType.UP,
        )
    }

    /** Currently used camera index. Resolved dynamically from the available
     *  camera list reported by the SDK. Starts at the caller's preferred
     *  index but is updated as soon as the SDK publishes the real list. */
    @Volatile
    private var activeCameraIndex: ComponentIndexType = preferredCameraIndex

    private val observers = ConcurrentHashMap<String, CapturerObserver>()
    private val isCapturing = AtomicBoolean(false)

    @Volatile var targetWidth: Int = 0

    @Volatile var targetHeight: Int = 0

    @Volatile private var scaleToTarget: Boolean = false

    @Volatile private var targetFps: Int = 30

    @Volatile private var frameIntervalNs: Long = 1_000_000_000L / 30L

    @Volatile var metricsListener: ((WebRTCStreamMetrics) -> Unit)? = null
    private val lastSentTimestampNs = AtomicLong(0L)
    private val frameCounter = AtomicLong(0)
    private val incomingFrameCounter = AtomicLong(0)
    private val droppedFrameCounter = AtomicLong(0)
    private val processingErrorCounter = AtomicLong(0)
    private val recoveryCounter = AtomicLong(0)

    @Suppress("PLATFORM_CLASS_MAPPED_TO_KOTLIN")
    private val frameWaitLock = Object()
    private val sentFramesInWindow = AtomicLong(0)
    private val inputFramesInWindow = AtomicLong(0)
    private val droppedFramesInWindow = AtomicLong(0)
    private val processingTimeNsInWindow = AtomicLong(0)
    private var lastSourceWidth = 0
    private var lastSourceHeight = 0
    private var lastOutputWidth = 0
    private var lastOutputHeight = 0
    private var lastMetricsTimestampNs = System.nanoTime()

    @Volatile private var lastError: String? = null

    private val cameraStreamManager: ICameraStreamManager by lazy {
        MediaDataCenter.getInstance().cameraStreamManager
    }

    private val availableCameraListener = object : ICameraStreamManager.AvailableCameraUpdatedListener {
        override fun onAvailableCameraUpdated(availableCameraList: MutableList<ComponentIndexType>) {
            onAvailableCamerasChanged(availableCameraList)
        }

        override fun onCameraStreamEnableUpdate(cameraStreamEnableMap: MutableMap<ComponentIndexType, Boolean>) {
            // Not used; frame availability is detected by the frame callback itself.
        }
    }

    init {
        // Begin observing available cameras as early as possible so we can
        // attach the frame listener to a camera index that actually exists
        // on the connected aircraft.
        try {
            cameraStreamManager.addAvailableCameraUpdatedListener(availableCameraListener)
        } catch (e: Exception) {
            Log.w(TAG, "Could not register available-camera listener: ${e.message}")
        }
    }

    /**
     * Pick the best camera to stream from given the list reported by the SDK.
     * Preference order:
     *  1. The caller's preferred index (if present in the list).
     *  2. Any entry from CAMERA_PREFERENCE that is present, in order.
     *  3. The first entry in the list as a last resort.
     */
    private fun pickCameraIndex(available: List<ComponentIndexType>): ComponentIndexType? {
        if (available.isEmpty()) return null
        if (available.contains(preferredCameraIndex)) return preferredCameraIndex
        for (candidate in CAMERA_PREFERENCE) {
            if (available.contains(candidate)) return candidate
        }
        return available.first()
    }

    @Synchronized
    private fun onAvailableCamerasChanged(available: List<ComponentIndexType>) {
        val resolved = pickCameraIndex(available) ?: run {
            Log.w(TAG, "Available camera list is empty — keeping current index $activeCameraIndex")
            return
        }
        if (resolved == activeCameraIndex) {
            Log.d(TAG, "Available cameras updated ($available); active index unchanged: $activeCameraIndex")
            return
        }
        val previous = activeCameraIndex
        activeCameraIndex = resolved
        Log.i(TAG, "Active camera index changed: $previous -> $resolved (available: $available, preferred: $preferredCameraIndex)")
        log("camera index changed: $previous -> $resolved (available $available)")

        // If we're already streaming, re-attach the frame listener to the new index.
        if (isCapturing.get()) {
            try {
                cameraStreamManager.removeFrameListener(frameListener)
                cameraStreamManager.addFrameListener(
                    activeCameraIndex,
                    ICameraStreamManager.FrameFormat.NV21,
                    frameListener,
                )
                Log.i(TAG, "Re-attached frame listener on $activeCameraIndex")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to re-attach frame listener on $activeCameraIndex: ${e.message}", e)
            }
        }
    }

    private val frameListener = object : ICameraStreamManager.CameraFrameListener {
        override fun onFrame(
            frameData: ByteArray,
            offset: Int,
            length: Int,
            width: Int,
            height: Int,
            format: ICameraStreamManager.FrameFormat,
        ) {
            if (!isCapturing.get()) return
            val singleObserver = if (observers.size == 1) observers.values.firstOrNull() else null
            val currentObservers = if (singleObserver == null) observers.values.toList() else emptyList()
            if (singleObserver == null && currentObservers.isEmpty()) return

            try {
                val timestampNs = System.nanoTime()
                incomingFrameCounter.incrementAndGet()
                inputFramesInWindow.incrementAndGet()

                val previousSent = lastSentTimestampNs.get()
                if (previousSent != 0L && (timestampNs - previousSent) < frameIntervalNs) {
                    droppedFrameCounter.incrementAndGet()
                    droppedFramesInWindow.incrementAndGet()
                    maybeEmitMetrics(timestampNs)
                    return
                }
                lastSentTimestampNs.set(timestampNs)

                if (width != lastSourceWidth || height != lastSourceHeight) {
                    lastSourceWidth = width
                    lastSourceHeight = height
                    Log.d(TAG, "Source: ${width}x$height, Target: ${targetWidth}x$targetHeight, Scale: $scaleToTarget")
                    log("decoded source ${width}x$height ($format)")
                }

                frameCounter.incrementAndGet()
                synchronized(frameWaitLock) {
                    frameWaitLock.notifyAll()
                }
                sentFramesInWindow.incrementAndGet()

                val (outputWidth, outputHeight) = chooseOutputSize(width, height)
                lastOutputWidth = outputWidth
                lastOutputHeight = outputHeight

                // Create NV21 buffer and scale ONCE
                val buffer = NV21Buffer(frameData, width, height, null)

                val needsScale = scaleToTarget && (width != outputWidth || height != outputHeight)
                val outputBuffer = if (needsScale) {
                    val scaled = buffer.cropAndScale(0, 0, width, height, outputWidth, outputHeight)
                    buffer.release()
                    scaled
                } else {
                    buffer
                }

                // Broadcast the same VideoFrame to every observer.
                // Retain once per extra observer; the first consumer uses the initial ref.
                val videoFrame = VideoFrame(outputBuffer, 0, timestampNs)
                if (singleObserver != null) {
                    singleObserver.onFrameCaptured(videoFrame)
                } else {
                    val extra = currentObservers.size - 1
                    repeat(extra) { videoFrame.retain() }
                    currentObservers.forEach { it.onFrameCaptured(videoFrame) }
                }
                videoFrame.release()
                processingTimeNsInWindow.addAndGet(System.nanoTime() - timestampNs)
                maybeEmitMetrics(timestampNs)
            } catch (e: Exception) {
                processingErrorCounter.incrementAndGet()
                lastError = e.message
                Log.e(TAG, "Error processing frame: ${e.message}", e)
            }
        }
    }

    private fun chooseOutputSize(sourceWidth: Int, sourceHeight: Int): Pair<Int, Int> {
        if (!scaleToTarget) return sourceWidth to sourceHeight
        val boundedWidth = targetWidth.coerceAtMost(sourceWidth).coerceAtLeast(2)
        val boundedHeight = targetHeight.coerceAtMost(sourceHeight).coerceAtLeast(2)
        val evenWidth = boundedWidth - (boundedWidth % 2)
        val evenHeight = boundedHeight - (boundedHeight % 2)
        return evenWidth.coerceAtLeast(2) to evenHeight.coerceAtLeast(2)
    }

    fun totalOutputFrames(): Long = frameCounter.get()

    fun observerCount(): Int = observers.size

    fun waitForOutputFrameAfter(frameCount: Long, timeoutMs: Long): Boolean {
        val deadlineMs = System.currentTimeMillis() + timeoutMs
        synchronized(frameWaitLock) {
            while (frameCounter.get() <= frameCount) {
                val remainingMs = deadlineMs - System.currentTimeMillis()
                if (remainingMs <= 0L) return false
                runCatching { frameWaitLock.wait(remainingMs) }
            }
        }
        return true
    }

    private fun maybeEmitMetrics(nowNs: Long) {
        val elapsedNs = nowNs - lastMetricsTimestampNs
        if (elapsedNs < 1_000_000_000L) return

        val elapsedSeconds = elapsedNs / 1_000_000_000.0
        val inputFps = inputFramesInWindow.getAndSet(0) / elapsedSeconds
        val sentFrames = sentFramesInWindow.getAndSet(0)
        val outputFps = sentFrames / elapsedSeconds
        val droppedFps = droppedFramesInWindow.getAndSet(0) / elapsedSeconds
        val processingNs = processingTimeNsInWindow.getAndSet(0)
        val averageProcessingMs = if (sentFrames > 0) processingNs / sentFrames / 1_000_000.0 else 0.0
        lastMetricsTimestampNs = nowNs

        metricsListener?.invoke(
            WebRTCStreamMetrics(
                sourceWidth = lastSourceWidth,
                sourceHeight = lastSourceHeight,
                outputWidth = lastOutputWidth,
                outputHeight = lastOutputHeight,
                requestedWidth = targetWidth,
                requestedHeight = targetHeight,
                targetFps = targetFps,
                inputFps = inputFps,
                outputFps = outputFps,
                droppedFps = droppedFps,
                averageFrameProcessingMs = averageProcessingMs,
                totalFrames = frameCounter.get(),
                totalDroppedFrames = droppedFrameCounter.get(),
                processingErrors = processingErrorCounter.get(),
                observerCount = observers.size,
                activeCamera = activeCameraIndex.name,
                status = if (isCapturing.get()) "running" else "idle",
                recoveryCount = recoveryCounter.get().toInt(),
                scaleMode = if (scaleToTarget) "fixed" else "native",
                lastError = lastError,
            ),
        )
    }

    // ---- Client management ----

    fun registerObserver(clientId: String, observer: CapturerObserver) {
        observers[clientId] = observer
        Log.d(TAG, "Observer registered: $clientId (total: ${observers.size})")
    }

    /**
     * Start capturing if not already started. If already capturing, the
     * new client immediately begins receiving frames.
     */
    fun startClient(clientId: String, width: Int, height: Int, fps: Int) {
        // Use the first client's requested settings
        if (isCapturing.compareAndSet(false, true)) {
            applyResolutionRequest(width, height)
            targetFps = fps.coerceAtLeast(1)
            frameIntervalNs = 1_000_000_000L / targetFps.toLong()
            lastSentTimestampNs.set(0L)
            Log.d(TAG, "Starting shared capture: ${targetWidth}x$targetHeight@${targetFps}fps on camera $activeCameraIndex (preferred: $preferredCameraIndex)")
            log("re-encode source: decoded NV21 frames from $activeCameraIndex at $targetFps fps")
            runCatching { cameraStreamManager.enableStream(activeCameraIndex, true) }
                .onFailure { Log.w(TAG, "Could not enable stream on $activeCameraIndex: ${it.message}") }
            cameraStreamManager.addFrameListener(
                activeCameraIndex,
                ICameraStreamManager.FrameFormat.NV21,
                frameListener,
            )
        }
        observers[clientId]?.onCapturerStarted(true)
    }

    fun stopClient(clientId: String) {
        observers[clientId]?.onCapturerStopped()
        observers.remove(clientId)
        Log.d(TAG, "Client removed: $clientId (remaining: ${observers.size})")
        if (observers.isEmpty() && isCapturing.compareAndSet(true, false)) {
            Log.d(TAG, "No observers left – stopping shared capture")
            cameraStreamManager.removeFrameListener(frameListener)
        }
    }

    fun changeResolution(width: Int, height: Int) {
        val previousWidth = targetWidth
        val previousHeight = targetHeight
        val previousScale = scaleToTarget
        applyResolutionRequest(width, height)
        Log.d(
            TAG,
            "Changing target resolution: ${previousWidth}x$previousHeight (scale=$previousScale) -> ${targetWidth}x$targetHeight (scale=$scaleToTarget)",
        )
    }

    private fun applyResolutionRequest(width: Int, height: Int) {
        if (width <= 0 || height <= 0) {
            targetWidth = 0
            targetHeight = 0
            scaleToTarget = false
            return
        }
        targetWidth = width
        targetHeight = height
        scaleToTarget = true
    }

    fun changeFrameRate(fps: Int) {
        val boundedFps = fps.coerceIn(1, 60)
        Log.d(TAG, "Changing target FPS: $targetFps -> $boundedFps")
        targetFps = boundedFps
        frameIntervalNs = 1_000_000_000L / boundedFps.toLong()
        lastSentTimestampNs.set(0L)
    }

    @Synchronized
    fun recoverCapture(reason: String) {
        if (!isCapturing.get()) return
        recoveryCounter.incrementAndGet()
        lastSentTimestampNs.set(0L)
        runCatching { cameraStreamManager.removeFrameListener(frameListener) }
        runCatching { cameraStreamManager.enableStream(activeCameraIndex, true) }
            .onFailure { Log.w(TAG, "Could not enable stream during listener reset on $activeCameraIndex: ${it.message}") }
        cameraStreamManager.addFrameListener(
            activeCameraIndex,
            ICameraStreamManager.FrameFormat.NV21,
            frameListener,
        )
        Log.w(TAG, "Reset DJI frame listener on $activeCameraIndex: $reason")
    }

    fun dispose() {
        if (isCapturing.compareAndSet(true, false)) {
            cameraStreamManager.removeFrameListener(frameListener)
        }
        try {
            cameraStreamManager.removeAvailableCameraUpdatedListener(availableCameraListener)
        } catch (e: Exception) {
            Log.w(TAG, "Could not remove available-camera listener: ${e.message}")
        }
        observers.clear()
        metricsListener = null
        Log.d(TAG, "SharedDJIFrameSource disposed")
    }
}
