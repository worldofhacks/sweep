package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogState

/**
 * Node-authored frames from the Phase A spec: `capabilities` (the `CameraCapabilities`
 * fields of `adapters/protocols.py` plus a hardware profile), `capture_readiness` (the
 * guidance event in the capture-guidance research note), and `node_status`.
 */
data class HardwareProfile(
    val aircraftModel: String,
    val aircraftFirmware: String?,
    val rcFirmware: String?,
    val phoneModel: String,
    val androidVersion: String,
    val msdkVersion: String,
    val horizontalFovDeg: Double?,
) {
    fun toJson(): JsonObject = Json.json(
        "aircraft_model" to aircraftModel,
        "aircraft_firmware" to aircraftFirmware,
        "rc_firmware" to rcFirmware,
        "phone_model" to phoneModel,
        "android_version" to androidVersion,
        "msdk_version" to msdkVersion,
        "horizontal_fov_deg" to horizontalFovDeg,
    )

    companion object {
        private val FIELDS = setOf(
            "aircraft_model", "aircraft_firmware", "rc_firmware", "phone_model",
            "android_version", "msdk_version", "horizontal_fov_deg",
        )

        fun parse(json: JsonObject, code: String): HardwareProfile {
            Fields.exact(json, FIELDS, code)
            return HardwareProfile(
                aircraftModel = Fields.nonEmptyString(json["aircraft_model"], "aircraft_model", code),
                aircraftFirmware = Fields.nullableString(json["aircraft_firmware"], "aircraft_firmware", code),
                rcFirmware = Fields.nullableString(json["rc_firmware"], "rc_firmware", code),
                phoneModel = Fields.nonEmptyString(json["phone_model"], "phone_model", code),
                androidVersion = Fields.nonEmptyString(json["android_version"], "android_version", code),
                msdkVersion = Fields.nonEmptyString(json["msdk_version"], "msdk_version", code),
                horizontalFovDeg = json["horizontal_fov_deg"].takeUnless { it == null || it == JsonNull }
                    ?.let { Fields.finiteNumber(it, "horizontal_fov_deg", code) },
            )
        }
    }
}

data class CapabilitiesFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val nativePanoramaModes: List<String>,
    val photoCapture: Boolean,
    val gimbalPitchMinDeg: Double,
    val gimbalPitchMaxDeg: Double,
    val horizontalFovDeg: Double,
    val storageRemainingBytes: Long,
    val mediaRetrieval: Boolean,
    val hardwareProfile: HardwareProfile,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "native_panorama_modes" to nativePanoramaModes,
        "photo_capture" to photoCapture,
        "gimbal_pitch_min_deg" to gimbalPitchMinDeg,
        "gimbal_pitch_max_deg" to gimbalPitchMaxDeg,
        "horizontal_fov_deg" to horizontalFovDeg,
        "storage_remaining_bytes" to storageRemainingBytes,
        "media_retrieval" to mediaRetrieval,
        "hardware_profile" to hardwareProfile.toJson(),
    )

    companion object {
        const val TYPE = "capabilities"
        private const val CODE = "invalid_capabilities"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch",
            "native_panorama_modes", "photo_capture", "gimbal_pitch_min_deg", "gimbal_pitch_max_deg",
            "horizontal_fov_deg", "storage_remaining_bytes", "media_retrieval", "hardware_profile",
        )

        fun parse(json: JsonObject): CapabilitiesFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            return CapabilitiesFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                nativePanoramaModes = Fields.stringList(
                    json["native_panorama_modes"],
                    "native_panorama_modes",
                    CODE,
                    allowEmpty = true,
                ),
                photoCapture = Fields.boolean(json["photo_capture"], "photo_capture", CODE),
                gimbalPitchMinDeg = Fields.finiteNumber(json["gimbal_pitch_min_deg"], "gimbal_pitch_min_deg", CODE),
                gimbalPitchMaxDeg = Fields.finiteNumber(json["gimbal_pitch_max_deg"], "gimbal_pitch_max_deg", CODE),
                horizontalFovDeg = Fields.finiteNumber(json["horizontal_fov_deg"], "horizontal_fov_deg", CODE),
                storageRemainingBytes = Fields.nonNegativeInt(
                    json["storage_remaining_bytes"],
                    "storage_remaining_bytes",
                    CODE,
                ),
                mediaRetrieval = Fields.boolean(json["media_retrieval"], "media_retrieval", CODE),
                hardwareProfile = HardwareProfile.parse(
                    Fields.obj(json["hardware_profile"], "hardware_profile", CODE),
                    CODE,
                ),
            )
        }
    }
}

data class SuggestedDelta(val kind: String, val degrees: Double) {
    fun toJson(): JsonObject = Json.json("kind" to kind, "degrees" to degrees)

    companion object {
        fun parse(json: JsonObject, code: String): SuggestedDelta {
            Fields.exact(json, setOf("kind", "degrees"), code)
            val kind = Fields.nonEmptyString(json["kind"], "suggested_delta.kind", code)
            if (!Fields.isMachineCode(kind)) throw ContractError(code, "suggested_delta.kind must be snake_case")
            return SuggestedDelta(kind, Fields.finiteNumber(json["degrees"], "suggested_delta.degrees", code))
        }
    }
}

