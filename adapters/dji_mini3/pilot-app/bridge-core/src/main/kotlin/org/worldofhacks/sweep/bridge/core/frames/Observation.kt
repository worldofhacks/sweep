package org.worldofhacks.sweep.bridge.core.frames

import kotlin.math.abs
import kotlin.math.sqrt
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue

class ObservationSubmission private constructor(private val validated: ObservationFrame) {
    fun toEvent(): JsonObject = validated.toEvent().without("t_ingest")

    companion object {
        fun parse(json: JsonObject): ObservationSubmission {
            if ("t_ingest" in json.keys) {
                throw ContractError("invalid_observation", "submission cannot set relay ingest time")
            }
            return ObservationSubmission(ObservationFrame.parse(json.with("t_ingest", JsonInt(0))))
        }

        fun decode(encoded: String): ObservationSubmission {
            if (encoded.toByteArray(Charsets.UTF_8).size > ObservationFrame.MAX_CANONICAL_BYTES) {
                throw ContractError("observation_too_large", "encoded submission exceeds the v1 byte ceiling")
            }
            return parse(Json.parse(encoded) as? JsonObject ?: throw ContractError("invalid_observation", "submission must be an object"))
        }
    }
}

/** Shared v1 observation envelope. The relay adds t_ingest after authenticating a submission. */
@ConsistentCopyVisibility
data class ObservationFrame private constructor(
    val eventId: String,
    val session: String,
    val deviceId: Int,
    val connectionEpoch: Long,
    val sourceId: String,
    val nodeType: String,
    val frame: String,
    val confidence: Double,
    val capture: SourceTime?,
    val sourceReceipt: SourceTime,
    val clockMappingId: String?,
    val payload: JsonObject,
    val ingestMs: Long,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to 1,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "device_id" to deviceId,
        "connection_epoch" to connectionEpoch,
        "source_id" to sourceId,
        "node_type" to nodeType,
        "frame" to frame,
        "confidence" to confidence,
        "t_capture" to capture?.toJson(),
        "t_source_receipt" to sourceReceipt.toJson(),
        "clock_mapping_id" to clockMappingId,
        "payload" to payload,
        "t_ingest" to ingestMs,
    )

    companion object {
        const val TYPE = "observation"
        const val MAX_CANONICAL_BYTES = 64 * 1024
        private const val CODE = "invalid_observation"
        private val FIELDS = setOf(
            "v", "type", "event_id", "session", "device_id", "connection_epoch", "source_id",
            "node_type", "frame", "confidence", "t_capture", "t_source_receipt", "clock_mapping_id",
            "payload", "t_ingest",
        )

        fun parse(json: JsonObject): ObservationFrame {
            Fields.exact(json, FIELDS, CODE)
            if (json["v"] != JsonInt(1) || json["type"] != JsonString(TYPE)) {
                throw ContractError(CODE, "observation v/type is invalid")
            }
            val captureValue = json["t_capture"]
            val capture = if (captureValue == JsonNull) null else SourceTime.parse(captureValue)
            val receipt = SourceTime.parse(json["t_source_receipt"])
            if (capture != null && (capture.clockId != receipt.clockId || capture.unit != receipt.unit || capture.value > receipt.value)) {
                throw ContractError("invalid_time_order", "capture time exceeds source receipt time")
            }
            val result = ObservationFrame(
                eventId = text(json["event_id"], "event_id"),
                session = text(json["session"], "session", 512),
                deviceId = positiveInt32(json["device_id"], "device_id"),
                connectionEpoch = positiveInt(json["connection_epoch"], "connection_epoch"),
                sourceId = text(json["source_id"], "source_id"),
                nodeType = enumValue(json["node_type"], "node_type", setOf("aircraft", "ground")),
                frame = text(json["frame"], "frame"),
                confidence = unitInterval(json["confidence"], "confidence"),
                capture = capture,
                sourceReceipt = receipt,
                clockMappingId = nullableText(json["clock_mapping_id"], "clock_mapping_id"),
                payload = JsonObject(emptyMap()),
                ingestMs = nonNegativeInt(json["t_ingest"], "t_ingest"),
            )
            val parsedPayload = ObservationPayload.parse(json["payload"], result.frame)
            val complete = result.copy(payload = parsedPayload)
            if (Json.canonicalBytes(complete.toEvent()).size > MAX_CANONICAL_BYTES) {
                throw ContractError("observation_too_large", "encoded observation exceeds the v1 byte ceiling")
            }
            return complete
        }

        fun decode(encoded: String): ObservationFrame {
            if (encoded.toByteArray(Charsets.UTF_8).size > MAX_CANONICAL_BYTES) {
                throw ContractError("observation_too_large", "encoded observation exceeds the v1 byte ceiling")
            }
            return parse(Json.parse(encoded) as? JsonObject ?: throw ContractError(CODE, "observation must be an object"))
        }
    }
}

