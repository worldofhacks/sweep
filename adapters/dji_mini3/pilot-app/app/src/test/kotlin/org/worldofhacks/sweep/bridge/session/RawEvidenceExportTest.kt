package org.worldofhacks.sweep.bridge.session

import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.zip.ZipFile
import kotlin.io.path.createTempDirectory
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

class RawEvidenceExportTest {
    @Test
    fun `export includes phone records stream evidence report and explicit timing provenance`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            File(filesDir, "sensor-records").apply { mkdirs() }
                .resolve("phone-raw-a.jsonl")
                .writeText("{\"kind\":\"phone_velocity_raw\"}\n")
            File(filesDir, "bench").apply { mkdirs() }
                .resolve("stream-a.jsonl")
                .writeText("{\"kind\":\"video_frame\"}\n")

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)
            val zip = File((result as ExportResult.Saved).path)
            ZipFile(zip).use { archive ->
                assertEquals(
                    listOf("probe-report.txt", "bench/stream-a.jsonl", "sensor-records/phone-raw-a.jsonl", "provenance.json"),
                    archive.entries().asSequence().map { it.name }.toList(),
                )
                assertEquals("probe report", archive.getInputStream(archive.getEntry("probe-report.txt")).reader().readText())
                val provenance = archive.getInputStream(archive.getEntry("provenance.json")).reader().readText()
                assertTrue(provenance.contains("android_callback_receipt_elapsed_realtime_ms"))
                assertTrue(provenance.contains("not_provided_by_msdk_key_listener"))
                assertTrue(provenance.contains("StreamInfo.presentationTimeMs"))
                assertTrue(provenance.contains("not_exposed_by_receive_stream_listener"))
                assertTrue(provenance.contains("probe-report.txt"))
            }
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export manifests the exact complete jsonl snapshot while records continue`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        val complete = "{\"kind\":\"phone_velocity_raw\"}\n".toByteArray()
        val source = File(filesDir, "sensor-records/phone-raw-a.jsonl")
        try {
            source.parentFile!!.mkdirs()
            source.writeBytes(complete + "{\"kind\":\"partial".toByteArray())

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234) { file ->
                if (file == source) source.appendText("}\n{\"kind\":\"later\"}\n")
            }
            ZipFile(File((result as ExportResult.Saved).path)).use { archive ->
                val copied = archive.getInputStream(archive.getEntry("sensor-records/phone-raw-a.jsonl")).readBytes()
                assertEquals(complete.toList(), copied.toList())
                val provenance = Json.parse(
                    archive.getInputStream(archive.getEntry("provenance.json")).reader().readText(),
                ) as JsonObject
                val files = provenance["files"] as JsonArray
                val record = files.items
                    .map { it as JsonObject }
                    .single { (it["path"] as JsonString).value == "sensor-records/phone-raw-a.jsonl" }
                assertEquals(complete.size.toLong(), (record["bytes"] as JsonInt).value)
                assertEquals(sha256(copied), (record["sha256"] as JsonString).value)
            }
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export removes its incomplete zip when a source copy fails`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            File(filesDir, "sensor-records").apply { mkdirs() }
                .resolve("phone-raw-a.jsonl")
                .writeText("{\"kind\":\"phone_velocity_raw\"}\n")

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234) {
                error("source copy failed")
            }
            assertTrue(result is ExportResult.Failed)
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip").exists())
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip.tmp").exists())
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export fails rather than manifesting a source truncated during its snapshot`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        val source = File(filesDir, "sensor-records/phone-raw-a.jsonl")
        try {
            source.parentFile!!.mkdirs()
            source.writeText("{\"kind\":\"phone_velocity_raw\"}\n")

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234) { file ->
                if (file == source) source.writeText("")
            }

            assertTrue(result is ExportResult.Failed)
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip").exists())
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip.tmp").exists())
        } finally {
            filesDir.deleteRecursively()
        }
    }

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .joinToString("") { byte -> "%02x".format(byte) }

    @Test
    fun `export excludes files outside evidence directories`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            File(filesDir, "unrelated.txt").writeText("not evidence")
            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)
            ZipFile(File((result as ExportResult.Saved).path)).use { archive ->
                assertFalse(archive.entries().asSequence().any { it.name == "unrelated.txt" })
            }
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export admits only bounded jsonl evidence members`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            val records = File(filesDir, "sensor-records").apply { mkdirs() }
            repeat(65) { index ->
                records.resolve("phone-raw-$index.jsonl").writeText("{}\n")
            }

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)

            assertTrue(result is ExportResult.Failed)
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip").exists())
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `API 24 compatible path checks retain nested evidence and exclude linked files and sibling directories`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            val records = File(filesDir, "sensor-records").apply { mkdirs() }
            val nested = records.resolve("nested").apply { mkdirs() }.resolve("valid.jsonl").apply { writeText("{}\n") }
            val sibling = File(filesDir, "sensor-records-other").apply { mkdirs() }
            sibling.resolve("outside.jsonl").writeText("{}\n")
            java.nio.file.Files.createSymbolicLink(records.resolve("linked.jsonl").toPath(), nested.toPath())
            java.nio.file.Files.createSymbolicLink(records.resolve("sibling").toPath(), sibling.toPath())
            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)
            ZipFile(File((result as ExportResult.Saved).path)).use { archive ->
                assertEquals(listOf("probe-report.txt", "sensor-records/nested/valid.jsonl", "provenance.json"),
                    archive.entries().asSequence().map { it.name }.toList())
            }
        } finally {
            // Remove test-created links before recursive fixture cleanup can follow them.
            File(filesDir, "sensor-records/linked.jsonl").delete()
            File(filesDir, "sensor-records/sibling").delete()
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export refuses an oversized source before copying it`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            val source = File(filesDir, "sensor-records/phone-raw-a.jsonl")
            source.parentFile!!.mkdirs()
            RandomAccessFile(source, "rw").use { output ->
                output.seek(64L * 1024 * 1024)
                output.write('\n'.code)
            }

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)

            assertTrue(result is ExportResult.Failed)
            assertFalse(File(filesDir, "evidence-exports/raw-evidence-1234.zip").exists())
        } finally {
            filesDir.deleteRecursively()
        }
    }

    @Test
    fun `export refuses a fifth archive without deleting prior evidence`() {
        val filesDir = createTempDirectory("raw-evidence").toFile()
        try {
            val exports = File(filesDir, "evidence-exports").apply { mkdirs() }
            val retained = (1..4).map { index ->
                exports.resolve("raw-evidence-$index.zip").apply { writeText("evidence-$index") }
            }

            val result = RawEvidenceExport.write(filesDir, "probe report", exportedAtMs = 1234)

            assertTrue(result is ExportResult.Failed)
            assertTrue(retained.all(File::isFile))
            assertFalse(File(exports, "raw-evidence-1234.zip").exists())
        } finally {
            filesDir.deleteRecursively()
        }
    }
}
