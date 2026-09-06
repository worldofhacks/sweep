package org.worldofhacks.sweep.bridge

import java.io.File
import java.io.Writer
import java.nio.file.Path
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

class SensorRawSinkTest {
    @TempDir
    lateinit var temporaryDirectory: Path

    @Test
    fun `typed records carry immutable provenance and identity changes rotate UUID runs`() {
        val ids = ArrayDeque(listOf(RUN_ONE, EVENT_ONE, RUN_TWO, EVENT_TWO))
        val clock = AtomicLong(1_000)
        val sink = sink(elapsedRealtimeMs = clock::getAndIncrement, uuid = ids::removeFirst)
        val first = identity(generation = 4, epoch = 8, config = 'a')
        val second = identity(generation = 5, epoch = 9, config = 'b')

        sink.updateIdentity(first)
        assertEquals(SensorRawAppendResult.QUEUED, sink.recordVelocityNedMps(1.0, 2.0, 3.0))
        sink.updateIdentity(second)
        assertEquals(SensorRawAppendResult.QUEUED, sink.recordUltrasonicHeightDm(17))
        sink.close()

        val records = records().sortedBy { it.integer("received_at_android_elapsed_realtime_ms") }
        assertEquals(2, records.size)
        assertIdentity(records[0], first, RUN_ONE, EVENT_ONE, "phone_velocity_raw")
        assertEquals("ned", records[0].text("coordinate_frame"))
        assertEquals(1.0, records[0].number("north_mps"))
        assertEquals(2.0, records[0].number("east_mps"))
        assertEquals(3.0, records[0].number("down_mps"))
        assertIdentity(records[1], second, RUN_TWO, EVENT_TWO, "phone_height_raw")
        assertEquals("KeyUltrasonicHeight", records[1].text("sdk_key"))
        assertEquals(17L, records[1].integer("height_value"))
        assertEquals("dm", records[1].text("height_unit"))
        assertEquals(2, files().size)
        assertEquals(2L, sink.metrics().runsStarted)
    }

    @Test
    fun `barometric callback keeps the source value in metres without localization conversion`() {
        val sink = sink()
        sink.updateIdentity(identity())

        assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(1.25))
        sink.close()