@ConsistentCopyVisibility
data class SourceTime private constructor(val clockId: String, val unit: String, val value: Long) {
    fun toJson(): JsonObject = Json.json("clock_id" to clockId, "unit" to unit, "value" to value)

    companion object {
        private const val CODE = "invalid_observation"
        private val FIELDS = setOf("clock_id", "unit", "value")

        fun parse(value: JsonValue?): SourceTime {
            val json = value as? JsonObject ?: throw ContractError(CODE, "source timestamp must be an object")
            Fields.exact(json, FIELDS, CODE)
            return SourceTime(
                clockId = text(json["clock_id"], "clock_id"),
                unit = enumValue(json["unit"], "source clock unit", setOf("ms", "ns")),
                value = nonNegativeInt(json["value"], "source timestamp"),
            )
        }
    }
}

private object ObservationPayload {
    private const val CODE = "invalid_payload"

    fun parse(value: JsonValue?, frame: String): JsonObject {
        val json = value as? JsonObject ?: throw ContractError(CODE, "payload must be an object")
        return when (json["kind"]) {
            JsonString("telemetry") -> telemetry(json, frame)
            JsonString("pose") -> pose(json, frame)
            JsonString("range_scan") -> rangeScan(json, frame)
            JsonString("camera_frame") -> cameraFrame(json)
            JsonString("tag_observation") -> tagObservation(json, frame)
            JsonString("status") -> status(json)
            else -> throw ContractError(CODE, "payload kind is unknown")
        }
    }

    private fun telemetry(json: JsonObject, frame: String): JsonObject {
        exact(json, setOf("kind", "position", "velocity", "battery", "link", "pos_quality", "state"), "telemetry")
        val position = vector(json["position"])
        val velocity = json["velocity"] as? JsonObject ?: throw ContractError(CODE, "framed velocity must be an object")
        exact(velocity, setOf("frame", "x_m_s", "y_m_s", "z_m_s"), "framed velocity")
        if (position["frame"] != JsonString(frame) || text(velocity["frame"], "velocity frame") != frame) {
            throw ContractError("payload_frame_mismatch", "telemetry vectors must use the envelope frame")
        }
        number(velocity["x_m_s"], "x_m_s")
        number(velocity["y_m_s"], "y_m_s")
        number(velocity["z_m_s"], "z_m_s")
        unitInterval(json["battery"], "battery")
        unitInterval(json["link"], "link")
        unitInterval(json["pos_quality"], "pos_quality")
        text(json["state"], "state", 512)
        return copyObject(json)
    }

    private fun pose(json: JsonObject, frame: String): JsonObject {
        val fields = if (json["capture_alignment"] != null) {
            setOf("kind", "pose", "capture_alignment")
        } else {
            setOf("kind", "pose")
        }
        exact(json, fields, "pose payload")
        val pose = pose(json["pose"])
        if (pose["parent_frame"] != JsonString(frame)) {
            throw ContractError("payload_frame_mismatch", "pose parent frame must equal the envelope frame")
        }
        if (json["capture_alignment"] != null) {
            if (frame != "body" || pose["child_frame"] != JsonString("camera")) {
                throw ContractError("payload_frame_mismatch", "capture-aligned pose must be body-to-camera")
            }
            captureAlignment(json["capture_alignment"])
        }
        return copyObject(json)
    }

