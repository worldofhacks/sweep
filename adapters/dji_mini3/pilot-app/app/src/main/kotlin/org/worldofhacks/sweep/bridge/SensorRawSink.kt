package org.worldofhacks.sweep.bridge

import android.os.SystemClock
import java.io.BufferedWriter
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStreamWriter
import java.io.Writer
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import org.worldofhacks.sweep.bridge.core.json.Json

/**
 * Bounded asynchronous recorder for the raw values delivered by DJI callbacks.
 *
 * The timestamp is Android callback receipt time, not a sensor capture timestamp. Callback
 * threads only validate scalars and offer an immutable record to [queue]; JSON, file I/O,
 * flushes, segment rotation, and retention happen on [worker].
 */
internal class SensorRawSink private constructor(
    private val directory: File,
    private val limits: Limits,
    private val elapsedRealtimeMs: () -> Long,
    private val wallClockMs: () -> Long,
    private val uuid: () -> String,
    private val writerFactory: (File) -> Writer,
    private val event: (String) -> Unit,
) : SensorRawRecorder, AutoCloseable {
    internal data class Limits(
        val queueCapacity: Int = 256,
        val batchSize: Int = 64,
        val flushIntervalMs: Long = 250,
        val maxSegmentBytes: Long = 4L * 1024 * 1024,
        val maxSegments: Int = 8,
        val maxTotalBytes: Long = 32L * 1024 * 1024,
        val closeTimeoutMs: Long = 2_000,
    ) {
        init {
            require(queueCapacity in 1..MAX_QUEUE_CAPACITY)
            require(batchSize in 1..queueCapacity)
            require(flushIntervalMs in 1..MAX_FLUSH_INTERVAL_MS)
            require(maxSegmentBytes in 1..MAX_SEGMENT_BYTES)
            require(maxSegments in 1..MAX_SEGMENTS)
            require(maxTotalBytes in maxSegmentBytes..MAX_TOTAL_BYTES)
            require(closeTimeoutMs in 1..MAX_CLOSE_TIMEOUT_MS)
        }

        private companion object {
            const val MAX_QUEUE_CAPACITY = 4_096
            const val MAX_FLUSH_INTERVAL_MS = 60_000L
            const val MAX_SEGMENT_BYTES = 64L * 1024 * 1024
            const val MAX_SEGMENTS = 64
            const val MAX_TOTAL_BYTES = 512L * 1024 * 1024
            const val MAX_CLOSE_TIMEOUT_MS = 30_000L
        }
    }

    private sealed interface Sample {
        val kind: String

        data class Velocity(val northMps: Double, val eastMps: Double, val downMps: Double) : Sample {
            override val kind = "phone_velocity_raw"
        }

        data class BarometricHeight(val heightM: Double) : Sample {
            override val kind = "phone_height_raw"
        }

        data class UltrasonicHeight(val heightDm: Int) : Sample {
            override val kind = "phone_height_raw"
        }

        data class AircraftAttitude(val yawDeg: Double, val pitchDeg: Double, val rollDeg: Double) : Sample {
            override val kind = "phone_attitude_raw"
        }

        data class GimbalAttitude(val yawDeg: Double, val pitchDeg: Double, val rollDeg: Double) : Sample {
            override val kind = "phone_attitude_raw"
        }
    }

    private data class Pending(
        val identity: SensorRawIdentity,
        val sample: Sample,
        val receivedAtMonotonicMs: Long,
    )

    private val admission = Any()
    private val accepting = AtomicBoolean(true)
    private val identity = AtomicReference<SensorRawIdentity?>(null)
    private val queue = ArrayBlockingQueue<Pending>(limits.queueCapacity)
    private val queued = AtomicLong()
    private val appendedToWriter = AtomicLong()
    private val rejectedInvalid = AtomicLong()
    private val droppedWithoutIdentity = AtomicLong()
    private val droppedQueueFull = AtomicLong()
    private val droppedByWriter = AtomicLong()
    private val writeErrors = AtomicLong()
    private val runsStarted = AtomicLong()
    private val segmentRotations = AtomicLong()
    private val retentionDeletes = AtomicLong()
    private val closeTimeouts = AtomicLong()
    private val closeTimeoutReported = AtomicBoolean(false)
    private val worker = Thread(::writeLoop, "sweep-sensor-raw").apply {
        isDaemon = true
        start()
    }

    fun updateIdentity(next: SensorRawIdentity?) {
        synchronized(admission) {
            if (accepting.get()) identity.set(next)
        }
    }

    override fun recordVelocityNedMps(
        northMps: Double,
        eastMps: Double,
        downMps: Double,
    ): SensorRawAppendResult {
        if (!northMps.isFinite() || !eastMps.isFinite() || !downMps.isFinite()) return invalid()
        return append(Sample.Velocity(northMps, eastMps, downMps))
    }

    override fun recordBarometricHeightM(heightM: Double): SensorRawAppendResult {
        if (!heightM.isFinite()) return invalid()
        return append(Sample.BarometricHeight(heightM))
    }

    override fun recordUltrasonicHeightDm(heightDm: Int): SensorRawAppendResult =
        append(Sample.UltrasonicHeight(heightDm))

    override fun recordAircraftAttitudeDegrees(
        yawDeg: Double,
        pitchDeg: Double,
        rollDeg: Double,
    ): SensorRawAppendResult = recordAttitude(
        yawDeg,
        pitchDeg,
        rollDeg,
        Sample.AircraftAttitude(yawDeg, pitchDeg, rollDeg),
    )

    override fun recordGimbalAttitudeDegrees(
        yawDeg: Double,
        pitchDeg: Double,
        rollDeg: Double,
    ): SensorRawAppendResult = recordAttitude(
        yawDeg,
        pitchDeg,
        rollDeg,
        Sample.GimbalAttitude(yawDeg, pitchDeg, rollDeg),
    )

    fun metrics(): SensorRawMetrics = SensorRawMetrics(
        queued.get(),
        appendedToWriter.get(),
        rejectedInvalid.get(),
        droppedWithoutIdentity.get(),
        droppedQueueFull.get(),
        droppedByWriter.get(),
        writeErrors.get(),
        runsStarted.get(),
        segmentRotations.get(),
        retentionDeletes.get(),
        closeTimeouts.get(),
        !accepting.get(),
        worker.isAlive,
    )

    override fun close() {
        synchronized(admission) {
            accepting.set(false)
            identity.set(null)
        }
        if (Thread.currentThread() === worker || !worker.isAlive) return
        try {
            worker.join(limits.closeTimeoutMs)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
        if (worker.isAlive) {
            worker.interrupt()
            if (closeTimeoutReported.compareAndSet(false, true)) {
                closeTimeouts.incrementAndGet()
                notify("writer did not stop within ${limits.closeTimeoutMs} ms; recorder remains closed")
            }
        }
    }

    private fun invalid(): SensorRawAppendResult {
        rejectedInvalid.incrementAndGet()
        return SensorRawAppendResult.INVALID
    }

    private fun recordAttitude(
        yawDeg: Double,
        pitchDeg: Double,
        rollDeg: Double,
        sample: Sample,
    ): SensorRawAppendResult {
        if (!yawDeg.isFinite() || !pitchDeg.isFinite() || !rollDeg.isFinite()) return invalid()
        return append(sample)
    }

    private fun append(sample: Sample): SensorRawAppendResult = synchronized(admission) {
        if (!accepting.get()) return@synchronized SensorRawAppendResult.CLOSED
        val current = identity.get()
        if (current == null) {
            droppedWithoutIdentity.incrementAndGet()
            return@synchronized SensorRawAppendResult.NO_IDENTITY
        }
        if (!queue.offer(Pending(current, sample, elapsedRealtimeMs()))) {
            droppedQueueFull.incrementAndGet()
            SensorRawAppendResult.QUEUE_FULL
        } else {
            queued.incrementAndGet()
            SensorRawAppendResult.QUEUED
        }
    }

    private fun writeLoop() {
        var activeIdentity: SensorRawIdentity? = null
        var activeRunId: String? = null
        var runSequence = 0L
        var segment = 0
        var writer: Writer? = null
        var bytesInSegment = 0L
        var dirtyRecords = 0
        var reportedInvalid = 0L
        var reportedNoIdentity = 0L
        var reportedQueueFull = 0L

        fun reportDrops() {
            reportedInvalid = reportMilestone(rejectedInvalid.get(), reportedInvalid, "invalid samples rejected")
            reportedNoIdentity = reportMilestone(
                droppedWithoutIdentity.get(),
                reportedNoIdentity,
                "samples dropped without a complete recording identity",
            )
            reportedQueueFull = reportMilestone(
                droppedQueueFull.get(),
                reportedQueueFull,
                "samples dropped because the recording queue was full",
            )
        }

        fun closeWriter() {
            val current = writer
            writer = null
            bytesInSegment = 0
            dirtyRecords = 0
            if (current != null) {
                runCatching { current.flush() }.onFailure(::recordWriteError)
                runCatching { current.close() }.onFailure(::recordWriteError)
            }
            runCatching { enforceRetention(active = null, reservedBytes = 0) }.onFailure(::recordWriteError)
        }

        fun beginRun(next: SensorRawIdentity): Boolean = try {
            val nextRunId = canonicalUuid(uuid())
            closeWriter()
            activeIdentity = next
            activeRunId = nextRunId
            runSequence = 0
            segment = 0
            runsStarted.incrementAndGet()
            true
        } catch (error: Throwable) {
            recordWriteError(error)
            false
        }

        fun openWriter(): Boolean {
            val currentRun = activeRunId ?: return false
            val candidate = File(
                directory,
                "$FILE_PREFIX${wallClockMs()}-$currentRun-s${segment.toString().padStart(3, '0')}$FILE_SUFFIX",
            )
            segment += 1
            var created = false
            return try {
                created = candidate.createNewFile()
                check(created) { "sensor raw filename collision" }
                writer = writerFactory(candidate)
                bytesInSegment = 0
                enforceRetention(active = candidate, reservedBytes = limits.maxSegmentBytes)
                notify("recording run $currentRun to ${candidate.absolutePath}")
                true
            } catch (error: Throwable) {
                runCatching { writer?.close() }
                writer = null
                if (created && !candidate.delete()) notify("could not remove incomplete segment ${candidate.name}")
                recordWriteError(error)
                false
            }
        }

        fun line(pending: Pending, runId: String, sequence: Long): String {
            val sampleFields: Array<Pair<String, Any>> = when (val sample = pending.sample) {
                is Sample.Velocity -> arrayOf(
                    "sdk_key" to "KeyAircraftVelocity",
                    "coordinate_frame" to "ned",
                    "north_mps" to sample.northMps,
                    "east_mps" to sample.eastMps,
                    "down_mps" to sample.downMps,
                )
                is Sample.BarometricHeight -> arrayOf(
                    "sdk_key" to "KeyAltitude",
                    "height_value" to sample.heightM,
                    "height_unit" to "m",
                )
                is Sample.UltrasonicHeight -> arrayOf(
                    "sdk_key" to "KeyUltrasonicHeight",
                    "height_value" to sample.heightDm,
                    "height_unit" to "dm",
                )
                is Sample.AircraftAttitude -> arrayOf(
                    "sdk_key" to "KeyAircraftAttitude",
                    "attitude_frame" to "aircraft_body_to_ned",
                    "yaw_deg" to sample.yawDeg,
                    "pitch_deg" to sample.pitchDeg,
                    "roll_deg" to sample.rollDeg,
                )
                is Sample.GimbalAttitude -> arrayOf(
                    "sdk_key" to "KeyGimbalAttitude",
                    "attitude_frame" to "raw_sdk_axes",
                    "yaw_deg" to sample.yawDeg,
                    "pitch_deg" to sample.pitchDeg,
                    "roll_deg" to sample.rollDeg,
                )
            }
            return Json.canonical(
                Json.json(
                    "record_schema_version" to RECORD_SCHEMA_VERSION,
                    "kind" to pending.sample.kind,
                    "event_id" to canonicalUuid(uuid()),
                    "recording_run_id" to runId,
                    "run_sequence" to sequence,
                    "session" to pending.identity.session,
                    "product_id" to pending.identity.productId,
                    "drone_id" to pending.identity.droneId,
                    "connection_generation" to pending.identity.connectionGeneration,
                    "connection_epoch" to pending.identity.connectionEpoch,
                    "product_type" to pending.identity.productType,
                    "aircraft_firmware" to pending.identity.aircraftFirmware,
                    "rc_firmware" to pending.identity.rcFirmware,
                    "sdk_version" to pending.identity.sdkVersion,
                    "recorder_config_sha256" to pending.identity.recorderConfigSha256,
                    "time_basis" to "android_callback_receipt_elapsed_realtime_ms",
                    "source_timestamp_status" to "not_provided_by_msdk_key_listener",
                    "received_at_android_elapsed_realtime_ms" to pending.receivedAtMonotonicMs,
                    "written_at_android_elapsed_realtime_ms" to elapsedRealtimeMs(),
                    *sampleFields,
                ),
            )
        }

        fun write(pending: Pending) {
            if (pending.identity != activeIdentity && !beginRun(pending.identity)) {
                droppedByWriter.incrementAndGet()
                return
            }
            val currentRun = checkNotNull(activeRunId)
            val nextSequence = runSequence + 1
            val encoded = try {
                line(pending, currentRun, nextSequence)
            } catch (error: Throwable) {
                recordWriteError(error)
                droppedByWriter.incrementAndGet()
                return
            }
            val byteCount = encoded.toByteArray(Charsets.UTF_8).size + 1L
            if (byteCount > limits.maxSegmentBytes) {
                recordWriteError(IllegalArgumentException("one sensor record exceeds the segment bound"))
                droppedByWriter.incrementAndGet()
                return
            }
            if (writer == null && !openWriter()) {
                droppedByWriter.incrementAndGet()
                return
            }
            if (bytesInSegment + byteCount > limits.maxSegmentBytes) {
                closeWriter()
                segmentRotations.incrementAndGet()
                if (!openWriter()) {
                    droppedByWriter.incrementAndGet()
                    return
                }
            }
            try {
                checkNotNull(writer).append(encoded).append('\n')
                runSequence = nextSequence
                bytesInSegment += byteCount
                dirtyRecords += 1
                appendedToWriter.incrementAndGet()
                if (dirtyRecords >= limits.batchSize) {
                    writer?.flush()
                    dirtyRecords = 0
                }
            } catch (error: Throwable) {
                recordWriteError(error)
                droppedByWriter.incrementAndGet()
                closeWriter()
            }
        }

        try {
            while (accepting.get() || queue.isNotEmpty()) {
                reportDrops()
                val first = try {
                    queue.poll(limits.flushIntervalMs, TimeUnit.MILLISECONDS)
                } catch (_: InterruptedException) {
                    null
                }
                if (first == null) {
                    if (queue.isEmpty() && identity.get() != activeIdentity) {
                        closeWriter()
                        activeIdentity = null
                        activeRunId = null
                    } else if (dirtyRecords > 0) {
                        try {
                            writer?.flush()
                            dirtyRecords = 0
                        } catch (error: Throwable) {
                            recordWriteError(error)
                            closeWriter()
                        }
                    }
                    continue
                }
                val batch = ArrayList<Pending>(limits.batchSize)
                batch += first
                queue.drainTo(batch, limits.batchSize - 1)
                batch.forEach(::write)
            }
        } finally {
            reportDrops()
            closeWriter()
        }
    }

    /** Reserve a complete future segment so total retention remains bounded while it grows. */
    private fun enforceRetention(active: File?, reservedBytes: Long) {
        val files = directory.listFiles { candidate ->
            candidate.isFile && candidate.name.startsWith(FILE_PREFIX) && candidate.name.endsWith(FILE_SUFFIX)
        }?.sortedWith(compareBy<File>({ it.lastModified() }, { it.name }))?.toMutableList()
            ?: throw IllegalStateException("could not inspect sensor raw retention directory")
        var bytes = files.sumOf(File::length)
        while (files.size > limits.maxSegments || bytes + reservedBytes > limits.maxTotalBytes) {
            val oldest = files.firstOrNull { it != active }
                ?: throw IllegalStateException("sensor raw retention limits cannot admit an active segment")
            files.remove(oldest)
            val oldBytes = oldest.length()
            check(oldest.delete()) { "could not remove expired sensor raw segment ${oldest.name}" }
            bytes -= oldBytes
            retentionDeletes.incrementAndGet()
        }
    }

    private fun recordWriteError(error: Throwable) {
        val count = writeErrors.incrementAndGet()
        if (powerOfTwo(count)) notify("sensor raw writer error ${error::class.simpleName} (count $count)")
    }

    private fun reportMilestone(count: Long, reported: Long, detail: String): Long {
        val milestone = java.lang.Long.highestOneBit(count)
        if (milestone > reported) notify("$detail (count $count)")
        return maxOf(reported, milestone)
    }

    private fun notify(detail: String) {
        runCatching { event(detail) }
    }

    companion object {
        const val RECORD_SCHEMA_VERSION = SensorRawConfiguration.SCHEMA_VERSION
        private const val FILE_PREFIX = "phone-raw-"
        private const val FILE_SUFFIX = ".jsonl"

        fun open(filesDir: File, event: (String) -> Unit = {}): SensorRawSink? = runCatching {
            val directory = File(filesDir, "sensor-records")
            check(directory.mkdirs() || directory.isDirectory) { "could not create sensor-records" }
            SensorRawSink(
                directory,
                Limits(),
                SystemClock::elapsedRealtime,
                System::currentTimeMillis,
                { UUID.randomUUID().toString() },
                ::fileWriter,
                event,
            )
        }.getOrElse {
            runCatching { event("could not initialize sensor raw recorder: ${it::class.simpleName}") }
            null
        }

        internal fun testing(
            directory: File,
            limits: Limits,
            elapsedRealtimeMs: () -> Long,
            wallClockMs: () -> Long = { 1L },
            uuid: () -> String = { UUID.randomUUID().toString() },
            writerFactory: (File) -> Writer = ::fileWriter,
            event: (String) -> Unit = {},
        ): SensorRawSink {
            check(directory.mkdirs() || directory.isDirectory)
            return SensorRawSink(directory, limits, elapsedRealtimeMs, wallClockMs, uuid, writerFactory, event)
        }

        fun recorderConfigSha256(
            applicationId: String,
            appVersion: String,
            aircraftVariant: String,
        ): String = SensorRawConfiguration.sha256(applicationId, appVersion, aircraftVariant)

        private fun fileWriter(file: File): Writer = BufferedWriter(
            OutputStreamWriter(FileOutputStream(file, false), Charsets.UTF_8),
        )

        private fun canonicalUuid(raw: String): String {
            val parsed = UUID.fromString(raw)
            require(parsed.toString() == raw) { "recording identifiers must be canonical UUIDs" }
            return raw
        }

        private fun powerOfTwo(value: Long): Boolean = value > 0 && value and (value - 1) == 0L
    }
}
