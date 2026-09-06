package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue
import org.worldofhacks.sweep.bridge.core.signing.Signing

data class NavigationRouteAuthorization(
    val t: Long,
    val expiresAtMs: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val commandId: String,
    val routeId: String,
    val seq: Long,
    val navigationConfigId: String,
    val mapId: String,
    val geometryId: String,
    val cameraCalibrationId: String,
    val bodyExtrinsicsId: String,
    val startXMm: Long,
    val startYMm: Long,
    val startZMm: Long,
    val targetXMm: Long,
    val targetYMm: Long,
    val targetZMm: Long,
    val maxSpeedMmS: Long,
    val horizontalToleranceMm: Long,
    val verticalToleranceMm: Long,
    val maxPositionUncertaintyMm: Long,
    val tubeRadiusMm: Long,
    val signature: String,
) {
    init {
        require(t >= 0 && droneId > 0 && connectionEpoch > 0 && seq > 0) { "route identity is invalid" }
        require(expiresAtMs > t) { "route authorization must expire after issuance" }
        require(validIdentity(eventId) && validIdentity(commandId) && validIdentity(routeId)) { "route identities are invalid" }
        require(validSession(session)) { "session is invalid" }
        require(provenanceIds().all(::validIdentity)) { "route provenance pins are invalid" }
        require(positions().all { it in -MAX_ABS_POSITION_MM..MAX_ABS_POSITION_MM }) { "route positions exceed the navigation envelope" }
        require(startPosition() != targetPosition()) { "route target must differ from start" }
        require(listOf(maxSpeedMmS, horizontalToleranceMm, verticalToleranceMm, maxPositionUncertaintyMm, tubeRadiusMm).all { it > 0 }) {
            "route limits must be positive"
        }
        require(Signing.isWellFormed(signature)) { "signature must be lowercase HMAC-SHA256 hex" }
    }

    fun unsignedEvent(): JsonObject = JsonObject(linkedMapOf(
        "v" to JsonInt(Fields.PROTOCOL_VERSION), "type" to JsonString(TYPE), "t" to JsonInt(t),
        "expires_at_ms" to JsonInt(expiresAtMs), "event_id" to JsonString(eventId), "session" to JsonString(session),
        "drone_id" to JsonInt(droneId.toLong()), "connection_epoch" to JsonInt(connectionEpoch.toLong()),
        "command_id" to JsonString(commandId), "route_id" to JsonString(routeId), "seq" to JsonInt(seq),
        "navigation_config_id" to JsonString(navigationConfigId), "map_id" to JsonString(mapId),
        "geometry_id" to JsonString(geometryId), "camera_calibration_id" to JsonString(cameraCalibrationId),
        "body_extrinsics_id" to JsonString(bodyExtrinsicsId), "start_x_mm" to JsonInt(startXMm),
        "start_y_mm" to JsonInt(startYMm), "start_z_mm" to JsonInt(startZMm), "target_x_mm" to JsonInt(targetXMm),
        "target_y_mm" to JsonInt(targetYMm), "target_z_mm" to JsonInt(targetZMm),
        "max_speed_mm_s" to JsonInt(maxSpeedMmS), "horizontal_tolerance_mm" to JsonInt(horizontalToleranceMm),
        "vertical_tolerance_mm" to JsonInt(verticalToleranceMm),
        "max_position_uncertainty_mm" to JsonInt(maxPositionUncertaintyMm), "tube_radius_mm" to JsonInt(tubeRadiusMm),
        "flight_approved" to JsonBool(true),
    ))

    fun verifies(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)

    private fun provenanceIds() = listOf(navigationConfigId, mapId, geometryId, cameraCalibrationId, bodyExtrinsicsId)

    private fun positions() = startPosition() + targetPosition()

    private fun startPosition() = listOf(startXMm, startYMm, startZMm)

    private fun targetPosition() = listOf(targetXMm, targetYMm, targetZMm)

    companion object {
        const val TYPE = "navigation_route_authorization"
        internal const val MAX_ABS_POSITION_MM = 1_000_000L
        private const val CODE = "invalid_navigation_route_authorization"
        private val FIELDS = setOf(
            "v", "type", "t", "expires_at_ms", "event_id", "session", "drone_id", "connection_epoch",
            "command_id", "route_id", "seq", "navigation_config_id", "map_id", "geometry_id",
            "camera_calibration_id", "body_extrinsics_id", "start_x_mm", "start_y_mm", "start_z_mm",
            "target_x_mm", "target_y_mm", "target_z_mm", "max_speed_mm_s", "horizontal_tolerance_mm",
            "vertical_tolerance_mm", "max_position_uncertainty_mm", "tube_radius_mm", "flight_approved", "signature",
        )

        fun parse(json: JsonObject): NavigationRouteAuthorization {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            if (json["flight_approved"] != JsonBool(true)) throw ContractError(CODE, "flight_approved must be true")
            val signature = signature(json, CODE)
            try {
                return NavigationRouteAuthorization(
                    t = Fields.nonNegativeInt(json["t"], "t", CODE),
                    expiresAtMs = Fields.positiveInt(json["expires_at_ms"], "expires_at_ms", CODE),
                    eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                    session = Fields.nonEmptyString(json["session"], "session", CODE),
                    droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                    connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                    commandId = Fields.nonEmptyString(json["command_id"], "command_id", CODE),
                    routeId = Fields.nonEmptyString(json["route_id"], "route_id", CODE),
                    seq = Fields.positiveInt(json["seq"], "seq", CODE),
                    navigationConfigId = Fields.nonEmptyString(json["navigation_config_id"], "navigation_config_id", CODE),
                    mapId = Fields.nonEmptyString(json["map_id"], "map_id", CODE),
                    geometryId = Fields.nonEmptyString(json["geometry_id"], "geometry_id", CODE),
                    cameraCalibrationId = Fields.nonEmptyString(json["camera_calibration_id"], "camera_calibration_id", CODE),
                    bodyExtrinsicsId = Fields.nonEmptyString(json["body_extrinsics_id"], "body_extrinsics_id", CODE),
                    startXMm = Fields.integer(json["start_x_mm"], "start_x_mm", CODE),
                    startYMm = Fields.integer(json["start_y_mm"], "start_y_mm", CODE),
                    startZMm = Fields.integer(json["start_z_mm"], "start_z_mm", CODE),
                    targetXMm = Fields.integer(json["target_x_mm"], "target_x_mm", CODE),
                    targetYMm = Fields.integer(json["target_y_mm"], "target_y_mm", CODE),
                    targetZMm = Fields.integer(json["target_z_mm"], "target_z_mm", CODE),
                    maxSpeedMmS = Fields.positiveInt(json["max_speed_mm_s"], "max_speed_mm_s", CODE),
                    horizontalToleranceMm = Fields.positiveInt(json["horizontal_tolerance_mm"], "horizontal_tolerance_mm", CODE),
                    verticalToleranceMm = Fields.positiveInt(json["vertical_tolerance_mm"], "vertical_tolerance_mm", CODE),
                    maxPositionUncertaintyMm = Fields.positiveInt(json["max_position_uncertainty_mm"], "max_position_uncertainty_mm", CODE),
                    tubeRadiusMm = Fields.positiveInt(json["tube_radius_mm"], "tube_radius_mm", CODE),
                    signature = signature,
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
    val droneId: Int,
    val connectionEpoch: Int,
    val commandId: String,
    val routeId: String,
    val seq: Long,
    val navigationConfigId: String,
    val mapId: String,
    val geometryId: String,
    val cameraCalibrationId: String,
    val bodyExtrinsicsId: String,
    val poseTimeMs: Long?,
    val fixTimeMs: Long?,
    val xMm: Long?,
    val yMm: Long?,
    val zMm: Long?,
    val positionUncertaintyMm: Long?,
    val status: Status,
    val signature: String,
) {
    enum class Status { READY, HOLD, LAND }

    init {
        require(t >= 0 && droneId > 0 && connectionEpoch > 0 && seq > 0) { "route identity is invalid" }
        require(validIdentity(eventId) && validIdentity(commandId) && validIdentity(routeId)) { "route identities are invalid" }
        require(validSession(session)) { "session is invalid" }
        require(listOf(navigationConfigId, mapId, geometryId, cameraCalibrationId, bodyExtrinsicsId).all(::validIdentity)) {
            "pose provenance pins are invalid"
        }
        val observation = listOf(poseTimeMs, fixTimeMs, xMm, yMm, zMm, positionUncertaintyMm)
        require(
            if (status == Status.READY) observation.all { it != null } else observation.all { it == null },
        ) { "navigation pose observation does not match status" }
        if (status == Status.READY) {
            require(t >= poseTimeMs!! && poseTimeMs >= fixTimeMs!!) { "pose timestamps are invalid" }
            require(listOf(xMm!!, yMm!!, zMm!!).all { it in -NavigationRouteAuthorization.MAX_ABS_POSITION_MM..NavigationRouteAuthorization.MAX_ABS_POSITION_MM }) {
                "pose position exceeds the navigation envelope"
            }
            require(positionUncertaintyMm!! > 0) { "position uncertainty must be positive" }
        }
        require(Signing.isWellFormed(signature)) { "signature must be lowercase HMAC-SHA256 hex" }
    }

    fun unsignedEvent(): JsonObject = JsonObject(linkedMapOf(
        "v" to JsonInt(Fields.PROTOCOL_VERSION), "type" to JsonString(TYPE), "t" to JsonInt(t),
        "event_id" to JsonString(eventId), "session" to JsonString(session), "drone_id" to JsonInt(droneId.toLong()),
        "connection_epoch" to JsonInt(connectionEpoch.toLong()), "command_id" to JsonString(commandId),
        "route_id" to JsonString(routeId), "seq" to JsonInt(seq), "navigation_config_id" to JsonString(navigationConfigId),
        "map_id" to JsonString(mapId), "geometry_id" to JsonString(geometryId),
        "camera_calibration_id" to JsonString(cameraCalibrationId), "body_extrinsics_id" to JsonString(bodyExtrinsicsId),
        "pose_time_ms" to nullableInt(poseTimeMs), "fix_time_ms" to nullableInt(fixTimeMs), "x_mm" to nullableInt(xMm),
        "y_mm" to nullableInt(yMm), "z_mm" to nullableInt(zMm), "position_uncertainty_mm" to nullableInt(positionUncertaintyMm),
        "status" to JsonString(status.name.lowercase()), "flight_approved" to JsonBool(true),
    ))

    fun verifies(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)

    companion object {
        const val TYPE = "navigation_pose"
        private const val CODE = "invalid_navigation_pose"
        private val FIELDS = setOf(
            "v", "type", "t", "event_id", "session", "drone_id", "connection_epoch", "command_id", "route_id",
            "seq", "navigation_config_id", "map_id", "geometry_id", "camera_calibration_id", "body_extrinsics_id",
            "pose_time_ms", "fix_time_ms", "x_mm", "y_mm", "z_mm", "position_uncertainty_mm", "status",
            "flight_approved", "signature",
        )

        fun parse(json: JsonObject): NavigationPose {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            if (json["flight_approved"] != JsonBool(true)) throw ContractError(CODE, "flight_approved must be true")
            val status = when (Fields.nonEmptyString(json["status"], "status", CODE)) {
                "ready" -> Status.READY
                "hold" -> Status.HOLD
                "land" -> Status.LAND
                else -> throw ContractError(CODE, "status must be ready, hold, or land")
            }
            val signature = signature(json, CODE)
            try {
                return NavigationPose(
                    t = Fields.nonNegativeInt(json["t"], "t", CODE),
                    eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                    session = Fields.nonEmptyString(json["session"], "session", CODE),
                    droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                    connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                    commandId = Fields.nonEmptyString(json["command_id"], "command_id", CODE),
                    routeId = Fields.nonEmptyString(json["route_id"], "route_id", CODE),
                    seq = Fields.positiveInt(json["seq"], "seq", CODE),
                    navigationConfigId = Fields.nonEmptyString(json["navigation_config_id"], "navigation_config_id", CODE),
                    mapId = Fields.nonEmptyString(json["map_id"], "map_id", CODE),
                    geometryId = Fields.nonEmptyString(json["geometry_id"], "geometry_id", CODE),
                    cameraCalibrationId = Fields.nonEmptyString(json["camera_calibration_id"], "camera_calibration_id", CODE),
                    bodyExtrinsicsId = Fields.nonEmptyString(json["body_extrinsics_id"], "body_extrinsics_id", CODE),
                    poseTimeMs = nullableNonNegativeInt(json["pose_time_ms"], "pose_time_ms"),
                    fixTimeMs = nullableNonNegativeInt(json["fix_time_ms"], "fix_time_ms"),
                    xMm = nullableInteger(json["x_mm"], "x_mm"),
                    yMm = nullableInteger(json["y_mm"], "y_mm"),
                    zMm = nullableInteger(json["z_mm"], "z_mm"),
                    positionUncertaintyMm = nullableNonNegativeInt(json["position_uncertainty_mm"], "position_uncertainty_mm"),
                    status = status,
                    signature = signature,
                )
            } catch (error: IllegalArgumentException) {
                throw ContractError(CODE, error.message ?: "navigation pose values are invalid")
            }
        }

        private fun nullableInt(value: Long?): JsonValue = value?.let(::JsonInt) ?: JsonNull

        private fun nullableNonNegativeInt(value: JsonValue?, field: String): Long? =
            if (value == JsonNull) null else Fields.nonNegativeInt(value, field, CODE)

        private fun nullableInteger(value: JsonValue?, field: String): Long? =
            if (value == JsonNull) null else Fields.integer(value, field, CODE)
    }
}

private fun signature(json: JsonObject, code: String): String {
    val signature = Fields.nonEmptyString(json["signature"], "signature", code)
    if (!Signing.isWellFormed(signature)) throw ContractError(code, "signature must be lowercase HMAC-SHA256 hex")
    return signature
}

private fun validIdentity(value: String): Boolean = Fields.isCanonicalPrintable(value, 128)

private fun validSession(value: String): Boolean = Fields.isCanonicalPrintable(value, Fields.MAX_STRING)
