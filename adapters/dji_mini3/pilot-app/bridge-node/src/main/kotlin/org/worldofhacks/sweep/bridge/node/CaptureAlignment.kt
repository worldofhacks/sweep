package org.worldofhacks.sweep.bridge.node

import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.roundToLong
import kotlin.math.sin
import org.worldofhacks.sweep.bridge.core.frames.Fields
import org.worldofhacks.sweep.bridge.core.frames.ObservationSubmission
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue

private const val PHONE_CLOCK = "phone_elapsed_realtime_ms"
private const val DJI_PTS_CLOCK = "dji_stream_presentation_ms"
private const val INTRINSIC_ZYX_DEGREES = "intrinsic_zyx_degrees"

data class CaptureAlignmentConfig(
    val id: String,
    val sha256: String,
    val scope: CaptureAlignmentScope,
    val clockMappingId: String,
    val ptsToCapture: AffineClockMapping,
    val gimbalCallback: CallbackBound,
    val bodyAttitudeCallback: CallbackBound,
    val maxExtrinsicsAngleErrorDeg: Double,
    val kinematicCalibration: KinematicCalibration,
) {
    init {
        require(sha256.isSha256()) { "alignment configuration hash must be lowercase SHA-256" }
        require(maxExtrinsicsAngleErrorDeg >= 0.0 && maxExtrinsicsAngleErrorDeg.isFinite()) { "extrinsics angle budget is invalid" }
    }

    companion object {
        const val MAX_CONFIG_BYTES = 16 * 1024

        fun decode(encoded: String, artifactSha256: String): CaptureAlignmentConfig {
            require(encoded.toByteArray(Charsets.UTF_8).size <= MAX_CONFIG_BYTES) { "capture alignment configuration is too large" }
            val value = Json.parse(encoded) as? JsonObject ?: error("capture alignment configuration must be an object")
            exact(value, setOf(
                "v", "enabled", "id", "scope", "capture_clock", "frame_pts_clock", "clock_mapping_id",
                "frame_pts_to_capture", "gimbal_callback", "body_attitude_callback", "max_extrinsics_angle_error_deg",
                "kinematic_calibration",
            ), "capture alignment configuration")
            require(value["v"] == JsonInt(1) && value.boolean("enabled")) { "capture alignment configuration is disabled or unsupported" }
            val captureClock = value.objectOf("capture_clock")
            exact(captureClock, setOf("clock_id", "unit"), "capture clock")
            require(captureClock.string("clock_id") == PHONE_CLOCK && captureClock.string("unit") == "ms") { "capture clock must be phone elapsed milliseconds" }
            val ptsClock = value.objectOf("frame_pts_clock")
            exact(ptsClock, setOf("clock_id", "unit"), "frame PTS clock")
            require(ptsClock.string("clock_id") == DJI_PTS_CLOCK && ptsClock.string("unit") == "ms") { "frame PTS clock is invalid" }
            return CaptureAlignmentConfig(
                id = value.string("id"),
                sha256 = artifactSha256,
                scope = CaptureAlignmentScope.parse(value.objectOf("scope")),
                clockMappingId = value.string("clock_mapping_id"),
                ptsToCapture = AffineClockMapping.parse(value.objectOf("frame_pts_to_capture")),
                gimbalCallback = CallbackBound.parse(value.objectOf("gimbal_callback")),
                bodyAttitudeCallback = CallbackBound.parse(value.objectOf("body_attitude_callback")),
                maxExtrinsicsAngleErrorDeg = value.number("max_extrinsics_angle_error_deg"),
                kinematicCalibration = KinematicCalibration.parse(value.objectOf("kinematic_calibration")),
            )
        }
    }
}

data class CaptureAlignmentScope(
    val session: String,
    val deviceId: Int,
    val connectionEpoch: Int,
    val mapId: String,
    val sourceId: String,
) {
    fun matches(config: NodeConfig, epoch: Int): Boolean =
        session == config.session && deviceId == config.droneId && connectionEpoch == epoch &&
            mapId == config.localizationPins?.mapId && sourceId == "dji-body-camera"

    companion object {
        fun parse(value: JsonObject): CaptureAlignmentScope {
            exact(value, setOf("session", "device_id", "connection_epoch", "map_id", "source_id", "frame", "camera_frame"), "capture alignment scope")
            require(value.string("frame") == "body" && value.string("camera_frame") == "camera") { "capture alignment frames are invalid" }
            return CaptureAlignmentScope(value.string("session"), value.positiveInt("device_id").toInt(), value.positiveInt("connection_epoch").toInt(), value.string("map_id"), value.string("source_id"))
        }
    }
}