        val record = records().single()
        assertEquals("KeyAltitude", record.text("sdk_key"))
        assertEquals(1.25, record.number("height_value"))
        assertEquals("m", record.text("height_unit"))
        assertFalse(record.fields.containsKey("position_map_enu_m"))
        assertFalse(record.fields.containsKey("source_capture_time_ms"))
    }

    @Test
    fun `blocked storage cannot block callbacks and queue overflow is measured`() {
        val writer = BlockingWriter()
        val sink = sink(
            limits = limits(queueCapacity = 1, batchSize = 1),
            writerFactory = { writer },
        )
        val callbacks = Executors.newSingleThreadExecutor()
        sink.updateIdentity(identity())
        try {
            assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(1.0))
            assertTrue(writer.writeStarted.await(2, TimeUnit.SECONDS))
            val admitted = callbacks.submit<SensorRawAppendResult> { sink.recordBarometricHeightM(1.1) }
            assertEquals(SensorRawAppendResult.QUEUED, admitted.get(2, TimeUnit.SECONDS))
            val dropped = callbacks.submit<SensorRawAppendResult> { sink.recordBarometricHeightM(1.2) }
            assertEquals(SensorRawAppendResult.QUEUE_FULL, dropped.get(2, TimeUnit.SECONDS))
        } finally {
            writer.release.countDown()
            sink.close()
            callbacks.shutdownNow()
        }

        assertEquals(2L, sink.metrics().queued)
        assertEquals(2L, sink.metrics().appendedToWriter)
        assertEquals(1L, sink.metrics().droppedQueueFull)
    }

    @Test
    fun `invalid scalar is rejected without poisoning a later valid record`() {
        val sink = sink()
        sink.updateIdentity(identity())

        assertEquals(SensorRawAppendResult.INVALID, sink.recordBarometricHeightM(Double.NaN))
        assertEquals(SensorRawAppendResult.INVALID, sink.recordVelocityNedMps(1.0, Double.POSITIVE_INFINITY, 0.0))
        assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(1.5))
        sink.close()

        assertEquals(2L, sink.metrics().rejectedInvalid)
        assertEquals(1L, sink.metrics().appendedToWriter)
        assertEquals(SensorRawAppendResult.CLOSED, sink.recordBarometricHeightM(2.0))
        assertTrue(records().single().text("event_id").let(::canonicalUuid))
    }

    @Test
    fun `samples are rejected until joined product identity is available`() {
        val sink = sink()

        assertEquals(SensorRawAppendResult.NO_IDENTITY, sink.recordUltrasonicHeightDm(10))
        sink.close()

        assertEquals(1L, sink.metrics().droppedWithoutIdentity)
        assertTrue(files().isEmpty())
    }

    @Test
    fun `segment and aggregate retention stay bounded across recorder restarts`() {
        repeat(3) { index ->
            File(temporaryDirectory.toFile(), "phone-raw-old-$index.jsonl").writeText("x".repeat(600))
        }
        val sink = sink(
            limits = limits(maxSegmentBytes = 1_024, maxSegments = 2, maxTotalBytes = 2_048),
        )
        sink.updateIdentity(identity())
        repeat(4) { assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(it.toDouble())) }
        sink.close()

        assertTrue(files().size <= 2)
        assertTrue(files().sumOf(File::length) <= 2_048)
        assertTrue(files().all { it.length() in 1..1_024 })
        assertTrue(sink.metrics().segmentRotations > 0)
        assertTrue(sink.metrics().retentionDeletes >= 3)
    }

    @Test
    fun `writer failure and close timeout are observable and admission stays closed`() {
        val events = mutableListOf<String>()
        val writer = StuckWriter()
        val sink = sink(
            limits = limits(batchSize = 1, closeTimeoutMs = 20),
            writerFactory = { writer },
            event = { synchronized(events) { events += it } },
        )
        sink.updateIdentity(identity())
        assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(1.0))
        assertTrue(writer.writeStarted.await(2, TimeUnit.SECONDS))

        sink.close()
        assertEquals(1L, sink.metrics().closeTimeouts)
        assertTrue(sink.metrics().workerAlive)
        assertEquals(SensorRawAppendResult.CLOSED, sink.recordBarometricHeightM(2.0))
        assertTrue(synchronized(events) { events.any { it.contains("did not stop") } })

        writer.release.countDown()
        assertTrue(writer.writeFinished.await(2, TimeUnit.SECONDS))
        eventually { !sink.metrics().workerAlive }
        assertFalse(sink.metrics().workerAlive)
    }

    @Test
    fun `invalid injected UUID is a counted writer drop rather than a dead worker`() {
        val events = mutableListOf<String>()
        val sink = sink(uuid = { "not-a-uuid" }, event = events::add)
        sink.updateIdentity(identity())

        assertEquals(SensorRawAppendResult.QUEUED, sink.recordBarometricHeightM(1.0))
        sink.close()

        assertEquals(1L, sink.metrics().writeErrors)
        assertEquals(1L, sink.metrics().droppedByWriter)
        assertEquals(0L, sink.metrics().appendedToWriter)
        assertTrue(events.any { it.contains("writer error") })
    }

    @Test
    fun `recorder configuration digest binds app variant and source contract`() {
        val fake = SensorRawSink.recorderConfigSha256("org.example", "1 (1)", "fake")
        val probe = SensorRawSink.recorderConfigSha256("org.example", "1 (1)", "dji-probe")

        assertTrue(fake.matches(Regex("[0-9a-f]{64}")))
        assertNotEquals(fake, probe)
        assertEquals(fake, SensorRawSink.recorderConfigSha256("org.example", "1 (1)", "fake"))
    }

    private fun sink(
        limits: SensorRawSink.Limits = limits(),
        elapsedRealtimeMs: () -> Long = AtomicLong(1_000)::getAndIncrement,
        uuid: () -> String = uuidSequence(),
        writerFactory: (File) -> Writer = { it.bufferedWriter(Charsets.UTF_8) },
        event: (String) -> Unit = {},
    ): SensorRawSink = SensorRawSink.testing(
        directory = temporaryDirectory.toFile(),
        limits = limits,
        elapsedRealtimeMs = elapsedRealtimeMs,
        wallClockMs = { 1_800_000_000_000 },
        uuid = uuid,
        writerFactory = writerFactory,
        event = event,
    )

    private fun files(): List<File> = temporaryDirectory.toFile().listFiles()
        .orEmpty()
        .filter { it.name.startsWith("phone-raw-") && it.name.endsWith(".jsonl") }

    private fun records(): List<JsonObject> = files().flatMap { file ->
        file.readLines().filter(String::isNotBlank).map { Json.parse(it) as JsonObject }
    }

    private fun assertIdentity(
        record: JsonObject,
        identity: SensorRawIdentity,
        runId: String,
        eventId: String,
        kind: String,
    ) {
        assertEquals(SensorRawSink.RECORD_SCHEMA_VERSION.toLong(), record.integer("record_schema_version"))
        assertEquals(kind, record.text("kind"))
        assertEquals(eventId, record.text("event_id"))
        assertEquals(runId, record.text("recording_run_id"))
        assertEquals(1L, record.integer("run_sequence"))
        assertEquals(identity.session, record.text("session"))
        assertEquals(identity.productId.toLong(), record.integer("product_id"))
        assertEquals(identity.droneId.toLong(), record.integer("drone_id"))
        assertEquals(identity.connectionGeneration, record.integer("connection_generation"))
        assertEquals(identity.connectionEpoch.toLong(), record.integer("connection_epoch"))
        assertEquals(identity.productType, record.text("product_type"))
        assertEquals(identity.aircraftFirmware, record.text("aircraft_firmware"))
        assertEquals(identity.rcFirmware, record.text("rc_firmware"))
        assertEquals(identity.sdkVersion, record.text("sdk_version"))
        assertEquals(identity.recorderConfigSha256, record.text("recorder_config_sha256"))
        assertFalse(record.fields.containsKey("flight_approved"))
    }

    private fun identity(
        generation: Long = 1,
        epoch: Int = 1,
        config: Char = 'a',
    ) = SensorRawIdentity(
        session = "sensor-recording-test",
        productId = 17,
        droneId = 1,
        connectionGeneration = generation,
        connectionEpoch = epoch,
        productType = "DJI_MINI_3",
        aircraftFirmware = "01.00.0500",
        rcFirmware = "01.02.0000",
        sdkVersion = "5.18.0",
        recorderConfigSha256 = config.toString().repeat(64),
    )

    private fun JsonObject.text(name: String) = (get(name) as JsonString).value

    private fun JsonObject.integer(name: String) = (get(name) as JsonInt).value

    private fun JsonObject.number(name: String): Double = when (val value = get(name)) {
        is JsonFloat -> value.value
        is JsonInt -> value.value.toDouble()
        else -> error("$name is not numeric")
    }

    private fun eventually(assertion: () -> Boolean) {
        repeat(100) {
            if (assertion()) return
            Thread.sleep(10)
        }
    }

    private class BlockingWriter : Writer() {
        val writeStarted = CountDownLatch(1)
        val release = CountDownLatch(1)

        override fun write(buffer: CharArray, offset: Int, length: Int) {
            writeStarted.countDown()
            check(release.await(5, TimeUnit.SECONDS)) { "test did not release writer" }
        }

        override fun flush() = Unit

        override fun close() = Unit
    }

    private class StuckWriter : Writer() {
        val writeStarted = CountDownLatch(1)
        val writeFinished = CountDownLatch(1)
        val release = CountDownLatch(1)

        override fun write(buffer: CharArray, offset: Int, length: Int) {
            writeStarted.countDown()
            try {
                while (!release.await(20, TimeUnit.MILLISECONDS)) {
                    Thread.yield()
                }
            } catch (_: InterruptedException) {
                while (release.count > 0) Thread.yield()
            } finally {
                writeFinished.countDown()
            }
        }

        override fun flush() = Unit

        override fun close() = Unit
    }

    private companion object {
        const val RUN_ONE = "11111111-1111-4111-8111-111111111111"
        const val EVENT_ONE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        const val RUN_TWO = "22222222-2222-4222-8222-222222222222"
        const val EVENT_TWO = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

        fun limits(
            queueCapacity: Int = 16,
            batchSize: Int = 8,
            maxSegmentBytes: Long = 4_096,
            maxSegments: Int = 8,
            maxTotalBytes: Long = 32_768,
            closeTimeoutMs: Long = 2_000,
        ) = SensorRawSink.Limits(
            queueCapacity = queueCapacity,
            batchSize = batchSize,
            flushIntervalMs = 10,
            maxSegmentBytes = maxSegmentBytes,
            maxSegments = maxSegments,
            maxTotalBytes = maxTotalBytes,
            closeTimeoutMs = closeTimeoutMs,
        )

        fun uuidSequence(): () -> String {
            val sequence = AtomicLong()
            return {
                UUID.nameUUIDFromBytes("sensor-test-${sequence.incrementAndGet()}".toByteArray()).toString()
            }
        }

        fun canonicalUuid(value: String): Boolean = runCatching { UUID.fromString(value).toString() == value }.getOrDefault(false)
    }
}
