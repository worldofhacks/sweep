package org.worldofhacks.sweep.bridge

import android.os.SystemClock
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.worldofhacks.sweep.bridge.core.json.Json

class SensorRawSink private constructor(val file: File, private val writer: BufferedWriter) {
    private var sequence = 0L
    private var bytesWritten = file.length()
    private var full = false

    @Synchronized
    fun append(kind: String, fields: Map<String, Any>): Boolean {
        if (full) return false
        sequence += 1
        val line = Json.canonical(
            Json.json(
                "kind" to kind,
                "event_id" to "$kind-$sequence",
                "received_at_monotonic_ms" to SystemClock.elapsedRealtime(),
                *fields.toList().toTypedArray(),
            ),
        )
        val bytes = line.toByteArray(Charsets.UTF_8).size + 1
        if (bytesWritten + bytes > MAX_BYTES) {
            full = true
            return false
        }
        writer.append(line).append('\n')
        writer.flush()
        bytesWritten += bytes
        return true
    }

    companion object {
        private const val MAX_BYTES = 16 * 1024 * 1024

        fun open(filesDir: File): SensorRawSink? = runCatching {
            val directory = File(filesDir, "sensor-records").apply { mkdirs() }
            val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
            val file = File(directory, "phone-raw-$stamp.jsonl")
            SensorRawSink(file, BufferedWriter(FileWriter(file, true)))
        }.getOrNull()
    }
}
