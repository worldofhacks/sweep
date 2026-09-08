package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

/**
 * The media frames of the node protocol exactly as `relay.contracts` parses them:
 * `media_file` (one captured file with the pose it was taken from) and `capture_bundle`
 * (a closed set). Like the other node-authored frames they carry `drone_id` and
 * `connection_epoch`, rely on the authenticated drone binding, and are not signed.
 */
enum class RetrievalStatus(val wire: String) {
    /** Reported at capture time: the file exists on the aircraft, its bytes have not been downloaded. */
    PENDING("pending"),
    COMPLETED("completed"),
    UNSUPPORTED("unsupported"),
    FAILED("failed");

    companion object {
        fun fromWire(value: String): RetrievalStatus? = entries.firstOrNull { it.wire == value }
    }
}

/** `capture_bundle.status` and the camera result vocabulary; a bundle is never pending. */
enum class CaptureStatus(val wire: String) {
    COMPLETED("completed"),
    UNSUPPORTED("unsupported"),
    FAILED("failed");

    companion object {
        fun fromWire(value: String): CaptureStatus? = entries.firstOrNull { it.wire == value }
    }
}

data class WirePose(val x: Double, val y: Double, val z: Double) {
    fun toJson(): JsonObject = Json.json("x" to x, "y" to y, "z" to z)

    companion object {
        fun parse(json: JsonObject, code: String): WirePose {
            Fields.exact(json, setOf("x", "y", "z"), code)
            return WirePose(
                Fields.finiteNumber(json["x"], "x", code),
                Fields.finiteNumber(json["y"], "y", code),
                Fields.finiteNumber(json["z"], "z", code),
            )
        }
    }
}

data class WireIntrinsics(val widthPx: Int, val heightPx: Int, val horizontalFovDeg: Double, val projection: String) {
    fun toJson(): JsonObject = Json.json(
        "width_px" to widthPx,
        "height_px" to heightPx,
        "horizontal_fov_deg" to horizontalFovDeg,
        "projection" to projection,
    )

    companion object {
        fun parse(json: JsonObject, code: String): WireIntrinsics {
            Fields.exact(json, setOf("width_px", "height_px", "horizontal_fov_deg", "projection"), code)
            val fov = Fields.finiteNumber(json["horizontal_fov_deg"], "horizontal_fov_deg", code)
            if (fov <= 0.0 || fov > 360.0) throw ContractError(code, "horizontal_fov_deg must be between 0 and 360")
            return WireIntrinsics(
                widthPx = Fields.positiveInt32(json["width_px"], "width_px", code),
                heightPx = Fields.positiveInt32(json["height_px"], "height_px", code),
                horizontalFovDeg = fov,
                projection = Fields.nonEmptyString(json["projection"], "projection", code),
            )
        }
    }
}

enum class MediaPositionFrame(val wire: String) {
    DJI_LOCAL_ENU("dji_local_enu"),
    MAP_ENU("map_enu");

    companion object {
        fun fromWire(value: String): MediaPositionFrame? = entries.firstOrNull { it.wire == value }
    }
}

enum class MediaYawFrame(val wire: String) {
    DJI_COMPASS_DEG("dji_compass_deg");

    companion object {
        fun fromWire(value: String): MediaYawFrame? = entries.firstOrNull { it.wire == value }
    }
}

