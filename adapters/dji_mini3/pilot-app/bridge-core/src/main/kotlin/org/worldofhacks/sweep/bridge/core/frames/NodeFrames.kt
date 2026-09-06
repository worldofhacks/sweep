package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue
import org.worldofhacks.sweep.bridge.core.watchdog.NodeWatchdogState

/**
 * Node-authored frames exactly as `relay.contracts` parses them: `capabilities` (the
 * `CameraCapabilities` fields plus the probed hardware profile, flat), `node_status`, and
 * `capture_readiness`. All carry `drone_id` and `connection_epoch`, rely on the authenticated
 * drone binding like telemetry, and are not signed.
 */
enum class VideoPublishState(val wire: String) {
    STOPPED("stopped"),
    CONNECTING("connecting"),
    PUBLISHING("publishing"),
    FAILED("failed");

    companion object {
        fun fromWire(value: String): VideoPublishState? = entries.firstOrNull { it.wire == value }
    }
}

enum class PhoneThermalState(val wire: String) {
    NONE("none"),
    LIGHT("light"),
    MODERATE("moderate"),
    SEVERE("severe"),
    CRITICAL("critical"),
    EMERGENCY("emergency"),
    SHUTDOWN("shutdown");

    companion object {
        fun fromWire(value: String): PhoneThermalState? = entries.firstOrNull { it.wire == value }
    }
}

enum class GuidanceMode(val wire: String) {
    VISUAL_ADVISORY("visual_advisory"),
    REGISTERED_METRIC("registered_metric");

    companion object {
        fun fromWire(value: String): GuidanceMode? = entries.firstOrNull { it.wire == value }
    }
}

enum class DeltaKind(val wire: String) {
    YAW("yaw"),
    GIMBAL("gimbal");

    companion object {
        fun fromWire(value: String): DeltaKind? = entries.firstOrNull { it.wire == value }
    }
}

/**
 * The probed hardware profile carried inside `capabilities`. Every string is required to be
 * non-empty by the relay; a value the SDK has not reported yet is sent as `unreported`
 * rather than invented.
 */
data class HardwareProfile(
    val aircraftModel: String,
    val aircraftFirmware: String,
    val rcFirmware: String,
    val phoneModel: String,
    val androidVersion: String,
    val sdkVersion: String,
    val measuredHfovDeg: Double?,
) {
    init {
        listOf(
            "aircraft_model" to aircraftModel,
            "aircraft_firmware" to aircraftFirmware,
            "rc_firmware" to rcFirmware,
            "phone_model" to phoneModel,
            "android_version" to androidVersion,
            "sdk_version" to sdkVersion,
        ).forEach { (field, value) -> Fields.requireBoundedStateText(value, field) }
        require(measuredHfovDeg == null || measuredHfovDeg.isFinite() && measuredHfovDeg > 0 && measuredHfovDeg < 180) {
            "measured_hfov_deg must be null or between 0 and 180"
        }
    }

    companion object {
        const val UNREPORTED = "unreported"
    }
}

/** Camera capability fields as `adapters/protocols.py` `CameraCapabilities` defines them. */
data class CameraProbe(
    val nativePanoramaModes: List<String> = emptyList(),
    val photoCapture: Boolean = false,
    val gimbalPitchMinDeg: Double = -90.0,
    val gimbalPitchMaxDeg: Double = 60.0,
    val horizontalFovDeg: Double = 82.1,
    val storageRemainingBytes: Long = 0,
    val mediaRetrieval: Boolean = false,
) {
    init {
        Fields.validatedStringListSnapshot(nativePanoramaModes, "native_panorama_modes", allowEmpty = true)
        require(gimbalPitchMinDeg.isFinite() && gimbalPitchMaxDeg.isFinite() && gimbalPitchMinDeg < gimbalPitchMaxDeg) {
            "gimbal pitch range must be finite and ordered"
        }
        require(horizontalFovDeg.isFinite() && horizontalFovDeg > 0 && horizontalFovDeg <= 360) {
            "horizontal_fov_deg must be between 0 and 360"
        }
        require(storageRemainingBytes >= 0) { "storage_remaining_bytes must be non-negative" }
    }
}

