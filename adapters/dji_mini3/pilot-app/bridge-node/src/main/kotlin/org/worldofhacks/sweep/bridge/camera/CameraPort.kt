package org.worldofhacks.sweep.bridge.camera

import java.io.File
import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe
import org.worldofhacks.sweep.bridge.core.frames.MapPoseProvenance
import org.worldofhacks.sweep.bridge.core.frames.MediaFileRecord
import org.worldofhacks.sweep.bridge.core.frames.SuggestedDelta
import org.worldofhacks.sweep.bridge.core.frames.WirePose

/** Null measurements mean the SDK has not reported them. */
data class CameraFacts(
    val cameraConnected: Boolean = false,
    val photoMode: Boolean = false,
    val storageInserted: Boolean = false,
    val storageRemainingBytes: Long? = null,
    val gimbalPitchMinDeg: Double? = null,
    val gimbalPitchMaxDeg: Double? = null,
    /** Published lens value until `measured_hfov_deg` exists; the wire field is `horizontal_fov_deg`. */
    val horizontalFovDeg: Double = CameraProbe().horizontalFovDeg,
    val photoWidthPx: Int = 0,
    val photoHeightPx: Int = 0,
    /** True only when exact output pixel dimensions came from trusted calibration or the fake. */
    val photoDimensionsReported: Boolean = false,
    // Native panoramas yaw outside Virtual Stick control, so toProbe never advertises them.
    val panoramaAdvertised: List<String> = emptyList(),
) {
    fun toProbe(): CameraProbe {
        val defaults = CameraProbe()
        val min = gimbalPitchMinDeg ?: defaults.gimbalPitchMinDeg
        val max = gimbalPitchMaxDeg ?: defaults.gimbalPitchMaxDeg
        val ordered = min < max
        return CameraProbe(
            nativePanoramaModes = emptyList(),
            photoCapture = cameraConnected,
            gimbalPitchMinDeg = if (ordered) min else defaults.gimbalPitchMinDeg,
            gimbalPitchMaxDeg = if (ordered) max else defaults.gimbalPitchMaxDeg,
            horizontalFovDeg = horizontalFovDeg,
            storageRemainingBytes = (storageRemainingBytes ?: 0L).coerceAtLeast(0L),
            mediaRetrieval = cameraConnected,
        )
    }
}
data class CameraFile(
    /** The camera's file index, the key the media manager lists files by. */
    val index: Int,
    val name: String,
    val sizeBytes: Long,
    val createdAtMs: Long?,
)

/** Progress of one file download; every callback may arrive on any thread. */
interface DownloadListener {
    fun progress(bytes: Long, total: Long)

    fun finished()

    fun failed(detail: String)
}

/** Callbacks may arrive on any thread; CameraExecutor serializes them. */
interface CameraPort {
    val facts: StateFlow<CameraFacts>

    /** Ask the hardware for fresh storage and mode facts; [facts] updates before the result. */
    fun refreshFacts(onResult: (PortResult) -> Unit)

    /** The gimbal pitch the SDK last reported, degrees, or null when no gimbal has reported. */
    fun gimbalPitchDeg(): Double?

    fun setGimbalPitch(pitchDeg: Double, onResult: (PortResult) -> Unit)

    /** Still-photo mode with the media manager released, so the shutter can fire. */
    fun enterPhotoMode(onResult: (PortResult) -> Unit)

    fun shootPhoto(onResult: (PortResult) -> Unit)

    /** Files the camera announces after a shutter; one listener at a time, null removes it. */
    fun setFileListener(listener: ((CameraFile) -> Unit)?)

    /** Download one file over the RC link into [target]; the port owns the media-manager session. */
    fun download(file: CameraFile, target: File, listener: DownloadListener)

    /** Release the media manager after a download so the camera can shoot again. */
    fun leaveMediaMode(onResult: (PortResult) -> Unit)
}
data class NodeIdentity(val droneId: Int, val connectionEpoch: Int)
data class CaptureReadinessBody(
    val roomId: String? = null,
    val captureId: String? = null,
    val poseSource: String = "dji_telemetry",
    val poseOk: Boolean = false,
    /** No camera key proves physical clearance; this stays false until an explicit pilot input exists. */
    val clearanceOk: Boolean = false,
    val cameraOk: Boolean = false,
    val storageOk: Boolean = false,
    val motionOk: Boolean = false,
    val imageQualityOk: Boolean = false,
    val coverageMissing: List<Double> = emptyList(),
    val nextHeadingDeg: Double? = null,
    val suggestedDelta: SuggestedDelta? = null,
)

/** The link publishes current readiness on join, on change, and at a bounded refresh interval. */
fun interface CaptureReadinessSource {
    fun current(): CaptureReadinessBody
}

data class CaptureArrivalHold(
    val commandId: String,
    val routeId: String,
    val targetXMm: Long,
    val targetYMm: Long,
    val targetZMm: Long,
    val arrivalHorizontalToleranceMm: Long,
    val arrivalVerticalToleranceMm: Long,
)

fun interface CaptureArrivalHoldSource {
    fun current(): CaptureArrivalHold?
}

data class MapCapturePose(
    val pose: WirePose,
    val provenance: MapPoseProvenance,
)

/** Adds the joined identity and envelope; sends return false when disconnected. */
interface NodeFrameSink {
    fun identity(): NodeIdentity?

    fun sendCaptureReadiness(body: CaptureReadinessBody): Boolean

    fun captureMapPose(hold: CaptureArrivalHold?): MapCapturePose?

    fun sendMediaFile(record: MediaFileRecord): Boolean
}
