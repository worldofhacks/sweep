package org.worldofhacks.sweep.bridge

import android.os.SystemClock
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.io.Writer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import org.worldofhacks.sweep.bridge.core.json.Json

class SensorRawSink private constructor(
    val file: File,
    private val writer: Writer,
    private val nowMs: () -> Long,
    queueCapacity: Int,
) : AutoCloseable {
    data class Statistics(val accepted: Long, val written: Long, val dropped: Long)

    private data class Record(val kind: String, val fields: Map<String, Any>, val receivedAtMs: Long)

    private val queue = ArrayBlockingQueue<Record>(queueCapacity)
    private val acceptanceLock = Any()
    private val accepted = AtomicLong()
    private val written = AtomicLong()
    private val dropped = AtomicLong()

    @Volatile
    private var closed = false

    private val worker = Thread(::drain, "sensor-raw-log").apply {
        isDaemon = true
        start()
    }

    val statistics: Statistics
        get() = Statistics(accepted.get(), written.get(), dropped.get())

    fun append(kind: String, fields: Map<String, Any>): Boolean {
        val record = Record(kind, fields.toMap(), nowMs())
        synchronized(acceptanceLock) {
            if (closed || !queue.offer(record)) {
                dropped.incrementAndGet()
                return false
            }
            accepted.incrementAndGet()
            return true
        }
    }

    override fun close() {
        synchronized(acceptanceLock) { closed = true }
        try {
            worker.join()
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private fun drain() {
        var sequence = 0L
        var bytesWritten = file.length()
        var dirty = false
        var recordsSinceFlush = 0
        var unavailable = false
        try {
            while (!closed || queue.isNotEmpty()) {
                val record = queue.poll(FLUSH_INTERVAL_MS, TimeUnit.MILLISECONDS)
                if (record == null) {
                    if (dirty) {
                        flush()
                        dirty = false
                        recordsSinceFlush = 0
                    }
                    continue
                }
                if (unavailable) {
                    dropped.incrementAndGet()
                    continue
                }
                val line = try {
                    Json.canonical(
                        Json.json(
                            "kind" to record.kind,
                            "event_id" to "${record.kind}-${sequence + 1}",
                            "received_at_monotonic_ms" to record.receivedAtMs,
                            *record.fields.toList().toTypedArray(),
                        ),
                    )
                } catch (_: Exception) {
                    dropped.incrementAndGet()
                    continue
                }
                val bytes = line.toByteArray(Charsets.UTF_8).size + 1
                if (bytesWritten + bytes > MAX_BYTES) {
                    unavailable = true
                    dropped.incrementAndGet()
                    continue
                }
                try {
                    writer.append(line).append('\n')
                    sequence += 1
                    bytesWritten += bytes
                    written.incrementAndGet()
                    dirty = true
                    recordsSinceFlush += 1
                    if (recordsSinceFlush == FLUSH_BATCH_SIZE) {
                        flush()
                        dirty = false
                        recordsSinceFlush = 0
                    }
                } catch (_: Exception) {
                    unavailable = true
                    dropped.incrementAndGet()
                }
            }
            if (dirty) flush()
        } finally {
            synchronized(acceptanceLock) { closed = true }
            while (queue.poll() != null) dropped.incrementAndGet()
            runCatching { writer.close() }
        }
    }

    private fun flush() {
        try {
            writer.flush()
        } catch (_: Exception) {
            dropped.incrementAndGet()
        }
    }

    companion object {
        private const val MAX_BYTES = 16 * 1024 * 1024
        private const val QUEUE_CAPACITY = 256
        private const val FLUSH_BATCH_SIZE = 64
        private const val FLUSH_INTERVAL_MS = 1_000L

        fun open(filesDir: File): SensorRawSink? = runCatching {
            val directory = File(filesDir, "sensor-records").apply { mkdirs() }
            val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
            val file = File.createTempFile("phone-raw-$stamp-", ".jsonl", directory)
            SensorRawSink(file, BufferedWriter(FileWriter(file, true)), SystemClock::elapsedRealtime, QUEUE_CAPACITY)
        }.getOrNull()

        internal fun create(file: File, writer: Writer, nowMs: () -> Long, queueCapacity: Int): SensorRawSink {
            require(queueCapacity > 0) { "queueCapacity must be positive" }
            return SensorRawSink(file, writer, nowMs, queueCapacity)
        }
    }
}