data class MapPoseProvenance(
    val navigationPoseEventId: String,
    val navigationPoseSeq: Long,
    val commandId: String,
    val routeId: String,
    val poseTimeMs: Long,
    val fixTimeMs: Long,
    val positionUncertaintyMm: Long,
    val navigationConfigId: String,
    val navigationConfigSha256: String,
    val mapVersion: String,
    val mapSha256: String,
    val geometrySha256: String,
    val cameraCalibrationSha256: String,
    val bodyExtrinsicsSha256: String,
    val worldTransformSha256: String,
    val controlSourceIds: List<String>,
) {
    init {
        require(listOf(navigationPoseEventId, commandId, routeId, navigationConfigId, mapVersion).all(::isMapIdentity)) {
            "map pose provenance identities are invalid"
        }
        require(navigationPoseSeq > 0 && poseTimeMs >= fixTimeMs && fixTimeMs >= 0 && positionUncertaintyMm > 0) {
            "map pose provenance timing is invalid"
        }
        require(
            listOf(
                navigationConfigSha256, mapSha256, geometrySha256, cameraCalibrationSha256,
                bodyExtrinsicsSha256, worldTransformSha256,
            ).all { it.length == 64 && it.all { char -> char in '0'..'9' || char in 'a'..'f' } },
        ) { "map pose provenance hashes are invalid" }
        require(controlSourceIds.isNotEmpty() && controlSourceIds == controlSourceIds.distinct().sorted() && controlSourceIds.all(::isMapIdentity)) {
            "map pose provenance source identities are invalid"
        }
    }

    fun toJson(): JsonObject = Json.json(
        "navigation_pose_event_id" to navigationPoseEventId,
        "navigation_pose_seq" to navigationPoseSeq,
        "command_id" to commandId,
        "route_id" to routeId,
        "pose_time_ms" to poseTimeMs,
        "fix_time_ms" to fixTimeMs,
        "position_uncertainty_mm" to positionUncertaintyMm,
        "navigation_config_id" to navigationConfigId,
        "navigation_config_sha256" to navigationConfigSha256,
        "map_version" to mapVersion,
        "map_sha256" to mapSha256,
        "geometry_sha256" to geometrySha256,
        "camera_calibration_sha256" to cameraCalibrationSha256,
        "body_extrinsics_sha256" to bodyExtrinsicsSha256,
        "world_transform_sha256" to worldTransformSha256,
        "control_source_ids" to controlSourceIds,
    )

    companion object {
        private val FIELDS = setOf(
            "navigation_pose_event_id", "navigation_pose_seq", "command_id", "route_id", "pose_time_ms", "fix_time_ms",
            "position_uncertainty_mm", "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256",
            "geometry_sha256", "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256", "control_source_ids",
        )

        fun parse(json: JsonObject, code: String): MapPoseProvenance {
            Fields.exact(json, FIELDS, code)
            return try {
                MapPoseProvenance(
                    Fields.nonEmptyString(json["navigation_pose_event_id"], "navigation_pose_event_id", code),
                    Fields.positiveInt(json["navigation_pose_seq"], "navigation_pose_seq", code),
                    Fields.nonEmptyString(json["command_id"], "command_id", code),
                    Fields.nonEmptyString(json["route_id"], "route_id", code),
                    Fields.nonNegativeInt(json["pose_time_ms"], "pose_time_ms", code),
                    Fields.nonNegativeInt(json["fix_time_ms"], "fix_time_ms", code),
                    Fields.positiveInt(json["position_uncertainty_mm"], "position_uncertainty_mm", code),
                    Fields.nonEmptyString(json["navigation_config_id"], "navigation_config_id", code),
                    Fields.nonEmptyString(json["navigation_config_sha256"], "navigation_config_sha256", code),
                    Fields.nonEmptyString(json["map_version"], "map_version", code),
                    Fields.nonEmptyString(json["map_sha256"], "map_sha256", code),
                    Fields.nonEmptyString(json["geometry_sha256"], "geometry_sha256", code),
                    Fields.nonEmptyString(json["camera_calibration_sha256"], "camera_calibration_sha256", code),
                    Fields.nonEmptyString(json["body_extrinsics_sha256"], "body_extrinsics_sha256", code),
                    Fields.nonEmptyString(json["world_transform_sha256"], "world_transform_sha256", code),
                    Fields.stringList(json["control_source_ids"], "control_source_ids", code, allowEmpty = false),
                )
            } catch (error: IllegalArgumentException) {
                throw ContractError(code, error.message ?: "map pose provenance is invalid")
            }
        }
    }
}

private fun isMapIdentity(value: String): Boolean = Fields.isCanonicalPrintable(value, 128)

