package org.worldofhacks.sweep.bridge.publish

import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import org.worldofhacks.sweep.bridge.bench.BenchRecorder
import org.worldofhacks.sweep.bridge.core.admission.SystemClock

/**
 * One JSONL bench file per publish session under `filesDir/bench/`, written through the
 * bench module's recorder so `BenchAnalysis` reads it back. Flushed on every record so an
 * `adb pull` mid-run sees the windows so far.
 */
class BenchSink private constructor(val file: File, private val writer: BufferedWriter) {
    val recorder = BenchRecorder(FlushingAppendable(writer), SystemClock)

    fun close() {
        runCatching { writer.flush() }
        runCatching { writer.close() }
    }

    private class FlushingAppendable(private val writer: BufferedWriter) : Appendable {
        override fun append(csq: CharSequence?): Appendable {
            writer.append(csq)
            if (csq != null && csq.endsWith("\n")) writer.flush()
            return this
        }

        override fun append(csq: CharSequence?, start: Int, end: Int): Appendable {
            writer.append(csq, start, end)
            return this
        }

        override fun append(c: Char): Appendable {
            writer.append(c)
            if (c == '\n') writer.flush()
            return this
        }
    }

    companion object {
        fun open(filesDir: File, droneId: Int): BenchSink? = runCatching {
            val directory = File(filesDir, "bench").apply { mkdirs() }
            val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
            val file = File(directory, "publish-drone$droneId-$stamp.jsonl")
            BenchSink(file, BufferedWriter(FileWriter(file, true)))
        }.getOrNull()
    }
}