data class CapabilitiesFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val camera: CameraProbe,
    val hardware: HardwareProfile,
) {
    fun toEvent(): JsonObject {
        val panoramaModeSnapshot = Fields.validatedStringListSnapshot(
            camera.nativePanoramaModes,
            "native_panorama_modes",
            allowEmpty = true,
        )
        return Json.json(
            "v" to Fields.PROTOCOL_VERSION,
            "t" to t,
            "type" to TYPE,
            "event_id" to eventId,
            "session" to session,
            "drone_id" to droneId,
            "connection_epoch" to connectionEpoch,
            "native_panorama_modes" to panoramaModeSnapshot,
            "photo_capture" to camera.photoCapture,
            "gimbal_pitch_min_deg" to camera.gimbalPitchMinDeg,
            "gimbal_pitch_max_deg" to camera.gimbalPitchMaxDeg,
            "horizontal_fov_deg" to camera.horizontalFovDeg,
            "storage_remaining_bytes" to camera.storageRemainingBytes,
            "media_retrieval" to camera.mediaRetrieval,
            "aircraft_model" to hardware.aircraftModel,
            "aircraft_firmware" to hardware.aircraftFirmware,
            "rc_firmware" to hardware.rcFirmware,
            "phone_model" to hardware.phoneModel,
            "android_version" to hardware.androidVersion,
            "sdk_version" to hardware.sdkVersion,
            "measured_hfov_deg" to hardware.measuredHfovDeg,
        )
    }

    companion object {
        const val TYPE = "capabilities"
        private const val CODE = "invalid_capabilities"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch",
            "native_panorama_modes", "photo_capture", "gimbal_pitch_min_deg", "gimbal_pitch_max_deg",
            "horizontal_fov_deg", "storage_remaining_bytes", "media_retrieval", "aircraft_model",
            "aircraft_firmware", "rc_firmware", "phone_model", "android_version", "sdk_version",
            "measured_hfov_deg",
        )

        fun parse(json: JsonObject): CapabilitiesFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val pitchMin = Fields.finiteNumber(json["gimbal_pitch_min_deg"], "gimbal_pitch_min_deg", CODE)
            val pitchMax = Fields.finiteNumber(json["gimbal_pitch_max_deg"], "gimbal_pitch_max_deg", CODE)
            if (pitchMin >= pitchMax) throw ContractError(CODE, "gimbal pitch range must be ordered")
            val fov = Fields.finiteNumber(json["horizontal_fov_deg"], "horizontal_fov_deg", CODE)
            if (fov <= 0.0 || fov > 360.0) throw ContractError(CODE, "horizontal_fov_deg must be between 0 and 360")
            val measuredRaw = json["measured_hfov_deg"]
            val measured = if (measuredRaw == null || measuredRaw == JsonNull) {
                null
            } else {
                Fields.finiteNumber(measuredRaw, "measured_hfov_deg", CODE).also {
                    if (it <= 0.0 || it >= 180.0) {
                        throw ContractError(CODE, "measured_hfov_deg must be null or between 0 and 180")
                    }
                }
            }
            return CapabilitiesFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                camera = CameraProbe(
                    nativePanoramaModes = Fields.stringList(
                        json["native_panorama_modes"],
                        "native_panorama_modes",
                        CODE,
                        allowEmpty = true,
                    ),
                    photoCapture = Fields.boolean(json["photo_capture"], "photo_capture", CODE),
                    gimbalPitchMinDeg = pitchMin,
                    gimbalPitchMaxDeg = pitchMax,
                    horizontalFovDeg = fov,
                    storageRemainingBytes = Fields.nonNegativeInt(
                        json["storage_remaining_bytes"],
                        "storage_remaining_bytes",
                        CODE,
                    ),
                    mediaRetrieval = Fields.boolean(json["media_retrieval"], "media_retrieval", CODE),
                ),
                hardware = HardwareProfile(
                    aircraftModel = Fields.boundedStateText(json["aircraft_model"], "aircraft_model", CODE),
                    aircraftFirmware = Fields.boundedStateText(json["aircraft_firmware"], "aircraft_firmware", CODE),
                    rcFirmware = Fields.boundedStateText(json["rc_firmware"], "rc_firmware", CODE),
                    phoneModel = Fields.boundedStateText(json["phone_model"], "phone_model", CODE),
                    androidVersion = Fields.boundedStateText(json["android_version"], "android_version", CODE),
                    sdkVersion = Fields.boundedStateText(json["sdk_version"], "sdk_version", CODE),
                    measuredHfovDeg = measured,
                ),
            )
        }
    }
}