    private fun captureAlignment(value: JsonValue?) {
        val json = value as? JsonObject ?: throw ContractError(CODE, "capture alignment must be an object")
        exact(
            json,
            setOf(
                "v", "alignment_config_id", "alignment_config_sha256", "kinematic_calibration_id",
                "kinematic_calibration_sha256", "frame_pts", "gimbal_receipt", "body_attitude_receipt",
                "gimbal_attitude", "body_attitude", "frame_capture_error_ms", "gimbal_callback_latency_ms",
                "body_attitude_callback_latency_ms", "gimbal_callback_orientation_error_deg",
                "body_attitude_callback_orientation_error_deg", "gimbal_angular_rate_bound_deg_s",
                "body_angular_rate_bound_deg_s", "max_extrinsics_angle_error_deg",
            ),
            "pose capture alignment",
        )
        if (json["v"] != JsonInt(1)) throw ContractError(CODE, "capture alignment version is invalid")
        text(json["alignment_config_id"], "alignment_config_id")
        sha256(json["alignment_config_sha256"], "alignment_config_sha256")
        text(json["kinematic_calibration_id"], "kinematic_calibration_id")
        sha256(json["kinematic_calibration_sha256"], "kinematic_calibration_sha256")
        val pts = SourceTime.parse(json["frame_pts"])
        val gimbalReceipt = SourceTime.parse(json["gimbal_receipt"])
        val bodyReceipt = SourceTime.parse(json["body_attitude_receipt"])
        if (pts.clockId != "dji_stream_presentation_ms" || pts.unit != "ms" ||
            gimbalReceipt.clockId != "phone_elapsed_realtime_ms" || gimbalReceipt.unit != "ms" ||
            bodyReceipt.clockId != "phone_elapsed_realtime_ms" || bodyReceipt.unit != "ms") {
            throw ContractError(CODE, "capture alignment clocks are invalid")
        }
        attitude(json["gimbal_attitude"], "gimbal attitude")
        attitude(json["body_attitude"], "body attitude")
        for (name in setOf(
            "frame_capture_error_ms", "gimbal_callback_latency_ms", "body_attitude_callback_latency_ms",
            "gimbal_callback_orientation_error_deg", "body_attitude_callback_orientation_error_deg",
            "gimbal_angular_rate_bound_deg_s", "body_angular_rate_bound_deg_s", "max_extrinsics_angle_error_deg",
        )) {
            if (number(json[name], name) < 0.0) throw ContractError(CODE, "$name must be non-negative")
        }
    }

    private fun attitude(value: JsonValue?, name: String) {
        val json = value as? JsonObject ?: throw ContractError(CODE, "$name must be an object")
        exact(json, setOf("yaw_deg", "pitch_deg", "roll_deg"), name)
        number(json["yaw_deg"], "$name yaw", 360.0)
        number(json["pitch_deg"], "$name pitch", 360.0)
        number(json["roll_deg"], "$name roll", 360.0)
    }

    private fun sha256(value: JsonValue?, field: String) {
        val digest = value as? JsonString
        if (digest == null || !digest.value.matches(Regex("[0-9a-f]{64}"))) {
            throw ContractError(CODE, "$field must be lowercase SHA-256")
        }
    }

    private fun rangeScan(json: JsonObject, frame: String): JsonObject {
        exact(json, setOf("kind", "sensor_pose", "angle_min_rad", "angle_increment_rad", "range_min_m", "range_max_m", "ranges_m", "mount_id"), "range scan")
        val sensorPose = pose(json["sensor_pose"])
        if (sensorPose["child_frame"] != JsonString(frame)) {
            throw ContractError("payload_frame_mismatch", "range scan frame must equal sensor pose child frame")
        }
        val minimum = number(json["range_min_m"], "range_min_m")
        val maximum = number(json["range_max_m"], "range_max_m")
        val increment = number(json["angle_increment_rad"], "angle_increment_rad")
        if (minimum < 0 || minimum >= maximum || increment <= 0) {
            throw ContractError(CODE, "range scan bounds or increment are invalid")
        }
        number(json["angle_min_rad"], "angle_min_rad")
        val ranges = json["ranges_m"] as? JsonArray ?: throw ContractError(CODE, "range scan samples must be a list")
        if (ranges.items.isEmpty() || ranges.items.size > 720) throw ContractError(CODE, "range scan sample count exceeds its bounded envelope")
        for (range in ranges.items) {
            if (range == JsonNull) continue
            val distance = number(range, "range sample")
            if (distance < minimum || distance > maximum) throw ContractError(CODE, "range sample lies outside declared sensor bounds")
        }
        text(json["mount_id"], "mount_id", 512)
        return copyObject(json)
    }

    private fun cameraFrame(json: JsonObject): JsonObject {
        exact(json, setOf("kind", "image_id", "sha256", "width_px", "height_px", "calibration_id"), "camera frame")
        text(json["image_id"], "image_id", 512)
        val digest = json["sha256"] as? JsonString
        if (digest == null || !digest.value.matches(Regex("[0-9a-f]{64}"))) throw ContractError(CODE, "camera frame sha256 must be lowercase hexadecimal")
        boundedInt(json["width_px"], "width_px", 1, 16_384)
        boundedInt(json["height_px"], "height_px", 1, 16_384)
        text(json["calibration_id"], "calibration_id", 512)
        return copyObject(json)
    }

