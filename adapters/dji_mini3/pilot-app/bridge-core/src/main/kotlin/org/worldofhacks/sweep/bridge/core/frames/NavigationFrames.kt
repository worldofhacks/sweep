package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue
import org.worldofhacks.sweep.bridge.core.signing.Signing

data class NavigationSegment(
    val startXMm: Long,
    val startYMm: Long,
    val startZMm: Long,
    val endXMm: Long,
    val endYMm: Long,
    val endZMm: Long,
    val tubeRadiusMm: Long,
) {
    init {
        require(points().all { it in -MAX_ABS_POSITION_MM..MAX_ABS_POSITION_MM }) { "segment position exceeds the navigation envelope" }
        require(start() != end()) { "segment endpoints must differ" }
        require(tubeRadiusMm > 0) { "segment tube radius must be positive" }
    }

    fun toJson(): JsonObject = JsonObject(linkedMapOf(
        "start_x_mm" to JsonInt(startXMm), "start_y_mm" to JsonInt(startYMm), "start_z_mm" to JsonInt(startZMm),
        "end_x_mm" to JsonInt(endXMm), "end_y_mm" to JsonInt(endYMm), "end_z_mm" to JsonInt(endZMm),
        "tube_radius_mm" to JsonInt(tubeRadiusMm),
    ))

    fun start(): List<Long> = listOf(startXMm, startYMm, startZMm)
    fun end(): List<Long> = listOf(endXMm, endYMm, endZMm)
    private fun points(): List<Long> = start() + end()

    companion object {
        const val MAX_ABS_POSITION_MM = 1_000_000L
        private val FIELDS = setOf("start_x_mm", "start_y_mm", "start_z_mm", "end_x_mm", "end_y_mm", "end_z_mm", "tube_radius_mm")

        fun parse(value: JsonValue, code: String): NavigationSegment {
            val json = Fields.obj(value, "segments item", code)
            Fields.exact(json, FIELDS, code)
            return try {
                NavigationSegment(
                    Fields.integer(json["start_x_mm"], "start_x_mm", code),
                    Fields.integer(json["start_y_mm"], "start_y_mm", code),
                    Fields.integer(json["start_z_mm"], "start_z_mm", code),
                    Fields.integer(json["end_x_mm"], "end_x_mm", code),
                    Fields.integer(json["end_y_mm"], "end_y_mm", code),
                    Fields.integer(json["end_z_mm"], "end_z_mm", code),
                    Fields.positiveInt(json["tube_radius_mm"], "tube_radius_mm", code),
                )
            } catch (error: IllegalArgumentException) {
                throw ContractError(code, error.message ?: "invalid navigation segment")
            }
        }
    }
}