data class SuggestedDelta(val kind: DeltaKind, val degrees: Double) {
    fun toJson(): JsonObject = Json.json("kind" to kind.wire, "degrees" to degrees)

    companion object {
        fun parse(json: JsonObject, code: String): SuggestedDelta {
            Fields.exact(json, setOf("kind", "degrees"), code)
            val kind = (json["kind"] as? JsonString)?.let { DeltaKind.fromWire(it.value) }
                ?: throw ContractError(code, "suggested_delta kind must be yaw or gimbal")
            return SuggestedDelta(kind, Fields.finiteNumber(json["degrees"], "degrees", code))
        }
    }
}

data class CaptureReadinessFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val roomId: String?,
    val captureId: String?,
    val guidanceMode: GuidanceMode,
    val poseSource: String,
    val poseOk: Boolean,
    val clearanceOk: Boolean,
    val cameraOk: Boolean,
    val storageOk: Boolean,
    val motionOk: Boolean,
    val imageQualityOk: Boolean,
    val coverageMissing: List<Double>,
    val nextHeadingDeg: Double?,
    val suggestedDelta: SuggestedDelta?,
) {
    init {
        validatedCoverageSnapshot(coverageMissing)
    }

    fun toEvent(): JsonObject {
        val coverageSnapshot = validatedCoverageSnapshot(coverageMissing)
        return Json.json(
            "v" to Fields.PROTOCOL_VERSION,
            "t" to t,
            "type" to TYPE,
            "event_id" to eventId,
            "session" to session,
            "drone_id" to droneId,
            "connection_epoch" to connectionEpoch,
            "room_id" to roomId,
            "capture_id" to captureId,
            "guidance_mode" to guidanceMode.wire,
            "pose_source" to poseSource,
            "pose_ok" to poseOk,
            "clearance_ok" to clearanceOk,
            "camera_ok" to cameraOk,
            "storage_ok" to storageOk,
            "motion_ok" to motionOk,
            "image_quality_ok" to imageQualityOk,
            "coverage_missing" to coverageSnapshot,
            "next_heading_deg" to nextHeadingDeg,
            "suggested_delta" to suggestedDelta?.toJson(),
        )
    }

    companion object {
        const val TYPE = "capture_readiness"
        const val MAX_COVERAGE_MISSING_ITEMS = 8
        private const val CODE = "invalid_capture_readiness"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch", "room_id", "capture_id",
            "guidance_mode", "pose_source", "pose_ok", "clearance_ok", "camera_ok", "storage_ok", "motion_ok",
            "image_quality_ok", "coverage_missing", "next_heading_deg", "suggested_delta",
        )

        fun parse(json: JsonObject): CaptureReadinessFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val guidance = (json["guidance_mode"] as? JsonString)?.let { GuidanceMode.fromWire(it.value) }
                ?: throw ContractError(CODE, "guidance_mode must be visual_advisory or registered_metric")
            val coverage = json["coverage_missing"] as? JsonArray
                ?: throw ContractError(CODE, "coverage_missing must be a list")
            if (coverage.items.size > MAX_COVERAGE_MISSING_ITEMS) {
                throw ContractError(
                    CODE,
                    "coverage_missing may contain at most $MAX_COVERAGE_MISSING_ITEMS items",
                )
            }
            val coverageMissing = coverage.items.map { Fields.azimuth(it, "coverage_missing", CODE) }
            if (coverageMissing.map(::normalizedHeading).toSet().size != coverageMissing.size) {
                throw ContractError(CODE, "coverage_missing may not contain duplicates")
            }
            val heading = json["next_heading_deg"]
            val delta = json["suggested_delta"]
            return CaptureReadinessFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                roomId = Fields.nullableString(json["room_id"], "room_id", CODE),
                captureId = Fields.nullableString(json["capture_id"], "capture_id", CODE),
                guidanceMode = guidance,
                poseSource = Fields.nonEmptyString(json["pose_source"], "pose_source", CODE),
                poseOk = Fields.boolean(json["pose_ok"], "pose_ok", CODE),
                clearanceOk = Fields.boolean(json["clearance_ok"], "clearance_ok", CODE),
                cameraOk = Fields.boolean(json["camera_ok"], "camera_ok", CODE),
                storageOk = Fields.boolean(json["storage_ok"], "storage_ok", CODE),
                motionOk = Fields.boolean(json["motion_ok"], "motion_ok", CODE),
                imageQualityOk = Fields.boolean(json["image_quality_ok"], "image_quality_ok", CODE),
                coverageMissing = coverageMissing,
                nextHeadingDeg = if (heading == null || heading == JsonNull) {
                    null
                } else {
                    Fields.azimuth(heading, "next_heading_deg", CODE)
                },
                suggestedDelta = if (delta == null || delta == JsonNull) {
                    null
                } else {
                    SuggestedDelta.parse(Fields.obj(delta, "suggested_delta", CODE), CODE)
                },
            )
        }

        private fun normalizedHeading(value: Double): Double = if (value == 0.0) 0.0 else value

        private fun validatedCoverageSnapshot(value: List<Double>): List<Double> {
            val snapshot = Fields.boundedListSnapshot(
                value,
                MAX_COVERAGE_MISSING_ITEMS,
                "coverage_missing",
            )
            require(snapshot.all { it.isFinite() && it >= 0.0 && it < 360.0 }) {
                "coverage_missing headings must be finite azimuths"
            }
            require(snapshot.map(::normalizedHeading).toSet().size == snapshot.size) {
                "coverage_missing may not contain duplicates"
            }
            return snapshot
        }
    }
}

