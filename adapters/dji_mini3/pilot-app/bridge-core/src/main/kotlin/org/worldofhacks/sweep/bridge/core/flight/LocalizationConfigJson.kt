package org.worldofhacks.sweep.bridge.core.flight

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

/** Versioned, complete localization configuration exchanged through the device setup screen. */
object LocalizationConfigJson {
    const val VERSION = 1

    fun encode(config: LocalizationConfig): String = Json.canonical(
        Json.json(
            "v" to VERSION,
            "map_id" to config.mapId,
            "geometry_id" to config.geometryId,
            "camera_calibration_id" to config.cameraCalibrationId,
            "body_extrinsics_id" to config.bodyExtrinsicsId,
            "fix_freshness_ms" to config.fixFreshnessMs,
            "pose_freshness_ms" to config.poseFreshnessMs,
            "tracking_tube_mm" to config.trackingTubeMm,
            "target_tolerance_mm" to config.targetToleranceMm,
            "settled_hold_ms" to config.settledHoldMs,
            "tag_loss_land_after_ms" to config.tagLossLandAfterMs,
        ),
    )

    fun parse(text: String): LocalizationConfig {
        require(text.length in 2..4_096) { "localization config must be between 2 and 4096 characters" }
        val value = Json.parse(text)
        val fields = value as? JsonObject ?: throw IllegalArgumentException("localization config must be a JSON object")
        require(fields.keys == REQUIRED_FIELDS) { "localization config fields are invalid" }
        require(integer(fields, "v") == VERSION.toLong()) { "unsupported localization config version" }
        return LocalizationConfig(
            mapId = string(fields, "map_id"),
            geometryId = string(fields, "geometry_id"),
            cameraCalibrationId = string(fields, "camera_calibration_id"),
            bodyExtrinsicsId = string(fields, "body_extrinsics_id"),
            fixFreshnessMs = integer(fields, "fix_freshness_ms"),
            poseFreshnessMs = integer(fields, "pose_freshness_ms"),
            trackingTubeMm = integer(fields, "tracking_tube_mm"),
            targetToleranceMm = integer(fields, "target_tolerance_mm"),
            settledHoldMs = integer(fields, "settled_hold_ms"),
            tagLossLandAfterMs = integer(fields, "tag_loss_land_after_ms"),
        )
    }

    private fun string(fields: JsonObject, name: String): String =
        (fields[name] as? JsonString)?.value ?: throw IllegalArgumentException("$name must be a string")

    private fun integer(fields: JsonObject, name: String): Long =
        (fields[name] as? JsonInt)?.value ?: throw IllegalArgumentException("$name must be an integer")

    private val REQUIRED_FIELDS = setOf(
        "v",
        "map_id",
        "geometry_id",
        "camera_calibration_id",
        "body_extrinsics_id",
        "fix_freshness_ms",
        "pose_freshness_ms",
        "tracking_tube_mm",
        "target_tolerance_mm",
        "settled_hold_ms",
        "tag_loss_land_after_ms",
    )
}