/** `relay.contracts.MediaFileRecord`: the `MediaFile` fields without the transport envelope. */
data class MediaFileRecord(
    val captureId: String,
    val fileId: String,
    val timestampMs: Long,
    val droneId: Int,
    val connectionEpoch: Int,
    val pose: WirePose,
    val positionFrame: MediaPositionFrame = MediaPositionFrame.DJI_LOCAL_ENU,
    val actualYawDeg: Double,
    val yawFrame: MediaYawFrame = MediaYawFrame.DJI_COMPASS_DEG,
    val gimbalPitchDeg: Double,
    val intrinsics: WireIntrinsics,
    val checksumSha256: String,
    val storageRef: String,
    val retrievalStatus: RetrievalStatus,
    val mapPoseProvenance: MapPoseProvenance? = null,
) {
    init {
        require(isChecksum(checksumSha256)) { "checksum_sha256 must be 64 lowercase hex characters" }
        require(retrievalStatus != RetrievalStatus.PENDING || checksumSha256 == PENDING_CHECKSUM) {
            "pending media requires the all-zero checksum sentinel"
        }
        require(retrievalStatus != RetrievalStatus.COMPLETED || checksumSha256 != PENDING_CHECKSUM) {
            "completed media requires a content checksum"
        }
        require((positionFrame == MediaPositionFrame.MAP_ENU) == (mapPoseProvenance != null)) {
            "map-frame media requires map pose provenance"
        }
    }

    fun toJson(): JsonObject = Json.json(
        "capture_id" to captureId,
        "file_id" to fileId,
        "timestamp_ms" to timestampMs,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "pose" to pose.toJson(),
        "position_frame" to positionFrame.wire,
        "actual_yaw_deg" to actualYawDeg,
        "yaw_frame" to yawFrame.wire,
        "gimbal_pitch_deg" to gimbalPitchDeg,
        "intrinsics" to intrinsics.toJson(),
        "checksum_sha256" to checksumSha256,
        "storage_ref" to storageRef,
        "retrieval_status" to retrievalStatus.wire,
        "map_pose_provenance" to (mapPoseProvenance?.toJson() ?: JsonNull),
    )

    companion object {
        /** The checksum of a `pending` record: no bytes have been hashed yet. */
        const val PENDING_CHECKSUM = "0000000000000000000000000000000000000000000000000000000000000000"
        val FIELDS = setOf(
            "capture_id", "file_id", "timestamp_ms", "drone_id", "connection_epoch", "pose", "position_frame", "actual_yaw_deg",
            "yaw_frame", "gimbal_pitch_deg", "intrinsics", "checksum_sha256", "storage_ref", "retrieval_status", "map_pose_provenance",
        )

        fun isChecksum(value: String): Boolean = value.length == 64 && value.all { it in '0'..'9' || it in 'a'..'f' }

        fun parse(json: JsonObject, code: String): MediaFileRecord {
            Fields.exact(json, FIELDS, code)
            val checksum = (json["checksum_sha256"] as? JsonString)?.value
            if (checksum == null || !isChecksum(checksum)) throw ContractError(code, "checksum_sha256 must be 64 lowercase hex characters")
            val status = (json["retrieval_status"] as? JsonString)?.let { RetrievalStatus.fromWire(it.value) }
                ?: throw ContractError(code, "retrieval_status must be pending, completed, unsupported, or failed")
            if (status == RetrievalStatus.PENDING && checksum != PENDING_CHECKSUM) {
                throw ContractError(code, "pending media requires the all-zero checksum sentinel")
            }
            if (status == RetrievalStatus.COMPLETED && checksum == PENDING_CHECKSUM) {
                throw ContractError(code, "completed media requires a content checksum")
            }
            val positionFrame = (json["position_frame"] as? JsonString)?.let { MediaPositionFrame.fromWire(it.value) }
                ?: throw ContractError(code, "position_frame must be dji_local_enu or map_enu")
            val yawFrame = (json["yaw_frame"] as? JsonString)?.let { MediaYawFrame.fromWire(it.value) }
                ?: throw ContractError(code, "yaw_frame must be dji_compass_deg")
            val provenance = when (val raw = json["map_pose_provenance"]) {
                JsonNull -> null
                is JsonObject -> MapPoseProvenance.parse(raw, code)
                else -> throw ContractError(code, "map_pose_provenance must be an object or null")
            }
            return MediaFileRecord(
                captureId = Fields.nonEmptyString(json["capture_id"], "capture_id", code),
                fileId = Fields.nonEmptyString(json["file_id"], "file_id", code),
                timestampMs = Fields.nonNegativeInt(json["timestamp_ms"], "timestamp_ms", code),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", code),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", code),
                pose = WirePose.parse(Fields.obj(json["pose"], "pose", code), code),
                positionFrame = positionFrame,
                actualYawDeg = Fields.finiteNumber(json["actual_yaw_deg"], "actual_yaw_deg", code),
                yawFrame = yawFrame,
                gimbalPitchDeg = Fields.finiteNumber(json["gimbal_pitch_deg"], "gimbal_pitch_deg", code),
                intrinsics = WireIntrinsics.parse(Fields.obj(json["intrinsics"], "intrinsics", code), code),
                checksumSha256 = checksum,
                storageRef = Fields.nonEmptyString(json["storage_ref"], "storage_ref", code),
                retrievalStatus = status,
                mapPoseProvenance = provenance,
            )
        }
    }
}

/** `media_file`: the envelope plus one [MediaFileRecord], flat. */
data class MediaFileFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val file: MediaFileRecord,
) {
    fun toEvent(): JsonObject = JsonObject(
        Json.json(
            "v" to Fields.PROTOCOL_VERSION,
            "t" to t,
            "type" to TYPE,
            "event_id" to eventId,
            "session" to session,
        ).fields + file.toJson().fields,
    )

    companion object {
        const val TYPE = "media_file"
        private const val CODE = "invalid_media_file"
        private val ENVELOPE = setOf("v", "t", "type", "event_id", "session")

        fun parse(json: JsonObject): MediaFileFrame {
            Fields.exact(json, ENVELOPE + MediaFileRecord.FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            return MediaFileFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                file = MediaFileRecord.parse(JsonObject(json.fields.filterKeys { it !in ENVELOPE }), CODE),
            )
        }
    }
}