data class AffineClockMapping(val offsetMs: Long, val rateNumerator: Long, val rateDenominator: Long, val maxErrorMs: Long) {
    init {
        require(rateNumerator > 0 && rateDenominator > 0 && maxErrorMs >= 0) { "frame PTS mapping is invalid" }
    }

    fun captureMs(ptsMs: Long): Long? {
        val mapped = offsetMs.toDouble() + ptsMs.toDouble() * rateNumerator.toDouble() / rateDenominator.toDouble()
        return mapped.takeIf(Double::isFinite)?.roundToLong()
    }

    companion object {
        fun parse(value: JsonObject): AffineClockMapping {
            exact(value, setOf("offset_ms", "rate_numerator", "rate_denominator", "max_error_ms"), "frame PTS mapping")
            return AffineClockMapping(value.integer("offset_ms"), value.positiveInt("rate_numerator"), value.positiveInt("rate_denominator"), value.nonNegativeInt("max_error_ms"))
        }
    }
}

data class CallbackBound(val maxLatencyMs: Long, val maxOrientationErrorDeg: Double, val angularRateBoundDegS: Double) {
    init {
        require(maxLatencyMs >= 0 && maxOrientationErrorDeg >= 0 && angularRateBoundDegS >= 0 && maxOrientationErrorDeg.isFinite() && angularRateBoundDegS.isFinite()) { "callback bound is invalid" }
    }

    companion object {
        fun parse(value: JsonObject): CallbackBound {
            exact(value, setOf("max_latency_ms", "max_orientation_error_deg", "angular_rate_bound_deg_s"), "callback bound")
            return CallbackBound(value.nonNegativeInt("max_latency_ms"), value.number("max_orientation_error_deg"), value.number("angular_rate_bound_deg_s"))
        }
    }
}

data class KinematicCalibration(val id: String, val sha256: String, val bodyToGimbal: RigidTransform, val gimbalToCamera: RigidTransform) {
    init { require(sha256.isSha256()) { "kinematic calibration hash must be lowercase SHA-256" } }

    companion object {
        fun parse(value: JsonObject): KinematicCalibration {
            exact(value, setOf("id", "sha256", "gimbal_attitude_convention", "body_to_gimbal", "gimbal_to_camera"), "kinematic calibration")
            require(value.string("gimbal_attitude_convention") == INTRINSIC_ZYX_DEGREES) { "gimbal attitude convention is invalid" }
            return KinematicCalibration(value.string("id"), value.string("sha256"), RigidTransform.parse(value.objectOf("body_to_gimbal"), "body", "gimbal"), RigidTransform.parse(value.objectOf("gimbal_to_camera"), "gimbal", "camera"))
        }
    }
}

data class AttitudeSample(val yawDeg: Double, val pitchDeg: Double, val rollDeg: Double, val receiptMs: Long) {
    init { require(listOf(yawDeg, pitchDeg, rollDeg).all(Double::isFinite) && receiptMs >= 0) { "attitude sample is invalid" } }
    fun rotation(): RigidTransform = RigidTransform.eulerIntrinsicZyx(yawDeg, pitchDeg, rollDeg)
}

data class CaptureAlignmentSample(val framePtsMs: Long, val frameReceiptMs: Long, val gimbal: AttitudeSample, val bodyAttitude: AttitudeSample) {
    init { require(framePtsMs >= 0 && frameReceiptMs >= 0) { "frame times are invalid" } }
}

