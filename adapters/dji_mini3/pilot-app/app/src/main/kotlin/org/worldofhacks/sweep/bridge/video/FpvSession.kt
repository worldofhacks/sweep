package org.worldofhacks.sweep.bridge.video

import android.view.Surface
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.video.CaptureProgress
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence

/** Aircraft attitude as the SDK reports it (degrees); a null field is unknown, never invented. */
data class AircraftAttitude(
    val yawDeg: Double? = null,
    val pitchDeg: Double? = null,
    val rollDeg: Double? = null,
    val atMs: Long? = null,
)

/**
 * Phase D hook: the flavor's local camera stream behind the flight display's `Surface`. The
 * probe flavor forwards to `ICameraStreamManager`; the fake flavor draws a synthetic scene.
 * The Surface is attached when the view is laid out and released when it is destroyed and
 * whenever the aircraft disconnects.
 */
interface CameraStream {
    /** Codec evidence folded from the receive-stream listener; null until the first frame. */
    val evidence: StateFlow<StreamEvidence?>

    /** Path of the bench JSONL the evidence is written to, once a Surface has opened one. */
    val logPath: StateFlow<String?>

    /**
     * Arrival time of the newest frame. Unlike [evidence] it survives a reset, so after a
     * disconnect the display can say how long the picture has been gone instead of claiming
     * that no frame was ever received; null until the first frame.
     */
    val lastFrameAtMs: StateFlow<Long?>

    fun attachSurface(surface: Surface, width: Int, height: Int)

    fun detachSurface(surface: Surface)
}

/** The capture path (Phase G, `CameraExecutor.progress`) feeds this; [IdleCaptureProgress] is the no-camera default. */
interface CaptureProgressSource {
    val progress: StateFlow<CaptureProgress>
}

/** Wraps a live progress flow, such as the camera executor's, for the flight display. */
class FlowCaptureProgress(override val progress: StateFlow<CaptureProgress>) : CaptureProgressSource

object IdleCaptureProgress : CaptureProgressSource {
    override val progress: StateFlow<CaptureProgress> = MutableStateFlow(CaptureProgress()).asStateFlow()
}

/** Everything the flight display reads from a flavor beyond the Phase C session. */
interface FpvSession {
    val cameraStream: CameraStream

    val attitude: StateFlow<AircraftAttitude>

    val captureProgress: CaptureProgressSource
}

/** Sessions that can feed the flight display expose their [FpvSession] beside `AircraftSession`. */
interface FpvSessionHost {
    val fpv: FpvSession
}
