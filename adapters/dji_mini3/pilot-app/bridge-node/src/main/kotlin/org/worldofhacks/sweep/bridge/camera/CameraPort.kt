package org.worldofhacks.sweep.bridge.camera

import java.io.File
import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe
import org.worldofhacks.sweep.bridge.core.frames.MediaFileRecord
import org.worldofhacks.sweep.bridge.core.frames.SuggestedDelta

/**
 * What the flavor's camera hardware reports. Every field is what the SDK (or the fake)
 * said, never a guess: a null range or storage figure means the key has not answered.
 */
data class CameraFacts(
    val cameraConnected: Boolean = false,
    /** The camera is in a still-photo mode (`PHOTO_NORMAL` flat mode or camera mode). */
    val photoMode: Boolean = false,
    val storageInserted: Boolean = false,
    val storageRemainingBytes: Long? = null,
    val gimbalPitchMinDeg: Double? = null,
    val gimbalPitchMaxDeg: Double? = null,
    /** Published lens value until `measured_hfov_deg` exists; the wire field is `horizontal_fov_deg`. */
    val horizontalFovDeg: Double = CameraProbe().horizontalFovDeg,
    val photoWidthPx: Int = 4000,
    val photoHeightPx: Int = 3000,
    /**
     * Panorama modes the camera itself advertises, kept for the record. The node never
     * drives one: a native panorama yaws the aircraft under the flight controller, outside
     * the Virtual Stick loop and the arbiter's pose lock, so `native_panorama_modes` on the
     * wire stays empty and `capture_panorama` answers `camera_unsupported`.
     */
    val panoramaAdvertised: List<String> = emptyList(),
) {
    /** The `capabilities` fields the link sends; the wire requires an ordered pitch range. */
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

/** One file on the aircraft's storage as the camera announced it. */
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

/**
 * The camera-facing side of the capture path. The probe flavor implements it on DJI
 * `KeyManager` camera and gimbal keys plus `IMediaManager`; the fake flavor and the JVM
 * tests implement it on [FakeCameraPort]. Results may arrive on any thread; the
 * [CameraExecutor] serialises its own work on one thread and waits for them there.
 */
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

/** The joined connection identity the node stamps into media records. */
data class NodeIdentity(val droneId: Int, val connectionEpoch: Int)

/** The node-authored `capture_readiness` gates the camera path computes; the link adds the envelope. */
data class CaptureReadinessBody(
    val roomId: String? = null,
    val captureId: String? = null,
    val cameraOk: Boolean = false,
    val storageOk: Boolean = false,
    val motionOk: Boolean = true,
    val imageQualityOk: Boolean = true,
    val coverageMissing: List<Double> = emptyList(),
    val nextHeadingDeg: Double? = null,
    val suggestedDelta: SuggestedDelta? = null,
)

/** Where the camera path reads its readiness from; the link sends a frame on join and on every change. */
fun interface CaptureReadinessSource {
    fun current(): CaptureReadinessBody
}

/**
 * Node-authored frames the camera path asks the relay link to send. The link stamps the
 * envelope (`t`, `event_id`, `session`) and the connection identity, and drops a frame when
 * the node is not joined; the return value says whether it went out.
 */
interface NodeFrameSink {
    fun identity(): NodeIdentity?

    fun sendCaptureReadiness(body: CaptureReadinessBody): Boolean

    fun sendMediaFile(record: MediaFileRecord): Boolean
}
