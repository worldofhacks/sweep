package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.signing.Signing

/** The planner's operation vocabulary (`planner.models.CommandOperation`). */
enum class CommandOperation(val wire: String) {
    TAKEOFF("takeoff"),
    GOTO("goto"),
    ROTATE_TO("rotate_to"),
    HOVER("hover"),
    LAND("land"),
    ESTOP("estop"),
    CAMERA_CAPABILITIES("camera_capabilities"),
    SET_GIMBAL_PITCH("set_gimbal_pitch"),
    CAMERA_READY("camera_ready"),
    CAPTURE_PANORAMA("capture_panorama"),
    CAPTURE_PHOTO("capture_photo"),
    RETRIEVE_MEDIA("retrieve_media");

    companion object {
        fun fromWire(value: String): CommandOperation? = entries.firstOrNull { it.wire == value }
    }
}

/**
 * Typed `args` per operation, exactly as `relay.contracts.COMMAND_ARGUMENT_FIELDS` fixes
 * them: integers and IDs only. Millimetres, millimetres per second, millidegrees, and
 * millidegrees per second keep the signed canonical JSON free of floats, the same rule
 * signed membership claims follow. Speeds are positive.
 */
sealed interface CommandArgs {
    val operation: CommandOperation

    fun toJson(): JsonObject

    data class Takeoff(val zMm: Long) : CommandArgs {
        override val operation get() = CommandOperation.TAKEOFF
        override fun toJson() = Json.json("z_mm" to zMm)
    }

    data class Goto(val xMm: Long, val yMm: Long, val zMm: Long, val speedMmS: Long, val navigationRouteId: String? = null) : CommandArgs {
        override val operation get() = CommandOperation.GOTO
        override fun toJson() = Json.json("x_mm" to xMm, "y_mm" to yMm, "z_mm" to zMm, "speed_mm_s" to speedMmS).let { base ->
            navigationRouteId?.let { base.with("navigation_route_id", Json.value(it)) } ?: base
        }
    }

    data class RotateTo(val yawMdeg: Long, val speedMdegS: Long) : CommandArgs {
        override val operation get() = CommandOperation.ROTATE_TO
        override fun toJson() = Json.json("yaw_mdeg" to yawMdeg, "speed_mdeg_s" to speedMdegS)
    }

    data object Hover : CommandArgs {
        override val operation get() = CommandOperation.HOVER
        override fun toJson() = Json.json()
    }

    data object Land : CommandArgs {
        override val operation get() = CommandOperation.LAND
        override fun toJson() = Json.json()
    }

    data object Estop : CommandArgs {
        override val operation get() = CommandOperation.ESTOP
        override fun toJson() = Json.json()
    }

    data object CameraCapabilities : CommandArgs {
        override val operation get() = CommandOperation.CAMERA_CAPABILITIES
        override fun toJson() = Json.json()
    }

    data class SetGimbalPitch(val pitchMdeg: Long) : CommandArgs {
        override val operation get() = CommandOperation.SET_GIMBAL_PITCH
        override fun toJson() = Json.json("pitch_mdeg" to pitchMdeg)
    }

    data object CameraReady : CommandArgs {
        override val operation get() = CommandOperation.CAMERA_READY
        override fun toJson() = Json.json()
    }

    data class CapturePanorama(val captureId: String) : CommandArgs {
        override val operation get() = CommandOperation.CAPTURE_PANORAMA
        override fun toJson() = Json.json("capture_id" to captureId)
    }

    data class CapturePhoto(val captureId: String) : CommandArgs {
        override val operation get() = CommandOperation.CAPTURE_PHOTO
        override fun toJson() = Json.json("capture_id" to captureId)
    }

    data class RetrieveMedia(val fileId: String) : CommandArgs {
        override val operation get() = CommandOperation.RETRIEVE_MEDIA
        override fun toJson() = Json.json("file_id" to fileId)
    }