/** The informational body of `node_status`; the link resends the frame whenever this changes. */
data class NodeStatusBody(
    val virtualStickEnabled: Boolean,
    val controlAuthority: Boolean,
    val authorityChangeReason: String?,
    val watchdogState: NodeWatchdogState,
    val videoPublishState: VideoPublishState,
    val phoneBatteryPercent: Int,
    val phoneThermalState: PhoneThermalState,
) {
    init {
        require(phoneBatteryPercent in 0..100) { "phone_battery_percent must be between 0 and 100" }
        require(authorityChangeReason == null || Fields.isMachineCode(authorityChangeReason)) {
            "authority_change_reason must be snake_case"
        }
    }
}

data class NodeStatusFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val body: NodeStatusBody,
) {
    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "virtual_stick_enabled" to body.virtualStickEnabled,
        "control_authority" to body.controlAuthority,
        "authority_change_reason" to body.authorityChangeReason,
        "watchdog_state" to body.watchdogState.wire,
        "video_publish_state" to body.videoPublishState.wire,
        "phone_battery_percent" to body.phoneBatteryPercent,
        "phone_thermal_state" to body.phoneThermalState.wire,
    )

    companion object {
        const val TYPE = "node_status"
        private const val CODE = "invalid_node_status"
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "drone_id", "connection_epoch",
            "virtual_stick_enabled", "control_authority", "authority_change_reason", "watchdog_state",
            "video_publish_state", "phone_battery_percent", "phone_thermal_state",
        )

        fun parse(json: JsonObject): NodeStatusFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val watchdog = enumField(json["watchdog_state"], "watchdog_state") { NodeWatchdogState.fromWire(it) }
            val publish = enumField(json["video_publish_state"], "video_publish_state") { VideoPublishState.fromWire(it) }
            val thermal = enumField(json["phone_thermal_state"], "phone_thermal_state") { PhoneThermalState.fromWire(it) }
            val battery = Fields.nonNegativeInt32(json["phone_battery_percent"], "phone_battery_percent", CODE)
            if (battery > 100) throw ContractError(CODE, "phone_battery_percent must be between 0 and 100")
            return NodeStatusFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE),
                body = NodeStatusBody(
                    virtualStickEnabled = Fields.boolean(json["virtual_stick_enabled"], "virtual_stick_enabled", CODE),
                    controlAuthority = Fields.boolean(json["control_authority"], "control_authority", CODE),
                    authorityChangeReason = Fields.nullableString(
                        json["authority_change_reason"],
                        "authority_change_reason",
                        CODE,
                        machineReadable = true,
                    ),
                    watchdogState = watchdog,
                    videoPublishState = publish,
                    phoneBatteryPercent = battery,
                    phoneThermalState = thermal,
                ),
            )
        }

        private fun <E> enumField(value: JsonValue?, field: String, fromWire: (String) -> E?): E =
            (value as? JsonString)?.let { fromWire(it.value) } ?: throw ContractError(CODE, "unknown $field")
    }
}