data class NavigationRouteAuthorization(
    val t: Long,
    val expiresAtMs: Long,
    val eventId: String,
    val session: String,
    val deviceId: Int,
    val connectionEpoch: Int,
    val commandId: String,
    val routeId: String,
    val seq: Long,
    val positionFrame: String,
    val clockLeaseId: String,
    val maxClockErrorMs: Long,
    val navigationConfigId: String,
    val navigationConfigSha256: String,
    val mapVersion: String,
    val mapSha256: String,
    val geometrySha256: String,
    val cameraCalibrationSha256: String,
    val bodyExtrinsicsSha256: String,
    val worldTransformSha256: String,
    val controlSourceIds: List<String>,
    val segments: List<NavigationSegment>,
    val maxSpeedMmS: Long,
    val maxAccelerationMmS2: Long,
    val maxDecelerationMmS2: Long,
    val maxPositionUncertaintyMm: Long,
    val maxCrossTrackMm: Long,
    val arrivalHorizontalToleranceMm: Long,
    val arrivalVerticalToleranceMm: Long,
    val poseFreshnessMs: Long,
    val trackingTimeoutMs: Long,
    val flightApproved: Boolean,
    val signature: String,
    val arrivalHoldTimeoutMs: Long = 0,
) {
    init {
        require(t >= 0 && deviceId > 0 && connectionEpoch > 0 && seq > 0) { "route identity is invalid" }
        require(expiresAtMs > t) { "route authorization must expire after issuance" }
        require(validIdentity(eventId) && validIdentity(commandId) && validIdentity(routeId) && validIdentity(clockLeaseId)) { "route identities are invalid" }
        require(validSession(session) && positionFrame == POSITION_FRAME) { "route frame or session is invalid" }
        require(provenanceIds().all(::validIdentity) && controlSourceIds.all(::validIdentity)) { "route provenance pins are invalid" }
        require(controlSourceIds.isNotEmpty() && controlSourceIds == controlSourceIds.distinct().sorted()) { "route source identities must be sorted and unique" }
        require(segments.size == 1) { "route authorization must carry exactly one current segment" }
        require(maxClockErrorMs >= 0 && limits().all { it > 0 } && arrivalHoldTimeoutMs in 0..180_000) { "route limits must be nonnegative/positive" }
        require(flightApproved) { "route must be explicitly flight approved" }
        require(Signing.isWellFormed(signature)) { "signature must be lowercase HMAC-SHA256 hex" }
    }

    fun unsignedEvent(): JsonObject = JsonObject(linkedMapOf(
        "v" to JsonInt(Fields.PROTOCOL_VERSION), "type" to JsonString(TYPE), "t" to JsonInt(t),
        "expires_at_ms" to JsonInt(expiresAtMs), "event_id" to JsonString(eventId), "session" to JsonString(session),
        "device_id" to JsonInt(deviceId.toLong()), "connection_epoch" to JsonInt(connectionEpoch.toLong()),
        "command_id" to JsonString(commandId), "route_id" to JsonString(routeId), "seq" to JsonInt(seq),
        "position_frame" to JsonString(positionFrame), "clock_lease_id" to JsonString(clockLeaseId), "max_clock_error_ms" to JsonInt(maxClockErrorMs),
        "navigation_config_id" to JsonString(navigationConfigId), "navigation_config_sha256" to JsonString(navigationConfigSha256),
        "map_version" to JsonString(mapVersion), "map_sha256" to JsonString(mapSha256), "geometry_sha256" to JsonString(geometrySha256),
        "camera_calibration_sha256" to JsonString(cameraCalibrationSha256), "body_extrinsics_sha256" to JsonString(bodyExtrinsicsSha256),
        "world_transform_sha256" to JsonString(worldTransformSha256), "control_source_ids" to JsonArray(controlSourceIds.map(::JsonString)),
        "segments" to JsonArray(segments.map(NavigationSegment::toJson)), "max_speed_mm_s" to JsonInt(maxSpeedMmS),
        "max_acceleration_mm_s2" to JsonInt(maxAccelerationMmS2), "max_deceleration_mm_s2" to JsonInt(maxDecelerationMmS2),
        "max_position_uncertainty_mm" to JsonInt(maxPositionUncertaintyMm), "max_cross_track_mm" to JsonInt(maxCrossTrackMm),
        "arrival_horizontal_tolerance_mm" to JsonInt(arrivalHorizontalToleranceMm), "arrival_vertical_tolerance_mm" to JsonInt(arrivalVerticalToleranceMm),
        "pose_freshness_ms" to JsonInt(poseFreshnessMs), "tracking_timeout_ms" to JsonInt(trackingTimeoutMs),
        "flight_approved" to JsonBool(flightApproved),
    )).let { event -> if (arrivalHoldTimeoutMs > 0) event.with("arrival_hold_timeout_ms", JsonInt(arrivalHoldTimeoutMs)) else event }

    fun verifies(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)
    fun target(): List<Long> = segments.last().end()
    fun provenanceIds(): List<String> = listOf(navigationConfigId, navigationConfigSha256, mapVersion, mapSha256, geometrySha256, cameraCalibrationSha256, bodyExtrinsicsSha256, worldTransformSha256)
    private fun limits(): List<Long> = listOf(maxSpeedMmS, maxAccelerationMmS2, maxDecelerationMmS2, maxPositionUncertaintyMm, maxCrossTrackMm, arrivalHorizontalToleranceMm, arrivalVerticalToleranceMm, poseFreshnessMs, trackingTimeoutMs)

    companion object {
        const val TYPE = "navigation_route_authorization"
        const val POSITION_FRAME = "map_enu"
        private const val CODE = "invalid_navigation_route_authorization"
        private val FIELDS = setOf(
            "v", "type", "t", "expires_at_ms", "event_id", "session", "device_id", "connection_epoch", "command_id", "route_id", "seq",
            "position_frame", "clock_lease_id", "max_clock_error_ms", "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256", "geometry_sha256",
            "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256", "control_source_ids", "segments", "max_speed_mm_s", "max_acceleration_mm_s2",
            "max_deceleration_mm_s2", "max_position_uncertainty_mm", "max_cross_track_mm", "arrival_horizontal_tolerance_mm", "arrival_vertical_tolerance_mm",
            "pose_freshness_ms", "tracking_timeout_ms", "flight_approved", "signature",
        )
        private const val ARRIVAL_HOLD_TIMEOUT = "arrival_hold_timeout_ms"

        fun parse(json: JsonObject): NavigationRouteAuthorization {
            Fields.exact(json, if (ARRIVAL_HOLD_TIMEOUT in json.keys) FIELDS + ARRIVAL_HOLD_TIMEOUT else FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            Fields.exactString(json["position_frame"], "position_frame", POSITION_FRAME, CODE)
            if (json["flight_approved"] != JsonBool(true)) throw ContractError(CODE, "flight_approved must be true")
            val segments = (json["segments"] as? JsonArray)?.items ?: throw ContractError(CODE, "segments must be a list")
            if (segments.size != 1) throw ContractError(CODE, "segments must contain exactly one current segment")
            val sources = Fields.stringList(json["control_source_ids"], "control_source_ids", CODE, allowEmpty = false)
            try {
                return NavigationRouteAuthorization(
                    Fields.nonNegativeInt(json["t"], "t", CODE), Fields.positiveInt(json["expires_at_ms"], "expires_at_ms", CODE),
                    identity(json, "event_id", CODE), session(json, CODE), Fields.positiveInt32(json["device_id"], "device_id", CODE), Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                    identity(json, "command_id", CODE), identity(json, "route_id", CODE), Fields.positiveInt(json["seq"], "seq", CODE), POSITION_FRAME,
                    identity(json, "clock_lease_id", CODE), Fields.nonNegativeInt(json["max_clock_error_ms"], "max_clock_error_ms", CODE),
                    identity(json, "navigation_config_id", CODE), identity(json, "navigation_config_sha256", CODE), identity(json, "map_version", CODE), identity(json, "map_sha256", CODE),
                    identity(json, "geometry_sha256", CODE), identity(json, "camera_calibration_sha256", CODE), identity(json, "body_extrinsics_sha256", CODE), identity(json, "world_transform_sha256", CODE),
                    sources, segments.map { NavigationSegment.parse(it, CODE) }, Fields.positiveInt(json["max_speed_mm_s"], "max_speed_mm_s", CODE),
                    Fields.positiveInt(json["max_acceleration_mm_s2"], "max_acceleration_mm_s2", CODE), Fields.positiveInt(json["max_deceleration_mm_s2"], "max_deceleration_mm_s2", CODE),
                    Fields.positiveInt(json["max_position_uncertainty_mm"], "max_position_uncertainty_mm", CODE), Fields.positiveInt(json["max_cross_track_mm"], "max_cross_track_mm", CODE),
                    Fields.positiveInt(json["arrival_horizontal_tolerance_mm"], "arrival_horizontal_tolerance_mm", CODE), Fields.positiveInt(json["arrival_vertical_tolerance_mm"], "arrival_vertical_tolerance_mm", CODE),
                    Fields.positiveInt(json["pose_freshness_ms"], "pose_freshness_ms", CODE), Fields.positiveInt(json["tracking_timeout_ms"], "tracking_timeout_ms", CODE), true,
                    signature(json, CODE), Fields.nonNegativeInt(json[ARRIVAL_HOLD_TIMEOUT] ?: JsonInt(0), ARRIVAL_HOLD_TIMEOUT, CODE),
                )
            } catch (error: IllegalArgumentException) {
                throw ContractError(CODE, error.message ?: "navigation route authorization values are invalid")
            }
        }
    }
}

data class NavigationPose(
    val t: Long,
    val eventId: String,
    val session: String,
    val deviceId: Int,
    val connectionEpoch: Int,
    val commandId: String,
    val routeId: String,
    val seq: Long,
    val positionFrame: String,
    val clockLeaseId: String,
    val navigationConfigId: String,
    val navigationConfigSha256: String,
    val mapVersion: String,
    val mapSha256: String,
    val geometrySha256: String,
    val cameraCalibrationSha256: String,
    val bodyExtrinsicsSha256: String,
    val worldTransformSha256: String,
    val controlSourceIds: List<String>,
    val poseTimeMs: Long?,
    val fixTimeMs: Long?,
    val xMm: Long?,
    val yMm: Long?,
    val zMm: Long?,
    val positionUncertaintyMm: Long?,
    val status: Status,
    val flightApproved: Boolean,
    val signature: String,
) {
    enum class Status { READY, HOLD, LAND }

    init {
        require(t >= 0 && deviceId > 0 && connectionEpoch > 0 && seq > 0) { "pose identity is invalid" }
        require(validIdentity(eventId) && validIdentity(commandId) && validIdentity(routeId) && validIdentity(clockLeaseId)) { "pose identities are invalid" }
        require(validSession(session) && positionFrame == NavigationRouteAuthorization.POSITION_FRAME) { "pose frame or session is invalid" }
        require(provenanceIds().all(::validIdentity) && controlSourceIds.all(::validIdentity)) { "pose provenance pins are invalid" }
        require(controlSourceIds.isNotEmpty() && controlSourceIds == controlSourceIds.distinct().sorted()) { "pose source identities must be sorted and unique" }
        val observation = listOf(poseTimeMs, fixTimeMs, xMm, yMm, zMm, positionUncertaintyMm)
        require(if (status == Status.READY) observation.all { it != null } else observation.all { it == null }) { "navigation pose observation does not match status" }
        if (status == Status.READY) {
            require(t >= poseTimeMs!! && poseTimeMs >= fixTimeMs!!) { "pose timestamps are invalid" }
            require(listOf(xMm!!, yMm!!, zMm!!).all { it in -NavigationSegment.MAX_ABS_POSITION_MM..NavigationSegment.MAX_ABS_POSITION_MM }) { "pose position exceeds the navigation envelope" }
            require(positionUncertaintyMm!! > 0) { "position uncertainty must be positive" }
        }
        require(flightApproved) { "pose must be explicitly flight approved" }
        require(Signing.isWellFormed(signature)) { "signature must be lowercase HMAC-SHA256 hex" }
    }

    fun unsignedEvent(): JsonObject = JsonObject(linkedMapOf(
        "v" to JsonInt(Fields.PROTOCOL_VERSION), "type" to JsonString(TYPE), "t" to JsonInt(t), "event_id" to JsonString(eventId), "session" to JsonString(session),
        "device_id" to JsonInt(deviceId.toLong()), "connection_epoch" to JsonInt(connectionEpoch.toLong()), "command_id" to JsonString(commandId), "route_id" to JsonString(routeId), "seq" to JsonInt(seq),
        "position_frame" to JsonString(positionFrame), "clock_lease_id" to JsonString(clockLeaseId), "navigation_config_id" to JsonString(navigationConfigId),
        "navigation_config_sha256" to JsonString(navigationConfigSha256), "map_version" to JsonString(mapVersion), "map_sha256" to JsonString(mapSha256), "geometry_sha256" to JsonString(geometrySha256),
        "camera_calibration_sha256" to JsonString(cameraCalibrationSha256), "body_extrinsics_sha256" to JsonString(bodyExtrinsicsSha256), "world_transform_sha256" to JsonString(worldTransformSha256),
        "control_source_ids" to JsonArray(controlSourceIds.map(::JsonString)), "pose_time_ms" to nullableInt(poseTimeMs), "fix_time_ms" to nullableInt(fixTimeMs),
        "x_mm" to nullableInt(xMm), "y_mm" to nullableInt(yMm), "z_mm" to nullableInt(zMm), "position_uncertainty_mm" to nullableInt(positionUncertaintyMm),
        "status" to JsonString(status.name.lowercase()), "flight_approved" to JsonBool(flightApproved),
    ))

    fun verifies(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)
    fun provenanceIds(): List<String> = listOf(navigationConfigId, navigationConfigSha256, mapVersion, mapSha256, geometrySha256, cameraCalibrationSha256, bodyExtrinsicsSha256, worldTransformSha256)

    companion object {
        const val TYPE = "navigation_pose"
        private const val CODE = "invalid_navigation_pose"
        private val FIELDS = setOf(
            "v", "type", "t", "event_id", "session", "device_id", "connection_epoch", "command_id", "route_id", "seq", "position_frame", "clock_lease_id",
            "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256", "geometry_sha256", "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256",
            "control_source_ids", "pose_time_ms", "fix_time_ms", "x_mm", "y_mm", "z_mm", "position_uncertainty_mm", "status", "flight_approved", "signature",
        )

        fun parse(json: JsonObject): NavigationPose {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            Fields.exactString(json["position_frame"], "position_frame", NavigationRouteAuthorization.POSITION_FRAME, CODE)
            if (json["flight_approved"] != JsonBool(true)) throw ContractError(CODE, "flight_approved must be true")
            val status = when (Fields.nonEmptyString(json["status"], "status", CODE)) {
                "ready" -> Status.READY
                "hold" -> Status.HOLD
                "land" -> Status.LAND
                else -> throw ContractError(CODE, "status must be ready, hold, or land")
            }
            try {
                return NavigationPose(
                    Fields.nonNegativeInt(json["t"], "t", CODE), identity(json, "event_id", CODE), session(json, CODE), Fields.positiveInt32(json["device_id"], "device_id", CODE),
                    Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE), identity(json, "command_id", CODE), identity(json, "route_id", CODE), Fields.positiveInt(json["seq"], "seq", CODE),
                    NavigationRouteAuthorization.POSITION_FRAME, identity(json, "clock_lease_id", CODE), identity(json, "navigation_config_id", CODE), identity(json, "navigation_config_sha256", CODE),
                    identity(json, "map_version", CODE), identity(json, "map_sha256", CODE), identity(json, "geometry_sha256", CODE), identity(json, "camera_calibration_sha256", CODE),
                    identity(json, "body_extrinsics_sha256", CODE), identity(json, "world_transform_sha256", CODE), Fields.stringList(json["control_source_ids"], "control_source_ids", CODE, allowEmpty = false),
                    nullableNonNegativeInt(json["pose_time_ms"], "pose_time_ms"), nullableNonNegativeInt(json["fix_time_ms"], "fix_time_ms"), nullableInteger(json["x_mm"], "x_mm"),
                    nullableInteger(json["y_mm"], "y_mm"), nullableInteger(json["z_mm"], "z_mm"), nullableNonNegativeInt(json["position_uncertainty_mm"], "position_uncertainty_mm"), status, true, signature(json, CODE),
                )
            } catch (error: IllegalArgumentException) {
                throw ContractError(CODE, error.message ?: "navigation pose values are invalid")
            }
        }

        private fun nullableInt(value: Long?): JsonValue = value?.let(::JsonInt) ?: JsonNull
        private fun nullableNonNegativeInt(value: JsonValue?, field: String): Long? = if (value == JsonNull) null else Fields.nonNegativeInt(value, field, CODE)
        private fun nullableInteger(value: JsonValue?, field: String): Long? = if (value == JsonNull) null else Fields.integer(value, field, CODE)
    }
}

private fun signature(json: JsonObject, code: String): String {
    val signature = Fields.nonEmptyString(json["signature"], "signature", code)
    if (!Signing.isWellFormed(signature)) throw ContractError(code, "signature must be lowercase HMAC-SHA256 hex")
    return signature
}

private fun identity(json: JsonObject, field: String, code: String): String {
    val value = Fields.nonEmptyString(json[field], field, code)
    if (!validIdentity(value)) throw ContractError(code, "$field must be a canonical identifier")
    return value
}

private fun session(json: JsonObject, code: String): String {
    val value = Fields.nonEmptyString(json["session"], "session", code)
    if (!validSession(value)) throw ContractError(code, "session must be canonical printable text")
    return value
}

private fun validIdentity(value: String): Boolean = Fields.isCanonicalPrintable(value, 128)
private fun validSession(value: String): Boolean = Fields.isCanonicalPrintable(value, Fields.MAX_STRING)
