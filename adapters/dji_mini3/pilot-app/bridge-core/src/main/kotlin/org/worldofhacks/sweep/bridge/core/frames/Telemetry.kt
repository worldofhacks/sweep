package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

/** Telemetry v1 as the relay's `parse_telemetry` accepts it (transport fields included). */
data class TelemetryFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val drone: Int,
    val connectionEpoch: Int,
    val x: Double,
    val y: Double,
    val z: Double,
    val vx: Double,
    val vy: Double,
    val vz: Double,
    val battery: Double,
    val state: String,
    val link: Double,
    val posQuality: Double,
) {
    init {
        Fields.requireBoundedStateText(state, "state", MAX_STATE_UTF8_BYTES)
    }

    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "drone" to drone,
        "connection_epoch" to connectionEpoch,
        "x" to x,
        "y" to y,
        "z" to z,
        "vx" to vx,
        "vy" to vy,
        "vz" to vz,
        "battery" to battery,
        "state" to state,
        "link" to link,
        "pos_quality" to posQuality,
    )

    companion object {
        const val TYPE = "telemetry"
        const val MAX_STATE_UTF8_BYTES = 128
        private const val CODE = "invalid_telemetry"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone", "connection_epoch",
            "x", "y", "z", "vx", "vy", "vz", "battery", "state", "link", "pos_quality",
        )

        fun parse(json: JsonObject): TelemetryFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            return TelemetryFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                drone = Fields.positiveInt32(json["drone"], "drone", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                x = Fields.finiteNumber(json["x"], "x", CODE),
                y = Fields.finiteNumber(json["y"], "y", CODE),
                z = Fields.finiteNumber(json["z"], "z", CODE),
                vx = Fields.finiteNumber(json["vx"], "vx", CODE),
                vy = Fields.finiteNumber(json["vy"], "vy", CODE),
                vz = Fields.finiteNumber(json["vz"], "vz", CODE),
                battery = Fields.unitInterval(json["battery"], "battery", CODE),
                state = Fields.boundedStateText(
                    json["state"],
                    "state",
                    CODE,
                    maximumUtf8Bytes = MAX_STATE_UTF8_BYTES,
                ),
                link = Fields.unitInterval(json["link"], "link", CODE),
                posQuality = Fields.unitInterval(json["pos_quality"], "pos_quality", CODE),
            )
        }
    }
}
