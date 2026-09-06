package org.worldofhacks.sweep.bridge

import java.security.MessageDigest

/** Immutable provenance copied onto every sample admitted in one recording context. */
internal data class SensorRawIdentity(
    val session: String,
    val productId: Int,
    val droneId: Int,
    val connectionGeneration: Long,
    val connectionEpoch: Int,
    val productType: String,
    val aircraftFirmware: String,
    val rcFirmware: String,
    val sdkVersion: String,
    val recorderConfigSha256: String,
) {
    init {
        require(canonicalText(session, 512)) { "sensor session is invalid" }
        require(productId >= 0) { "sensor product id is invalid" }
        require(droneId > 0) { "sensor drone id is invalid" }
        require(connectionGeneration > 0) { "sensor product generation is invalid" }
        require(connectionEpoch > 0) { "sensor connection epoch is invalid" }
        require(listOf(productType, aircraftFirmware, rcFirmware, sdkVersion).all { canonicalText(it, 256) }) {
            "sensor product provenance is incomplete or unbounded"
        }
        require(recorderConfigSha256.matches(Regex("[0-9a-f]{64}"))) {
            "sensor recorder config must be lowercase SHA-256 hex"
        }
    }
}

internal data class SensorRawMetrics(
    val queued: Long,
    val appendedToWriter: Long,
    val rejectedInvalid: Long,
    val droppedWithoutIdentity: Long,
    val droppedQueueFull: Long,
    val droppedByWriter: Long,
    val writeErrors: Long,
    val runsStarted: Long,
    val segmentRotations: Long,
    val retentionDeletes: Long,
    val closeTimeouts: Long,
    val admissionClosed: Boolean,
    val workerAlive: Boolean,
) {
    fun summary(): String =
        "queued=$queued, writer_appends=$appendedToWriter, invalid=$rejectedInvalid, " +
            "no_identity=$droppedWithoutIdentity, queue_full=$droppedQueueFull, writer_drops=$droppedByWriter, " +
            "write_errors=$writeErrors, runs=$runsStarted, rotations=$segmentRotations, " +
            "retention_deletes=$retentionDeletes, close_timeouts=$closeTimeouts, worker_alive=$workerAlive"
}

internal enum class SensorRawAppendResult { QUEUED, INVALID, NO_IDENTITY, QUEUE_FULL, CLOSED }

/** Typed callback boundary; implementations must not perform disk I/O on the SDK callback. */
internal interface SensorRawRecorder {
    fun recordVelocityNedMps(northMps: Double, eastMps: Double, downMps: Double): SensorRawAppendResult

    fun recordBarometricHeightM(heightM: Double): SensorRawAppendResult

    fun recordUltrasonicHeightDm(heightDm: Int): SensorRawAppendResult

    fun recordAircraftAttitudeDegrees(
        yawDeg: Double,
        pitchDeg: Double,
        rollDeg: Double,
    ): SensorRawAppendResult

    fun recordGimbalAttitudeDegrees(
        yawDeg: Double,
        pitchDeg: Double,
        rollDeg: Double,
    ): SensorRawAppendResult

    companion object {
        val NONE = object : SensorRawRecorder {
            override fun recordVelocityNedMps(
                northMps: Double,
                eastMps: Double,
                downMps: Double,
            ) = SensorRawAppendResult.NO_IDENTITY

            override fun recordBarometricHeightM(heightM: Double) = SensorRawAppendResult.NO_IDENTITY

            override fun recordUltrasonicHeightDm(heightDm: Int) = SensorRawAppendResult.NO_IDENTITY

            override fun recordAircraftAttitudeDegrees(
                yawDeg: Double,
                pitchDeg: Double,
                rollDeg: Double,
            ) = SensorRawAppendResult.NO_IDENTITY

            override fun recordGimbalAttitudeDegrees(
                yawDeg: Double,
                pitchDeg: Double,
                rollDeg: Double,
            ) = SensorRawAppendResult.NO_IDENTITY
        }
    }
}

internal object SensorRawConfiguration {
    const val SCHEMA_VERSION = 3

    fun sha256(applicationId: String, appVersion: String, aircraftVariant: String): String {
        val config = listOf(
            "record_schema_version=$SCHEMA_VERSION",
            "application_id=$applicationId",
            "app_version=$appVersion",
            "aircraft_variant=$aircraftVariant",
            "velocity_source=KeyAircraftVelocity:ned:mps",
            "barometric_height_source=KeyAltitude:m",
            "ultrasonic_height_source=KeyUltrasonicHeight:dm",
            "aircraft_attitude_source=KeyAircraftAttitude:degrees",
            "gimbal_attitude_source=KeyGimbalAttitude:degrees",
            "timing=android_callback_receipt_elapsed_realtime_ms",
        ).joinToString("\n")
        return MessageDigest.getInstance("SHA-256")
            .digest(config.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }
}

private fun canonicalText(value: String, maxCodePoints: Int): Boolean =
    value.isNotEmpty() &&
        value == value.trim() &&
        value.codePointCount(0, value.length) <= maxCodePoints &&
        value.codePoints().allMatch { !Character.isISOControl(it) && Character.getType(it) != Character.FORMAT.toInt() }

internal fun boundedSensorProvenance(value: String?): String =
    value?.takeIf { canonicalText(it, 256) } ?: "unreported"