    private fun tagObservation(json: JsonObject, frame: String): JsonObject {
        exact(json, setOf("kind", "family", "tag_id", "image_id", "pose_accepted", "tag_pose", "covariance_m2", "reason", "size_m", "corners_px", "pixel_frame", "reprojection_rms_px"), "tag observation")
        if (json["family"] != JsonString("tag36h11")) throw ContractError(CODE, "tag family must be tag36h11")
        val tagId = boundedInt(json["tag_id"], "tag_id", 0, 586)
        text(json["image_id"], "image_id", 512)
        val accepted = (json["pose_accepted"] as? JsonBool)?.value ?: throw ContractError(CODE, "tag pose_accepted must be boolean")
        val reason = enumValue(json["reason"], "tag reason", setOf("pose", "ambiguous", "unconfigured_tag", "duplicate_tag", "tag_too_small", "reprojection_or_cheirality"))
        if ((reason == "pose") != accepted) throw ContractError(CODE, "tag reason must describe whether pose was accepted")
        val tagPose = if (json["tag_pose"] == JsonNull) null else pose(json["tag_pose"])
        if ((tagPose != null) != accepted) throw ContractError(CODE, "tag_pose must be present exactly when pose is accepted")
        if (tagPose != null && (tagPose["parent_frame"] != JsonString(frame) || tagPose["child_frame"] != JsonString("tag:$tagId"))) {
            throw ContractError("payload_frame_mismatch", "tag pose must be camera-to-declared-tag")
        }
        val size = if (json["size_m"] == JsonNull) null else number(json["size_m"], "tag size")
        if ((size == null && accepted) || (size != null && size <= 0)) throw ContractError(CODE, "tag size is invalid")
        val covariance = json["covariance_m2"]
        if (covariance != JsonNull) {
            if (tagPose == null) throw ContractError(CODE, "tag covariance requires an accepted pose")
            positiveSemidefinite(covariance)
        }
        corners(json["corners_px"])
        enumValue(json["pixel_frame"], "tag pixel_frame", setOf("camera", "rectified_camera"))
        if (json["reprojection_rms_px"] != JsonNull && number(json["reprojection_rms_px"], "reprojection_rms_px", 16_384.0) < 0) {
            throw ContractError(CODE, "reprojection_rms_px must be non-negative")
        }
        return copyObject(json)
    }

    private fun status(json: JsonObject): JsonObject {
        exact(json, setOf("kind", "code", "detail", "capabilities"), "status")
        text(json["code"], "status code")
        text(json["detail"], "status detail", 512)
        val capabilities = json["capabilities"] as? JsonArray ?: throw ContractError(CODE, "status capabilities must be a list")
        if (capabilities.items.size > 32) throw ContractError(CODE, "status capabilities exceed their bounded envelope")
        capabilities.items.forEach { text(it, "capability", 64) }
        return copyObject(json)
    }

    private fun vector(value: JsonValue?): JsonObject {
        val json = value as? JsonObject ?: throw ContractError(CODE, "framed vector must be an object")
        exact(json, setOf("frame", "x_m", "y_m", "z_m"), "framed vector")
        text(json["frame"], "vector frame")
        number(json["x_m"], "x_m")
        number(json["y_m"], "y_m")
        number(json["z_m"], "z_m")
        return json
    }

    private fun pose(value: JsonValue?): JsonObject {
        val json = value as? JsonObject ?: throw ContractError(CODE, "framed pose must be an object")
        exact(json, setOf("parent_frame", "child_frame", "x_m", "y_m", "z_m", "qx", "qy", "qz", "qw"), "framed pose")
        val parent = text(json["parent_frame"], "parent_frame")
        val child = text(json["child_frame"], "child_frame")
        if (parent == child) throw ContractError(CODE, "pose parent and child frames must differ")
        val qx = number(json["qx"], "qx")
        val qy = number(json["qy"], "qy")
        val qz = number(json["qz"], "qz")
        val qw = number(json["qw"], "qw")
        number(json["x_m"], "x_m")
        number(json["y_m"], "y_m")
        number(json["z_m"], "z_m")
        if (abs(sqrt(qx * qx + qy * qy + qz * qz + qw * qw) - 1) > 1e-6) {
            throw ContractError(CODE, "pose quaternion must have unit length")
        }
        return json
    }