data class CaptureReadinessFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val roomId: String,
    val captureId: String,
    val guidanceMode: String,
    val poseSource: String,
    val poseOk: Boolean,
    val clearanceOk: Boolean,
    val cameraOk: Boolean,
    val motionOk: Boolean,
    val imageQualityOk: Boolean,
    val coverageMissing: List<Int>,
    val nextHeadingDeg: Int?,
    val suggestedDelta: SuggestedDelta?,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "room_id" to roomId,
        "capture_id" to captureId,
        "guidance_mode" to guidanceMode,
        "pose_source" to poseSource,
        "pose_ok" to poseOk,
        "clearance_ok" to clearanceOk,
        "camera_ok" to cameraOk,
        "motion_ok" to motionOk,
        "image_quality_ok" to imageQualityOk,
        "coverage_missing" to coverageMissing,
        "next_heading_deg" to nextHeadingDeg,
        "suggested_delta" to suggestedDelta?.toJson(),
    )

    companion object {
        const val TYPE = "capture_readiness"
        val GUIDANCE_MODES = setOf("visual_advisory", "registered_metric")
        private const val CODE = "invalid_capture_readiness"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch", "room_id", "capture_id",
            "guidance_mode", "pose_source", "pose_ok", "clearance_ok", "camera_ok", "motion_ok",
            "image_quality_ok", "coverage_missing", "next_heading_deg", "suggested_delta",
        )

        fun parse(json: JsonObject): CaptureReadinessFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val guidanceMode = Fields.nonEmptyString(json["guidance_mode"], "guidance_mode", CODE)
            if (guidanceMode !in GUIDANCE_MODES) throw ContractError(CODE, "unknown guidance_mode")
            val heading = json["next_heading_deg"]
            val delta = json["suggested_delta"]
            return CaptureReadinessFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                roomId = Fields.nonEmptyString(json["room_id"], "room_id", CODE),
                captureId = Fields.nonEmptyString(json["capture_id"], "capture_id", CODE),
                guidanceMode = guidanceMode,
                poseSource = Fields.nonEmptyString(json["pose_source"], "pose_source", CODE),
                poseOk = Fields.boolean(json["pose_ok"], "pose_ok", CODE),
                clearanceOk = Fields.boolean(json["clearance_ok"], "clearance_ok", CODE),
                cameraOk = Fields.boolean(json["camera_ok"], "camera_ok", CODE),
                motionOk = Fields.boolean(json["motion_ok"], "motion_ok", CODE),
                imageQualityOk = Fields.boolean(json["image_quality_ok"], "image_quality_ok", CODE),
                coverageMissing = Fields.intList(json["coverage_missing"], "coverage_missing", CODE),
                nextHeadingDeg = if (heading == null || heading == JsonNull) {
                    null
                } else {
                    Fields.nonNegativeInt32(heading, "next_heading_deg", CODE)
                },
                suggestedDelta = if (delta == null || delta == JsonNull) {
                    null
                } else {
                    SuggestedDelta.parse(Fields.obj(delta, "suggested_delta", CODE), CODE)
                },
            )
        }
    }
}

data class NodeStatusFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val virtualStickEnabled: Boolean,
    val controlAuthority: Boolean,
    val authorityChangeReason: String?,
    val watchdogState: WatchdogState,
    val videoPublishState: String,
    val phoneBattery: Double,
    val phoneThermalState: String,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "virtual_stick_enabled" to virtualStickEnabled,
        "control_authority" to controlAuthority,
        "authority_change_reason" to authorityChangeReason,
        "watchdog_state" to watchdogState.wire,
        "video_publish_state" to videoPublishState,
        "phone_battery" to phoneBattery,
        "phone_thermal_state" to phoneThermalState,
    )

    companion object {
        const val TYPE = "node_status"
        private const val CODE = "invalid_node_status"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch",
            "virtual_stick_enabled", "control_authority", "authority_change_reason", "watchdog_state",
            "video_publish_state", "phone_battery", "phone_thermal_state",
        )

        fun parse(json: JsonObject): NodeStatusFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val watchdog = (json["watchdog_state"] as? JsonString)?.let { WatchdogState.fromWire(it.value) }
                ?: throw ContractError(CODE, "unknown watchdog_state")
            return NodeStatusFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                virtualStickEnabled = Fields.boolean(json["virtual_stick_enabled"], "virtual_stick_enabled", CODE),
                controlAuthority = Fields.boolean(json["control_authority"], "control_authority", CODE),
                authorityChangeReason = Fields.nullableString(
                    json["authority_change_reason"],
                    "authority_change_reason",
                    CODE,
                    machineReadable = true,
                ),
                watchdogState = watchdog,
                videoPublishState = Fields.nonEmptyString(json["video_publish_state"], "video_publish_state", CODE),
                phoneBattery = Fields.unitInterval(json["phone_battery"], "phone_battery", CODE),
                phoneThermalState = Fields.nonEmptyString(json["phone_thermal_state"], "phone_thermal_state", CODE),
            )
        }
    }
}