/** `capture_bundle`: a closed capture set with its nested media records. */
data class CaptureBundleFrame(
    val t: Long,
    val eventId: String,
    val session: String,
    val roomId: String,
    val captureId: String,
    val droneId: Int,
    val connectionEpoch: Int,
    val pattern: String,
    val coverage: String,
    val status: CaptureStatus,
    val media: List<MediaFileRecord>,
    val reason: String?,
    val detail: String?,
) {
    init {
        require(status == CaptureStatus.COMPLETED || reason != null) { "failed or unsupported bundle requires a reason" }
        require(reason == null || Fields.isMachineCode(reason)) { "bundle reason must be snake_case" }
        require(media.map { it.fileId }.distinct().size == media.size) { "media file ids must be unique" }
        require(media.size <= MAX_MEDIA_RECORDS) { "capture bundle media is limited to $MAX_MEDIA_RECORDS records" }
        require(media.all { it.captureId == captureId && it.droneId == droneId && it.connectionEpoch == connectionEpoch }) {
            "media record does not belong to this bundle"
        }
    }

    fun toEvent(): JsonObject = Json.json(
        "v" to Fields.PROTOCOL_VERSION,
        "t" to t,
        "type" to TYPE,
        "event_id" to eventId,
        "session" to session,
        "room_id" to roomId,
        "capture_id" to captureId,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "pattern" to pattern,
        "coverage" to coverage,
        "status" to status.wire,
        "media" to media.map { it.toJson() },
        "reason" to reason,
        "detail" to detail,
    )

    companion object {
        const val TYPE = "capture_bundle"
        private const val CODE = "invalid_capture_bundle"
        val PATTERNS = setOf("pano_360", "reconstruct_8", "single_still")
        val COVERAGES = setOf("full_equirectangular", "incomplete_vertical_coverage", "single_view")
        const val MAX_MEDIA_RECORDS = 8
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "room_id", "capture_id", "drone_id", "connection_epoch",
            "pattern", "coverage", "status", "media", "reason", "detail",
        )

        fun parse(json: JsonObject): CaptureBundleFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val pattern = Fields.nonEmptyString(json["pattern"], "pattern", CODE)
            if (pattern !in PATTERNS) throw ContractError(CODE, "unsupported capture pattern")
            val coverage = Fields.nonEmptyString(json["coverage"], "coverage", CODE)
            if (coverage !in COVERAGES) throw ContractError(CODE, "unsupported capture coverage")
            val status = (json["status"] as? JsonString)?.let { CaptureStatus.fromWire(it.value) }
                ?: throw ContractError(CODE, "status must be completed, unsupported, or failed")
            val mediaRaw = json["media"] as? JsonArray ?: throw ContractError(CODE, "media must be a list")
            if (mediaRaw.items.size > MAX_MEDIA_RECORDS) {
                throw ContractError(CODE, "media must contain at most $MAX_MEDIA_RECORDS records")
            }
            val captureId = Fields.nonEmptyString(json["capture_id"], "capture_id", CODE)
            val droneId = Fields.positiveInt32(json["drone_id"], "drone_id", CODE)
            val epoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", CODE)
            val media = mediaRaw.items.map { MediaFileRecord.parse(Fields.obj(it, "media", CODE), CODE) }
            if (media.any { it.captureId != captureId || it.droneId != droneId || it.connectionEpoch != epoch }) {
                throw ContractError(CODE, "media record does not belong to this bundle")
            }
            if (media.map { it.fileId }.distinct().size != media.size) {
                throw ContractError(CODE, "media file ids must be unique")
            }
            val reason = Fields.nullableString(json["reason"], "reason", CODE, machineReadable = true)
            if (status != CaptureStatus.COMPLETED && reason == null) throw ContractError(CODE, "failed or unsupported bundle requires a reason")
            return CaptureBundleFrame(
                t = Fields.nonNegativeInt(json["t"], "t", CODE),
                eventId = Fields.nonEmptyString(json["event_id"], "event_id", CODE),
                session = Fields.nonEmptyString(json["session"], "session", CODE),
                roomId = Fields.nonEmptyString(json["room_id"], "room_id", CODE),
                captureId = captureId,
                droneId = droneId,
                connectionEpoch = epoch,
                pattern = pattern,
                coverage = coverage,
                status = status,
                media = media,
                reason = reason,
                detail = Fields.nullableString(json["detail"], "detail", CODE),
            )
        }
    }
}
