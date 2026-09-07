package org.worldofhacks.sweep.bridge.node

import org.worldofhacks.sweep.bridge.core.frames.ObservationSubmission
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

data class ObservationSourceConfig(
    val telemetrySourceId: String,
    val frameId: String,
    val clockId: String,
) {
    init {
        require(telemetrySourceId == "dji-telemetry" && frameId == "dji_enu" && clockId == "phone_snapshot_wall_ms") {
            "observation source must identify the DJI ENU snapshot and its receipt clock"
        }
    }

    companion object {
        const val MAX_CONFIG_BYTES = 4096

        fun decode(encoded: String): ObservationSourceConfig {
            require(encoded.toByteArray(Charsets.UTF_8).size <= MAX_CONFIG_BYTES) { "observation source configuration is too large" }
            val value = Json.parse(encoded) as? JsonObject ?: error("observation source configuration must be an object")
            require(value.keys == setOf("v", "telemetry_source_id", "frame_id", "clock_id") && value["v"] == JsonInt(1)) {
                "observation source configuration fields are invalid"
            }
            fun text(name: String): String = (value[name] as? JsonString)?.value ?: error("$name must be text")
            return ObservationSourceConfig(text("telemetry_source_id"), text("frame_id"), text("clock_id"))
        }

    }
}

/** Receipt time records when the publisher sampled the snapshot; SDK capture time is unavailable. */
internal fun canonicalTelemetry(
    config: NodeConfig,
    snapshot: AircraftSnapshot,
    epoch: Int,
    eventId: String,
    receiptMs: Long,
): ObservationSubmission? {
    val source = config.observationSource ?: return null
    val numbers = listOf(snapshot.x, snapshot.y, snapshot.z, snapshot.vx, snapshot.vy, snapshot.vz, snapshot.battery, snapshot.link, snapshot.posQuality)
    if (!snapshot.aircraftConnected || numbers.any { !it.isFinite() } ||
        listOf(snapshot.battery, snapshot.link, snapshot.posQuality).any { it !in 0.0..1.0 }) return null
    return ObservationSubmission.parse(Json.json(
        "v" to 1,
        "type" to "observation",
        "event_id" to eventId,
        "session" to config.session,
        "device_id" to config.droneId,
        "connection_epoch" to epoch,
        "source_id" to source.telemetrySourceId,
        "node_type" to "aircraft",
        "frame" to source.frameId,
        "confidence" to snapshot.posQuality,
        "t_capture" to null,
        "t_source_receipt" to Json.json("clock_id" to source.clockId, "unit" to "ms", "value" to receiptMs),
        "clock_mapping_id" to null,
        "payload" to Json.json(
            "kind" to "telemetry",
            "position" to Json.json("frame" to source.frameId, "x_m" to snapshot.x, "y_m" to snapshot.y, "z_m" to snapshot.z),
            "velocity" to Json.json("frame" to source.frameId, "x_m_s" to snapshot.vx, "y_m_s" to snapshot.vy, "z_m_s" to snapshot.vz),
            "battery" to snapshot.battery,
            "link" to snapshot.link,
            "pos_quality" to snapshot.posQuality,
            "state" to snapshot.state,
        ),
    ))
}
