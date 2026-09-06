package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.signing.Signing

/** Wire membership actions a node may author; relay-internal actions are rejected on parse. */
enum class MembershipAction(val wire: String) {
    JOIN("join"),
    READINESS("readiness"),
    GRACEFUL_LEAVE("graceful_leave");

    companion object {
        fun fromWire(value: String): MembershipAction? = entries.firstOrNull { it.wire == value }
    }
}

/** Signed membership frames mirroring `relay.contracts.MembershipRequest.unsigned_event`. */
sealed interface MembershipFrame {
    val t: Long
    val eventId: String
    val session: String
    val droneId: Int
    val action: MembershipAction

    fun unsignedEvent(): JsonObject {
        val common = Json.json(
            "v" to Fields.PROTOCOL_VERSION,
            "t" to t,
            "type" to TYPE,
            "event_id" to eventId,
            "session" to session,
            "drone_id" to droneId,
            "action" to action.wire,
        )
        return JsonObject(common.fields + actionFields().fields)
    }

    fun actionFields(): JsonObject

    fun sign(key: ByteArray): String = Signing.sign(unsignedEvent(), key)

    fun signed(key: ByteArray): JsonObject {
        val unsigned = unsignedEvent()
        return unsigned.with("signature", JsonString(Signing.sign(unsigned, key)))
    }

    data class Join(
        override val t: Long,
        override val eventId: String,
        override val session: String,
        override val droneId: Int,
        val adapterId: String,
        val capabilities: List<String>,
    ) : MembershipFrame {
        init {
            Fields.requireBoundedStateText(adapterId, "adapter_id")
            Fields.validatedStringListSnapshot(capabilities, "capabilities", allowEmpty = false)
        }

        override val action: MembershipAction get() = MembershipAction.JOIN

        override fun actionFields(): JsonObject {
            val capabilitySnapshot = Fields.validatedStringListSnapshot(
                capabilities,
                "capabilities",
                allowEmpty = false,
            )
            return Json.json("adapter_id" to adapterId, "capabilities" to capabilitySnapshot)
        }
    }

    data class Readiness(
        override val t: Long,
        override val eventId: String,
        override val session: String,
        override val droneId: Int,
        val connectionEpoch: Int,
        val homePoseConfirmed: Boolean,
        val controlAuthority: Boolean,
        val rcSafetyOperatorPresent: Boolean,
    ) : MembershipFrame {
        override val action: MembershipAction get() = MembershipAction.READINESS

        override fun actionFields(): JsonObject = Json.json(
            "connection_epoch" to connectionEpoch,
            "home_pose_confirmed" to homePoseConfirmed,
            "control_authority" to controlAuthority,
            "rc_safety_operator_present" to rcSafetyOperatorPresent,
        )
    }

    data class GracefulLeave(
        override val t: Long,
        override val eventId: String,
        override val session: String,
        override val droneId: Int,
        val connectionEpoch: Int,
    ) : MembershipFrame {
        override val action: MembershipAction get() = MembershipAction.GRACEFUL_LEAVE

        override fun actionFields(): JsonObject = Json.json("connection_epoch" to connectionEpoch)
    }

    companion object {
        const val TYPE = "membership"
        private const val CODE = "invalid_membership"
        private val COMMON = setOf("v", "t", "type", "event_id", "session", "drone_id", "action", "signature")
        private val ACTION_FIELDS = mapOf(
            MembershipAction.JOIN to setOf("adapter_id", "capabilities"),
            MembershipAction.READINESS to setOf(
                "connection_epoch",
                "home_pose_confirmed",
                "control_authority",
                "rc_safety_operator_present",
            ),
            MembershipAction.GRACEFUL_LEAVE to setOf("connection_epoch"),
        )

        /** Validates shape exactly as the relay does; the signature is checked separately. */
        fun parse(json: JsonObject): MembershipFrame {
            val actionValue = (json["action"] as? JsonString)?.value
                ?: throw ContractError(CODE, "unknown membership action")
            val action = MembershipAction.fromWire(actionValue)
                ?: throw ContractError(CODE, "membership action is relay-internal or unknown")
            Fields.exact(json, COMMON + ACTION_FIELDS.getValue(action), CODE)
            Fields.envelope(json, TYPE, CODE)
            val droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE)
            Fields.nonEmptyString(json["signature"], "signature", "invalid_signature")
            val t = Fields.nonNegativeInt(json["t"], "t", CODE)
            val eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE)
            val session = Fields.nonEmptyString(json["session"], "session", CODE)
            return when (action) {
                MembershipAction.JOIN -> Join(
                    t = t,
                    eventId = eventId,
                    session = session,
                    droneId = droneId,
                    adapterId = Fields.boundedStateText(json["adapter_id"], "adapter_id", CODE),
                    capabilities = Fields.stringList(json["capabilities"], "capabilities", CODE, allowEmpty = false),
                )
                MembershipAction.READINESS -> Readiness(
                    t = t,
                    eventId = eventId,
                    session = session,
                    droneId = droneId,
                    connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                    homePoseConfirmed = Fields.boolean(json["home_pose_confirmed"], "home_pose_confirmed", CODE),
                    controlAuthority = Fields.boolean(json["control_authority"], "control_authority", CODE),
                    rcSafetyOperatorPresent = Fields.boolean(
                        json["rc_safety_operator_present"],
                        "rc_safety_operator_present",
                        CODE,
                    ),
                )
                MembershipAction.GRACEFUL_LEAVE -> GracefulLeave(
                    t = t,
                    eventId = eventId,
                    session = session,
                    droneId = droneId,
                    connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                )
            }
        }
    }
}