    companion object {
        private const val CODE = "invalid_command"

        fun parse(operation: CommandOperation, args: JsonObject): CommandArgs {
            val expected = when (operation) {
                CommandOperation.TAKEOFF -> setOf("z_mm")
                CommandOperation.GOTO -> setOf("x_mm", "y_mm", "z_mm", "speed_mm_s")
                CommandOperation.ROTATE_TO -> setOf("yaw_mdeg", "speed_mdeg_s")
                CommandOperation.SET_GIMBAL_PITCH -> setOf("pitch_mdeg")
                CommandOperation.CAPTURE_PANORAMA, CommandOperation.CAPTURE_PHOTO -> setOf("capture_id")
                CommandOperation.RETRIEVE_MEDIA -> setOf("file_id")
                CommandOperation.HOVER, CommandOperation.LAND, CommandOperation.ESTOP,
                CommandOperation.CAMERA_CAPABILITIES, CommandOperation.CAMERA_READY,
                -> emptySet()
            }
            if (args.keys != expected && !(operation == CommandOperation.GOTO && args.keys == expected + "navigation_route_id")) {
                throw ContractError(CODE, "${operation.wire} arguments do not match the v1 contract")
            }
            fun integer(field: String) = Fields.integer(args[field], field, CODE)
            fun positive(field: String) = Fields.positiveInt(args[field], field, CODE)
            fun id(field: String) = Fields.nonEmptyString(args[field], field, CODE)
            return when (operation) {
                CommandOperation.TAKEOFF -> Takeoff(integer("z_mm"))
                CommandOperation.GOTO -> Goto(integer("x_mm"), integer("y_mm"), integer("z_mm"), positive("speed_mm_s"), args["navigation_route_id"]?.let { Fields.nonEmptyString(it, "navigation_route_id", CODE) })
                CommandOperation.ROTATE_TO -> RotateTo(integer("yaw_mdeg"), positive("speed_mdeg_s"))
                CommandOperation.HOVER -> Hover
                CommandOperation.LAND -> Land
                CommandOperation.ESTOP -> Estop
                CommandOperation.CAMERA_CAPABILITIES -> CameraCapabilities
                CommandOperation.SET_GIMBAL_PITCH -> SetGimbalPitch(integer("pitch_mdeg"))
                CommandOperation.CAMERA_READY -> CameraReady
                CommandOperation.CAPTURE_PANORAMA -> CapturePanorama(id("capture_id"))
                CommandOperation.CAPTURE_PHOTO -> CapturePhoto(id("capture_id"))
                CommandOperation.RETRIEVE_MEDIA -> RetrieveMedia(id("file_id"))
            }
        }
    }
}

/**
 * Relay-to-node command frame (`relay.contracts.CommandFrame`). `seq` is monotonic per node
 * per connection epoch starting at 1, `issued_at` and `ttl_ms` are relay-clock milliseconds,
 * and `signature` is the relay's HMAC-SHA256 with this aircraft's key over the canonical JSON
 * of every other field, so the node can verify that the relay authored the command.
 *
 * [rawArgs] keeps the `args` object exactly as received so verification re-encodes the
 * bytes that were signed.
 */
data class CommandFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val commandId: String,
    val intentId: String,
    val rosterVersion: Int,
    val droneId: Int,
    val connectionEpoch: Int,
    val seq: Long,
    val issuedAt: Long,
    val ttlMs: Long,
    val operation: CommandOperation,
    val args: CommandArgs,
    val signature: String = "",
    val rawArgs: JsonObject = args.toJson(),
) {
    init {
        require(args.operation == operation) { "args ${args.operation.wire} do not belong to ${operation.wire}" }
    }

    fun unsignedEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "command_id" to commandId,
        "intent_id" to intentId,
        "roster_version" to rosterVersion,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "seq" to seq,
        "issued_at" to issuedAt,
        "ttl_ms" to ttlMs,
        "operation" to operation.wire,
        "args" to rawArgs,
    )

    fun toJson(): JsonObject = unsignedEvent().with("signature", JsonString(signature))

    fun signed(key: ByteArray): CommandFrame = copy(signature = Signing.sign(unsignedEvent(), key))

    fun verify(key: ByteArray): Boolean = Signing.verify(unsignedEvent(), signature, key)

    companion object {
        const val TYPE = "command"
        private const val CODE = "invalid_command"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "command_id", "intent_id", "roster_version",
            "drone_id", "connection_epoch", "seq", "issued_at", "ttl_ms", "operation", "args", "signature",
        )

        fun parse(json: JsonObject): CommandFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val operation = (json["operation"] as? JsonString)?.let { CommandOperation.fromWire(it.value) }
                ?: throw ContractError(CODE, "unknown command operation")
            val rawArgs = Fields.obj(json["args"], "args", CODE)
            return CommandFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                commandId = Fields.nonEmptyString(json["command_id"], "command_id", CODE),
                intentId = Fields.nonEmptyString(json["intent_id"], "intent_id", CODE),
                rosterVersion = Fields.nonNegativeInt32(json["roster_version"], "roster_version", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                seq = Fields.positiveInt(json["seq"], "seq", CODE),
                issuedAt = Fields.nonNegativeInt(json["issued_at"], "issued_at", CODE),
                ttlMs = Fields.positiveInt(json["ttl_ms"], "ttl_ms", CODE),
                operation = operation,
                args = CommandArgs.parse(operation, rawArgs),
                signature = Fields.nonEmptyString(json["signature"], "signature", "invalid_signature"),
                rawArgs = rawArgs,
            )
        }
    }
}