    private fun corners(value: JsonValue?) {
        val corners = value as? JsonArray ?: throw ContractError(CODE, "tag corners must contain four pixel pairs")
        if (corners.items.size != 4) throw ContractError(CODE, "tag corners must contain four pixel pairs")
        for (corner in corners.items) {
            val pair = corner as? JsonArray ?: throw ContractError(CODE, "tag corners must contain four pixel pairs")
            if (pair.items.size != 2) throw ContractError(CODE, "tag corners must contain four pixel pairs")
            number(pair.items[0], "tag corner", 16_384.0)
            number(pair.items[1], "tag corner", 16_384.0)
        }
    }

    private fun positiveSemidefinite(value: JsonValue?) {
        val matrix = value as? JsonArray ?: throw ContractError(CODE, "tag covariance must contain nine entries")
        if (matrix.items.size != 9) throw ContractError(CODE, "tag covariance must contain nine entries")
        val values = matrix.items.map { number(it, "tag covariance") }
        val scale = values.maxOf { abs(it) }
        val normalized = if (scale == 0.0) values else values.map { it / scale }
        val a = normalized[0]
        val b = normalized[1]
        val c = normalized[2]
        val d = normalized[4]
        val e = normalized[5]
        val f = normalized[8]
        val tolerance = 1e-9
        if (abs(b - normalized[3]) > tolerance || abs(c - normalized[6]) > tolerance || abs(e - normalized[7]) > tolerance || a < -tolerance || d < -tolerance || f < -tolerance || a * d - b * b < -tolerance || a * f - c * c < -tolerance || d * f - e * e < -tolerance || a * d * f + 2 * b * c * e - a * e * e - d * c * c - f * b * b < -tolerance) {
            throw ContractError(CODE, "tag covariance must be symmetric positive semidefinite")
        }
    }

    private fun exact(json: JsonObject, fields: Set<String>, name: String) {
        if (json.keys != fields) throw ContractError(CODE, "$name fields do not match the v1 contract")
    }

    private fun deepCopy(value: JsonValue): JsonValue = when (value) {
        is JsonArray -> JsonArray(value.items.map(::deepCopy))
        is JsonObject -> JsonObject(value.fields.mapValues { (_, item) -> deepCopy(item) })
        else -> value
    }

    private fun copyObject(value: JsonObject): JsonObject = deepCopy(value) as JsonObject
}

private fun text(value: JsonValue?, field: String, maximum: Int = 128): String {
    val result = (value as? JsonString)?.value
    if (result == null || !Fields.isCanonicalPrintable(result, maximum)) {
        throw ContractError("invalid_observation", "$field must be canonical printable text")
    }
    return result
}

private fun nullableText(value: JsonValue?, field: String): String? =
    if (value == JsonNull) null else text(value, field)

private fun enumValue(value: JsonValue?, field: String, values: Set<String>): String {
    val result = value as? JsonString
    if (result == null || result.value !in values) throw ContractError("invalid_observation", "$field is unknown")
    return result.value
}

private fun nonNegativeInt(value: JsonValue?, field: String): Long {
    val result = (value as? JsonInt)?.value
    if (result == null || result < 0) throw ContractError("invalid_observation", "$field must be a non-negative integer")
    return result
}

private fun positiveInt(value: JsonValue?, field: String): Long {
    val result = nonNegativeInt(value, field)
    if (result == 0L) throw ContractError("invalid_observation", "$field must be a positive integer")
    return result
}

private fun positiveInt32(value: JsonValue?, field: String): Int = boundedInt(value, field, 1, Int.MAX_VALUE)

private fun boundedInt(value: JsonValue?, field: String, minimum: Int, maximum: Int): Int {
    val result = (value as? JsonInt)?.value
    if (result == null || result !in minimum.toLong()..maximum.toLong()) throw ContractError("invalid_observation", "$field is outside its bounded integer range")
    return result.toInt()
}

private fun number(value: JsonValue?, field: String, maximum: Double = 1_000_000.0): Double {
    val result = when (value) {
        is JsonInt -> value.value.toDouble()
        is JsonFloat -> value.value
        else -> null
    }
    if (result == null || !result.isFinite() || abs(result) > maximum) throw ContractError("invalid_observation", "$field must be a bounded finite number")
    return result
}

private fun unitInterval(value: JsonValue?, field: String): Double {
    val result = number(value, field, 1.0)
    if (result < 0) throw ContractError("invalid_observation", "$field must be in [0, 1]")
    return result
}