/** Produces an observation only when configured bounds prove both attitude callbacks could describe the frame. */
internal fun canonicalBodyCameraPose(config: NodeConfig, epoch: Int, eventId: String, sample: CaptureAlignmentSample): ObservationSubmission? {
    val alignment = config.captureAlignment ?: return null
    if (!alignment.scope.matches(config, epoch)) return null
    val capture = alignment.ptsToCapture.captureMs(sample.framePtsMs) ?: return null
    if (capture > sample.frameReceiptMs - alignment.ptsToCapture.maxErrorMs || !overlaps(capture, alignment.ptsToCapture.maxErrorMs, sample.gimbal.receiptMs, alignment.gimbalCallback.maxLatencyMs) || !overlaps(capture, alignment.ptsToCapture.maxErrorMs, sample.bodyAttitude.receiptMs, alignment.bodyAttitudeCallback.maxLatencyMs)) return null
    val error = alignment.gimbalCallback.maxOrientationErrorDeg + alignment.bodyAttitudeCallback.maxOrientationErrorDeg +
        maxSeparationMs(capture, alignment.ptsToCapture.maxErrorMs, sample.gimbal.receiptMs, alignment.gimbalCallback.maxLatencyMs) * alignment.gimbalCallback.angularRateBoundDegS / 1_000.0 +
        maxSeparationMs(capture, alignment.ptsToCapture.maxErrorMs, sample.bodyAttitude.receiptMs, alignment.bodyAttitudeCallback.maxLatencyMs) * alignment.bodyAttitudeCallback.angularRateBoundDegS / 1_000.0
    if (error > alignment.maxExtrinsicsAngleErrorDeg) return null
    val pose = alignment.kinematicCalibration.bodyToGimbal.compose(sample.gimbal.rotation()).compose(alignment.kinematicCalibration.gimbalToCamera)
    return ObservationSubmission.parse(Json.json(
        "v" to 1, "type" to "observation", "event_id" to eventId, "session" to config.session,
        "device_id" to config.droneId, "connection_epoch" to epoch, "source_id" to alignment.scope.sourceId,
        "node_type" to "aircraft", "frame" to "body", "confidence" to 1.0,
        "t_capture" to sourceTime(PHONE_CLOCK, capture), "t_source_receipt" to sourceTime(PHONE_CLOCK, sample.frameReceiptMs),
        "clock_mapping_id" to alignment.clockMappingId,
        "payload" to Json.json("kind" to "pose", "pose" to pose.json("body", "camera"), "capture_alignment" to Json.json(
            "v" to 1, "alignment_config_id" to alignment.id, "alignment_config_sha256" to alignment.sha256,
            "kinematic_calibration_id" to alignment.kinematicCalibration.id, "kinematic_calibration_sha256" to alignment.kinematicCalibration.sha256,
            "frame_pts" to sourceTime(DJI_PTS_CLOCK, sample.framePtsMs), "gimbal_receipt" to sourceTime(PHONE_CLOCK, sample.gimbal.receiptMs), "body_attitude_receipt" to sourceTime(PHONE_CLOCK, sample.bodyAttitude.receiptMs),
            "gimbal_attitude" to sample.gimbal.json(), "body_attitude" to sample.bodyAttitude.json(),
            "frame_capture_error_ms" to alignment.ptsToCapture.maxErrorMs, "gimbal_callback_latency_ms" to alignment.gimbalCallback.maxLatencyMs,
            "body_attitude_callback_latency_ms" to alignment.bodyAttitudeCallback.maxLatencyMs,
            "gimbal_callback_orientation_error_deg" to alignment.gimbalCallback.maxOrientationErrorDeg,
            "body_attitude_callback_orientation_error_deg" to alignment.bodyAttitudeCallback.maxOrientationErrorDeg,
            "gimbal_angular_rate_bound_deg_s" to alignment.gimbalCallback.angularRateBoundDegS,
            "body_angular_rate_bound_deg_s" to alignment.bodyAttitudeCallback.angularRateBoundDegS,
            "max_extrinsics_angle_error_deg" to alignment.maxExtrinsicsAngleErrorDeg,
        )),
    ))
}

data class RigidTransform(val x: Double, val y: Double, val z: Double, val qx: Double, val qy: Double, val qz: Double, val qw: Double) {
    fun compose(right: RigidTransform): RigidTransform {
        val rotated = rotate(right.x, right.y, right.z)
        val q = quaternionMultiply(qx, qy, qz, qw, right.qx, right.qy, right.qz, right.qw)
        return RigidTransform(x + rotated.first, y + rotated.second, z + rotated.third, q[0], q[1], q[2], q[3])
    }
    fun json(parent: String, child: String): JsonObject = Json.json("parent_frame" to parent, "child_frame" to child, "x_m" to x, "y_m" to y, "z_m" to z, "qx" to qx, "qy" to qy, "qz" to qz, "qw" to qw)
    private fun rotate(px: Double, py: Double, pz: Double): Triple<Double, Double, Double> {
        val tx = 2.0 * (qy * pz - qz * py); val ty = 2.0 * (qz * px - qx * pz); val tz = 2.0 * (qx * py - qy * px)
        return Triple(px + qw * tx + (qy * tz - qz * ty), py + qw * ty + (qz * tx - qx * tz), pz + qw * tz + (qx * ty - qy * tx))
    }
    companion object {
        fun parse(value: JsonObject, parent: String, child: String): RigidTransform {
            exact(value, setOf("parent_frame", "child_frame", "x_m", "y_m", "z_m", "qx", "qy", "qz", "qw"), "kinematic transform")
            require(value.string("parent_frame") == parent && value.string("child_frame") == child) { "kinematic transform frames are invalid" }
            val q = listOf(value.number("qx"), value.number("qy"), value.number("qz"), value.number("qw")); require(abs(q.sumOf { it * it } - 1.0) <= 1e-6) { "kinematic transform quaternion is not unit length" }
            return RigidTransform(value.number("x_m"), value.number("y_m"), value.number("z_m"), q[0], q[1], q[2], q[3])
        }
        fun eulerIntrinsicZyx(yawDeg: Double, pitchDeg: Double, rollDeg: Double): RigidTransform {
            val yaw = Math.toRadians(yawDeg) / 2.0; val pitch = Math.toRadians(pitchDeg) / 2.0; val roll = Math.toRadians(rollDeg) / 2.0
            val cy = cos(yaw); val sy = sin(yaw); val cp = cos(pitch); val sp = sin(pitch); val cr = cos(roll); val sr = sin(roll)
            return RigidTransform(0.0, 0.0, 0.0, sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy, cr * cp * cy + sr * sp * sy)
        }
    }
}

