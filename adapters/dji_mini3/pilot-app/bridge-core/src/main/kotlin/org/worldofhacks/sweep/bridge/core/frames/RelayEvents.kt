package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue
import org.worldofhacks.sweep.bridge.core.signing.Signing

/**
 * Relay-authored events a node consumes. These parsers are deliberately lenient about extra
 * fields: the relay owns these shapes and may add projection fields without a node release.
 * Only the fields the node acts on are required.
 */

/**
 * The relay's per-node control lease. Unlike state projections, this frame is exact,
 * signed with the adapter credential, bound to the current connection identity, and
 * monotonically sequenced so fan-out echoes and captured frames cannot feed the deadman.
 */
data class ControlHeartbeat(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val rosterVersion: Int,
    val seq: Long,
    val signature: String,
) {
    fun unsignedEvent(): JsonObject = JsonObject(
        linkedMapOf(
            "v" to JsonInt(1),
            "t" to JsonInt(t),
            "type" to JsonString(TYPE),
            "event_id" to JsonString(eventId),
            "session" to JsonString(session),
            "source" to JsonString(SOURCE),
            "drone_id" to JsonInt(droneId.toLong()),
            "connection_epoch" to JsonInt(connectionEpoch.toLong()),
            "roster_version" to JsonInt(rosterVersion.toLong()),
            "seq" to JsonInt(seq),
        ),
    )

    fun verifies(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)

    companion object {
        const val TYPE = "control_heartbeat"
        const val SOURCE = "relay"
        private const val CODE = "invalid_control_heartbeat"
        private val FIELDS = setOf(
            "v",
            "t",
            "type",
            "event_id",
            "session",
            "source",
            "drone_id",
            "connection_epoch",
            "roster_version",
            "seq",
            "signature",
        )

        fun parse(json: JsonObject): ControlHeartbeat {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            Fields.exactString(json["source"], "source", SOURCE, CODE)
            val signature = Fields.nonEmptyString(json["signature"], "signature", CODE)
            if (!Signing.isWellFormed(signature)) throw ContractError(CODE, "signature must be lowercase HMAC-SHA256 hex")
            return ControlHeartbeat(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                seq = Fields.positiveInt(json["seq"], "seq", CODE),
                signature = signature,
            )
        }
    }
}

/** `relay.state.MembershipTransition.to_event`: the relay's answer to join and readiness. */
data class MembershipEvent(
    val t: Long,
    val eventId: String,
    val session: String,
    val action: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val membership: String,
    val rosterVersion: Int,
    val reason: String?,
    val readinessReasons: List<String>,
    val adapterId: String?,
    val capabilities: List<String>,
    val provenance: String,
) {
    companion object {
        const val TYPE = "membership"
        private const val CODE = "invalid_membership_event"

        fun parse(json: JsonObject): MembershipEvent {
            Fields.envelope(json, TYPE, CODE)
            return MembershipEvent(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                action = Fields.nonEmptyString(json["action"], "action", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                membership = Fields.nonEmptyString(json["membership"], "membership", CODE),
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                reason = Fields.nullableString(json["reason"], "reason", CODE),
                readinessReasons = Fields.stringList(json["readiness_reasons"], "readiness_reasons", CODE, allowEmpty = true),
                adapterId = Fields.nullableString(json["adapter_id"], "adapter_id", CODE),
                capabilities = Fields.stringList(json["capabilities"], "capabilities", CODE, allowEmpty = true),
                provenance = Fields.nonEmptyString(json["provenance"], "provenance", CODE),
            )
        }
    }
}

/** The per-aircraft slice of the 10 Hz `state` projection a node reads back about itself. */
data class DroneProjection(
    val droneId: Int,
    val connectionEpoch: Int?,
    val membership: String,
    val readinessReasons: List<String>,
    val flightState: String?,
    val battery: Double?,
    val link: Double?,
    val controlAuthority: Boolean?,
) {
    companion object {
        fun parse(json: JsonObject, code: String): DroneProjection = DroneProjection(
            droneId = Fields.positiveInt32(json["drone_id"], "drone_id", code),
            connectionEpoch = optionalInt(json["connection_epoch"], "connection_epoch", code),
            membership = Fields.nonEmptyString(json["membership"], "membership", code),
            readinessReasons = Fields.stringList(json["readiness_reasons"], "readiness_reasons", code, allowEmpty = true),
            flightState = (json["flight_state"] as? JsonString)?.value,
            battery = optionalNumber(json["battery"]),
            link = optionalNumber(json["link"]),
            controlAuthority = (json["control_authority"] as? JsonBool)?.value,
        )

        private fun optionalInt(value: JsonValue?, field: String, code: String): Int? =
            if (value == null || value == JsonNull) null else Fields.positiveInt32(value, field, code)

        private fun optionalNumber(value: JsonValue?): Double? = when (value) {
            is JsonInt -> value.value.toDouble()
            is JsonFloat -> value.value
            else -> null
        }
    }
}

/** The parts of the relay's `state` event a node consumes: roster, stop flag, its own row. */
data class StateEvent(
    val t: Long,
    val eventId: String,
    val session: String,
    val rosterVersion: Int,
    val armed: Boolean,
    val estop: Boolean,
    val drones: List<DroneProjection>,
) {
    fun drone(droneId: Int): DroneProjection? = drones.firstOrNull { it.droneId == droneId }

    companion object {
        const val TYPE = "state"
        private const val CODE = "invalid_state"

        fun parse(json: JsonObject): StateEvent {
            Fields.envelope(json, TYPE, CODE)
            val drones = json["drones"] as? JsonArray ?: throw ContractError(CODE, "drones must be a list")
            return StateEvent(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                armed = Fields.boolean(json["armed"], "armed", CODE),
                estop = Fields.boolean(json["estop"], "estop", CODE),
                drones = drones.items.map { DroneProjection.parse(Fields.obj(it, "drones", CODE), CODE) },
            )
        }
    }
}

/** `relay.contracts.refusal_event`: context fields are present as null when they do not apply. */
data class RefusalEvent(
    val t: Long,
    val eventId: String,
    val session: String,
    val intentId: String?,
    val commandId: String?,
    val droneId: Int?,
    val connectionEpoch: Int?,
    val rosterVersion: Int,
    val reason: String,
    val detail: String,
) {
    companion object {
        const val TYPE = "refusal"
        private const val CODE = "invalid_refusal"

        fun parse(json: JsonObject): RefusalEvent {
            Fields.envelope(json, TYPE, CODE)
            val drone = json["drone_id"]
            val epoch = json["connection_epoch"]
            return RefusalEvent(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                intentId = Fields.nullableString(json["intent_id"], "intent_id", CODE),
                commandId = Fields.nullableString(json["command_id"], "command_id", CODE),
                droneId = if (drone == null || drone == JsonNull) null else Fields.positiveInt32(drone, "drone_id", CODE),
                connectionEpoch = if (epoch == null || epoch == JsonNull) {
                    null
                } else {
                    Fields.positiveInt32(epoch, "connection_epoch", CODE)
                },
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                reason = Fields.nonEmptyString(json["reason"], "reason", CODE),
                detail = (json["detail"] as? JsonString)?.value ?: "",
            )
        }
    }
}
