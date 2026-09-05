package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

/** First frame on the node socket; mirrors `relay.auth.authenticate` for `source == "adapter"`. */
data class AuthFrame(val droneId: Int, val token: String) {
    fun toJson(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "type" to TYPE,
        "source" to SOURCE,
        "drone_id" to droneId,
        "token" to token,
    )

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
