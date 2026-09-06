package org.worldofhacks.sweep.bridge.core.localization

import org.worldofhacks.sweep.bridge.core.frames.Fields
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

/**
 * Identifier pins for staged localization diagnostics. They select which signed relay
 * observations the node may retain; they do not enable navigation or affect flight control.
 */
data class LocalizationPins(
    val mapId: String,
    val geometryId: String,
    val cameraCalibrationId: String,
    val bodyExtrinsicsId: String,
) {
    init {
        require(listOf(mapId, geometryId, cameraCalibrationId, bodyExtrinsicsId).all(::validIdentity)) {
            "localization diagnostic identities must be canonical pins"
        }
    }

    private companion object {
        const val MAX_ID_LENGTH = 128

        fun validIdentity(value: String): Boolean = Fields.isCanonicalPrintable(value, MAX_ID_LENGTH)
    }
}

/** Versioned, exact codec for the staged, non-flight localization pins. */
object LocalizationPinsJson {
    const val VERSION = 1

    fun encode(pins: LocalizationPins): String = Json.canonical(
        Json.json(
            "v" to VERSION,
            "map_id" to pins.mapId,
            "geometry_id" to pins.geometryId,
            "camera_calibration_id" to pins.cameraCalibrationId,
            "body_extrinsics_id" to pins.bodyExtrinsicsId,
        ),
    )

    fun parse(text: String): LocalizationPins {
        require(text.length in 2..4_096) { "localization pins must be between 2 and 4096 characters" }
        val value = Json.parse(text)
        val fields = value as? JsonObject ?: throw IllegalArgumentException("localization pins must be a JSON object")
        require(fields.keys == REQUIRED_FIELDS) { "localization pin fields are invalid" }
        require(integer(fields, "v") == VERSION.toLong()) { "unsupported localization pin version" }
        return LocalizationPins(
            mapId = string(fields, "map_id"),
            geometryId = string(fields, "geometry_id"),
            cameraCalibrationId = string(fields, "camera_calibration_id"),
            bodyExtrinsicsId = string(fields, "body_extrinsics_id"),
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
    )
}
