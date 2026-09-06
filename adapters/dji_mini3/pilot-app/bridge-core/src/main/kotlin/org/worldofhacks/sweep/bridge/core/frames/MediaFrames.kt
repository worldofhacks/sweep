package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
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

/** `relay.contracts.MediaFileRecord`: the `MediaFile` fields without the transport envelope. */
data class MediaFileRecord(
    val captureId: String,
    val fileId: String,
    val timestampMs: Long,
    val droneId: Int,
    val connectionEpoch: Int,
    val pose: WirePose,
    val actualYawDeg: Double,
    val gimbalPitchDeg: Double,
    val intrinsics: WireIntrinsics,
    val checksumSha256: String,
    val storageRef: String,
    val retrievalStatus: RetrievalStatus,
) {
    init {
        require(isChecksum(checksumSha256)) { "checksum_sha256 must be 64 lowercase hex characters" }
        require(retrievalStatus != RetrievalStatus.PENDING || checksumSha256 == PENDING_CHECKSUM) {
            "pending media requires the all-zero checksum sentinel"
        }
        require(retrievalStatus != RetrievalStatus.COMPLETED || checksumSha256 != PENDING_CHECKSUM) {
            "completed media requires a content checksum"
        }
    }

    fun toJson(): JsonObject = Json.json(
        "capture_id" to captureId,
        "file_id" to fileId,
        "timestamp_ms" to timestampMs,
        "drone_id" to droneId,
        "connection_epoch" to connectionEpoch,
        "pose" to pose.toJson(),
        "actual_yaw_deg" to actualYawDeg,
        "gimbal_pitch_deg" to gimbalPitchDeg,
        "intrinsics" to intrinsics.toJson(),
        "checksum_sha256" to checksumSha256,
        "storage_ref" to storageRef,
        "retrieval_status" to retrievalStatus.wire,
    )

    companion object {
        /** The checksum of a `pending` record: no bytes have been hashed yet. */
        const val PENDING_CHECKSUM = "0000000000000000000000000000000000000000000000000000000000000000"
        val FIELDS = setOf(
            "capture_id", "file_id", "timestamp_ms", "drone_id", "connection_epoch", "pose", "actual_yaw_deg",
            "gimbal_pitch_deg", "intrinsics", "checksum_sha256", "storage_ref", "retrieval_status",
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
            return MediaFileRecord(
                captureId = Fields.nonEmptyString(json["capture_id"], "capture_id", code),
                fileId = Fields.nonEmptyString(json["file_id"], "file_id", code),
                timestampMs = Fields.nonNegativeInt(json["timestamp_ms"], "timestamp_ms", code),
                droneId = Fields.positiveInt32(json["drone_id"], "drone_id", code),
                connectionEpoch = Fields.positiveInt32(json["connection_epoch"], "connection_epoch", code),
                pose = WirePose.parse(Fields.obj(json["pose"], "pose", code), code),
                actualYawDeg = Fields.finiteNumber(json["actual_yaw_deg"], "actual_yaw_deg", code),
                gimbalPitchDeg = Fields.finiteNumber(json["gimbal_pitch_deg"], "gimbal_pitch_deg", code),
                intrinsics = WireIntrinsics.parse(Fields.obj(json["intrinsics"], "intrinsics", code), code),
                checksumSha256 = checksum,
                storageRef = Fields.nonEmptyString(json["storage_ref"], "storage_ref", code),
                retrievalStatus = status,
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
        require(media.size <= MAX_MEDIA_RECORDS) { "capture bundle media is limited to $MAX_MEDIA_RECORDS records" }
        require(media.map { it.fileId }.toSet().size == media.size) { "media may not contain duplicate file_id values" }
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
        val PATTERNS = setOf("pano_360", "reconstruct_8")
        val COVERAGES = setOf("full_equirectangular", "incomplete_vertical_coverage")
        const val MAX_MEDIA_RECORDS = 8
        private val FIELDS = setOf(
            "v", "t", "type", "event_id", "session", "room_id", "capture_id", "drone_id", "connection_epoch",
            "pattern", "coverage", "status", "media", "reason", "detail",
        )

        fun parse(json: JsonObject): CaptureBundleFrame {
            Fields.exact(json, FIELDS, CODE)
            Fields.envelope(json, TYPE, CODE)
            val pattern = Fields.nonEmptyString(json["pattern"], "pattern", CODE)
            if (pattern !in PATTERNS) throw ContractError(CODE, "pattern must be pano_360 or reconstruct_8")
            val coverage = Fields.nonEmptyString(json["coverage"], "coverage", CODE)
            if (coverage !in COVERAGES) throw ContractError(CODE, "coverage must be full_equirectangular or incomplete_vertical_coverage")
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
            if (media.map { it.fileId }.toSet().size != media.size) {
                throw ContractError(CODE, "media may not contain duplicate file_id values")
            }
            if (media.any { it.captureId != captureId || it.droneId != droneId || it.connectionEpoch != epoch }) {
                throw ContractError(CODE, "media record does not belong to this bundle")
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
