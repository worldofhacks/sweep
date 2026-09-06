package org.worldofhacks.sweep.bridge

import java.io.File
import java.io.Writer
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class SensorRawSinkTest {
    @Test
    fun `callback returns while the raw sensor writer is blocked`() {
        val writer = BlockingWriter()
        val sink = sink(writer)
        val callbacks = Executors.newSingleThreadExecutor()
        try {
            val callback = callbacks.submit<Boolean> { sink.append("phone_height_raw", mapOf("height_m" to 1.5)) }
            assertTrue(writer.writeStarted.await(2, TimeUnit.SECONDS))
            assertTrue(callback.get(200, TimeUnit.MILLISECONDS))
        } finally {
            writer.unblock.countDown()
            sink.close()
            sink.file.delete()
            callbacks.shutdownNow()
        }
    }

    @Test
    fun `bounded queue counts samples dropped while disk is blocked`() {
        val writer = BlockingWriter()
        val sink = sink(writer, queueCapacity = 1)
        try {
            assertTrue(sink.append("phone_height_raw", mapOf("height_m" to 1.5)))
            assertTrue(writer.writeStarted.await(2, TimeUnit.SECONDS))
            assertTrue(sink.append("phone_height_raw", mapOf("height_m" to 1.6)))
            assertFalse(sink.append("phone_height_raw", mapOf("height_m" to 1.7)))
            assertEquals(1L, sink.statistics.dropped)
        } finally {
            writer.unblock.countDown()
            sink.close()
            sink.file.delete()
        }
    }

    @Test
    fun `close drains the queue and flushes a partial batch`() {
        val writer = RecordingWriter()
        val sink = sink(writer)
        try {
            assertTrue(sink.append("phone_height_raw", mapOf("height_m" to 1.5)))
        } finally {
            sink.close()
        }
        assertTrue(writer.text.contains("phone_height_raw"))
        assertEquals(1, writer.flushes)
        assertEquals(1L, sink.statistics.written)
        sink.file.delete()
    }

    @Test
    fun `invalid sensor values do not stop later recording`() {
        val writer = RecordingWriter()
        val sink = sink(writer)
        try {
            assertTrue(sink.append("phone_height_raw", mapOf("height_m" to Double.NaN)))
            assertTrue(sink.append("phone_height_raw", mapOf("height_m" to 1.5)))
        } finally {
            sink.close()
        }
        assertEquals(1L, sink.statistics.written)
        assertEquals(1L, sink.statistics.dropped)
        assertTrue(writer.text.contains("1.5"))
        assertFalse(sink.append("phone_height_raw", mapOf("height_m" to 2.0)))
        sink.file.delete()
    }

    private fun sink(writer: Writer, queueCapacity: Int = 4): SensorRawSink =
        SensorRawSink.create(
            file = Files.createTempFile("sensor-raw", ".jsonl").toFile(),
            writer = writer,
            nowMs = { 1_000L },
            queueCapacity = queueCapacity,
        )

    private class BlockingWriter : Writer() {
        val writeStarted = CountDownLatch(1)
        val unblock = CountDownLatch(1)

        override fun write(buffer: CharArray, offset: Int, length: Int) {
            writeStarted.countDown()
            check(unblock.await(5, TimeUnit.SECONDS)) { "test did not release the writer" }
        }

        override fun flush() = Unit

        override fun close() = Unit
    }

    private class RecordingWriter : Writer() {
        private val buffer = StringBuilder()
        var flushes = 0
            private set

        val text: String
            get() = buffer.toString()

        override fun write(chars: CharArray, offset: Int, length: Int) {
            buffer.append(chars, offset, length)
        }

        override fun flush() {
            flushes += 1
        }

        override fun close() = Unit
    }
}