private fun sourceTime(clockId: String, value: Long): JsonObject = Json.json("clock_id" to clockId, "unit" to "ms", "value" to value)
private fun AttitudeSample.json(): JsonObject = Json.json("yaw_deg" to yawDeg, "pitch_deg" to pitchDeg, "roll_deg" to rollDeg)
private fun overlaps(capture: Long, captureError: Long, receipt: Long, latency: Long): Boolean = capture - captureError <= receipt && receipt - latency <= capture + captureError
private fun maxSeparationMs(capture: Long, captureError: Long, receipt: Long, latency: Long): Long = maxOf(abs((capture - captureError) - receipt), abs((capture + captureError) - (receipt - latency)))
private fun String.isSha256(): Boolean = matches(Regex("[0-9a-f]{64}"))
private fun exact(value: JsonObject, fields: Set<String>, name: String) { require(value.keys == fields) { "$name fields are invalid" } }
private fun JsonObject.objectOf(name: String): JsonObject = this[name] as? JsonObject ?: error("$name must be an object")
private fun JsonObject.string(name: String): String = (this[name] as? JsonString)?.value?.also { require(Fields.isCanonicalPrintable(it, 512)) { "$name is invalid" } } ?: error("$name must be text")
private fun JsonObject.integer(name: String): Long = (this[name] as? JsonInt)?.value ?: error("$name must be an integer")
private fun JsonObject.nonNegativeInt(name: String): Long = integer(name).also { require(it >= 0) { "$name must be non-negative" } }
private fun JsonObject.positiveInt(name: String): Long = nonNegativeInt(name).also { require(it > 0) { "$name must be positive" } }
private fun JsonObject.number(name: String): Double = when (val value = this[name]) { is JsonInt -> value.value.toDouble(); is JsonFloat -> value.value; else -> error("$name must be a number") }.also { require(it.isFinite() && abs(it) <= 1_000_000.0) { "$name is invalid" } }
private fun JsonObject.boolean(name: String): Boolean = (this[name] as? org.worldofhacks.sweep.bridge.core.json.JsonBool)?.value ?: error("$name must be boolean")

interface CaptureAlignmentSampleSource {
    fun drain(): List<CaptureAlignmentSample>
}

class CaptureAlignmentCollector : CaptureAlignmentSampleSource {
    private val lock = Any()
    private var gimbal: AttitudeSample? = null
    private var body: AttitudeSample? = null
    private val frames = ArrayDeque<CaptureAlignmentSample>()

    fun recordGimbal(sample: AttitudeSample) = synchronized(lock) { gimbal = sample }
    fun recordBodyAttitude(sample: AttitudeSample) = synchronized(lock) { body = sample }
    fun recordFrame(ptsMs: Long, receiptMs: Long) = synchronized(lock) {
        val gimbalSample = gimbal ?: return
        val bodySample = body ?: return
        if (frames.size == MAX_PENDING_FRAMES) frames.removeFirst()
        frames.addLast(CaptureAlignmentSample(ptsMs, receiptMs, gimbalSample, bodySample))
    }
    override fun drain(): List<CaptureAlignmentSample> = synchronized(lock) { buildList { while (frames.isNotEmpty()) add(frames.removeFirst()) } }
    private companion object { const val MAX_PENDING_FRAMES = 32 }
}
