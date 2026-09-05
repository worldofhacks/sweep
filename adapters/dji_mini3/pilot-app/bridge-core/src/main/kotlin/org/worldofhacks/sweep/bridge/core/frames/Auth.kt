package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

/** First frame on the node socket; mirrors `relay.auth.authenticate` for `source == "adapter"`. */
data class AuthFrame(val droneId: Int, val token: String) {
    fun toJson(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "type" to TYPE,
        "source" to SOURCE,
        "drone_id" to droneId,
        "token" to token,
    )

    /** Never includes the token. */
    override fun toString(): String = "AuthFrame(droneId=$droneId, token=<redacted>)"

    companion object {
        const val TYPE = "auth"
        const val SOURCE = "adapter"
        private const val CODE = "invalid_auth"
        private val FIELDS = setOf("v", "type", "source", "drone_id", "token")

        fun parse(json: JsonObject): AuthFrame {
            Fields.exact(json, FIELDS, CODE)
            if (json["v"] != Json.value(Fields.PROTOCOL_VERSION)) throw ContractError(CODE, "v must be integer 1")
            Fields.exactString(json["type"], "type", TYPE, CODE)
            Fields.exactString(json["source"], "source", SOURCE, CODE)
            return AuthFrame(
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                token = Fields.nonEmptyString(json["token"], "token", CODE),
            )
        }
    }
}

/**
 * Thresholds the relay distributes inside `auth.accepted.node` (`RelaySettings.node_settings`)
 * so no node invents its own: the command TTL, the Virtual Stick rate, and the watchdog hold
 * and failsafe thresholds. The same invariants the relay settings enforce apply here.
 */
data class NodeSettings(
    val commandTtlMs: Long,
    val virtualStickHz: Int,
    val watchdogHoldMs: Long,
    val watchdogFailsafeMs: Long,
) {
    init {
        require(commandTtlMs > 0) { "command_ttl_ms must be positive" }
        require(virtualStickHz in 5..25) { "virtual_stick_hz must be within the documented 5 to 25" }
        require(watchdogHoldMs >= 0 && watchdogFailsafeMs > watchdogHoldMs) {
            "watchdog thresholds must satisfy 0 <= hold < failsafe"
        }
    }

    fun toJson(): JsonObject = Json.json(
        "command_ttl_ms" to commandTtlMs,
        "virtual_stick_hz" to virtualStickHz,
        "watchdog_hold_ms" to watchdogHoldMs,
        "watchdog_failsafe_ms" to watchdogFailsafeMs,
    )

    companion object {
        private val FIELDS = setOf("command_ttl_ms", "virtual_stick_hz", "watchdog_hold_ms", "watchdog_failsafe_ms")

        fun parse(json: JsonObject, code: String): NodeSettings {
            Fields.exact(json, FIELDS, code)
            val hz = Fields.positiveInt32(json["virtual_stick_hz"], "virtual_stick_hz", code)
            if (hz !in 5..25) throw ContractError(code, "virtual_stick_hz must be within the documented 5 to 25")
            val hold = Fields.nonNegativeInt(json["watchdog_hold_ms"], "watchdog_hold_ms", code)
            val failsafe = Fields.positiveInt(json["watchdog_failsafe_ms"], "watchdog_failsafe_ms", code)
            if (failsafe <= hold) throw ContractError(code, "watchdog thresholds must satisfy 0 <= hold < failsafe")
            return NodeSettings(
                commandTtlMs = Fields.positiveInt(json["command_ttl_ms"], "command_ttl_ms", code),
                virtualStickHz = hz,
                watchdogHoldMs = hold,
                watchdogFailsafeMs = failsafe,
            )
        }
    }
}

/** `auth.accepted` as `relay.app._auth_accepted` emits it; `node` is present for adapters. */
data class AuthAccepted(
    val t: Long,
    val eventId: String,
    val session: String,
    val source: String,
    val droneId: Int?,
    val node: NodeSettings?,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "source" to source,
        "drone_id" to droneId,
        "node" to node?.toJson(),
    )

    companion object {
        const val TYPE = "auth.accepted"
        private const val CODE = "invalid_auth_accepted"
        private val FIELDS = setOf("v", "t", "type", "event_id", "session", "source", "drone_id", "node")

        fun parse(json: JsonObject): AuthAccepted {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val drone = json["drone_id"]
            val node = json["node"]
            return AuthAccepted(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                source = Fields.nonEmptyString(json["source"], "source", CODE),
                droneId = if (drone == null || drone == JsonNull) null else Fields.positiveInt32(drone, "drone_id", CODE),
                node = if (node == null || node == JsonNull) null else NodeSettings.parse(Fields.obj(node, "node", CODE), CODE),
            )
        }
    }
}

/** `auth.refused` as `relay.app._auth_refused` emits it; the relay then closes with code 1008. */
data class AuthRefused(
    val t: Long,
    val eventId: String,
    val session: String,
    val reason: String,
    val detail: String,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "status" to "refused",
        "reason" to reason,
        "detail" to detail,
    )

    companion object {
        const val TYPE = "auth.refused"
        private const val CODE = "invalid_auth_refused"
        private val FIELDS = setOf("v", "t", "type", "event_id", "session", "status", "reason", "detail")

        fun parse(json: JsonObject): AuthRefused {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            Fields.exactString(json["status"], "status", "refused", CODE)
            val reason = Fields.nonEmptyString(json["reason"], "reason", CODE)
            if (!Fields.isMachineCode(reason)) throw ContractError(CODE, "reason must be snake_case")
            val detail = json["detail"]
            return AuthRefused(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                reason = reason,
                detail = (detail as? JsonString)?.value ?: "",
            )
        }
    }
}
