package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

enum class LifecycleStatus(val wire: String) {
    ACCEPTED("accepted"),
    REFUSED("refused"),
    EXECUTING("executing"),
    COMPLETED("completed"),
    FAILED("failed"),
    INVALIDATED("invalidated");

    val terminalFailure: Boolean
        get() = this == FAILED || this == INVALIDATED

    companion object {
        fun fromWire(value: String): LifecycleStatus? = entries.firstOrNull { it.wire == value }
    }
}

/**
 * Node-authored acknowledgement exactly as `relay.contracts.parse_adapter_acknowledgement`
 * accepts it: no `source` field (the relay stamps that), `refused` never allowed here, and a
 * snake_case `reason` required for `failed` and `invalidated`.
 */
data class AcknowledgementFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val intentId: String,
    val commandId: String,
    val status: LifecycleStatus,
    val droneId: Int,
    val connectionEpoch: Int,
    val rosterVersion: Int,
    val reason: String?,
    val detail: String?,
) {
    init {
        require(status != LifecycleStatus.REFUSED) { "refused outcomes use the refusal envelope" }
        require(!status.terminalFailure || reason != null) { "terminal failure requires a reason" }
        require(reason == null || Fields.isMachineCode(reason)) { "acknowledgement reason must be snake_case" }
    }

    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "intent_id" to intentId,
        "command_id" to commandId,
        "status" to status.wire,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "roster_version" to rosterVersion,
        "reason" to reason,
        "detail" to detail,
    )

    companion object {
        const val TYPE = "acknowledgement"
        private const val CODE = "invalid_acknowledgement"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "intent_id", "command_id", "status",
            "drone_id", "connection_epoch", "roster_version", "reason", "detail",
        )

        fun parse(json: JsonObject): AcknowledgementFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val status = (json["status"] as? JsonString)?.let { LifecycleStatus.fromWire(it.value) }
                ?: throw ContractError(CODE, "unknown lifecycle status")
            if (status == LifecycleStatus.REFUSED) throw ContractError(CODE, "refused outcomes use the refusal envelope")
            val reason = Fields.nullableString(json["reason"], "reason", CODE, machineReadable = true)
            val detail = Fields.nullableString(json["detail"], "detail", CODE)
            val commandId = Fields.nonEmptyString(json["command_id"], "command_id", CODE)
            if (status.terminalFailure && reason == null) throw ContractError(CODE, "terminal failure requires a reason")
            return AcknowledgementFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                intentId = Fields.nonEmptyString(json["intent_id"], "intent_id", CODE),
                commandId = commandId,
                status = status,
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                reason = reason,
                detail = detail,
            )
        }
    }
}
