package org.worldofhacks.sweep.bridge.session

import java.io.File
import java.io.FileInputStream
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import org.worldofhacks.sweep.bridge.core.json.Json

internal interface RawEvidenceSession {
    fun exportRawEvidence(): ExportResult
}

internal object RawEvidenceExport {
    private const val SCHEMA_VERSION = 1

    fun write(
        filesDir: File,
        probeReport: String,
        exportedAtMs: Long,
        onMemberCopyStarted: (File) -> Unit = {},
    ): ExportResult {
        var temporary: File? = null
        return try {
            val exports = File(filesDir, "evidence-exports").apply {
                check(mkdirs() || isDirectory) { "could not create evidence export directory" }
            }
            val destination = File(exports, "raw-evidence-$exportedAtMs.zip")
            check(!destination.exists()) { "evidence export already exists" }
            temporary = File(exports, "raw-evidence-$exportedAtMs.zip.tmp")
            check(!temporary.exists()) { "evidence export is already in progress" }
            val members = evidenceFiles(filesDir)
            val reportBytes = probeReport.toByteArray(Charsets.UTF_8)
            ZipOutputStream(temporary.outputStream().buffered()).use { zip ->
                writeEntry(zip, "probe-report.txt", reportBytes)
                val snapshots = members.map { member -> copyMember(zip, member, onMemberCopyStarted) }
                val manifest = Json.canonical(
                    Json.json(
                        "evidence_export_schema_version" to SCHEMA_VERSION,
                        "exported_at_epoch_ms" to exportedAtMs,
                        "raw_record_schema_version" to 3,
                        "raw_time_basis" to "android_callback_receipt_elapsed_realtime_ms",
                        "raw_source_timestamp_status" to "not_provided_by_msdk_key_listener",
                        "camera_presentation_time_source" to "StreamInfo.presentationTimeMs",
                        "camera_decode_time_status" to "not_exposed_by_receive_stream_listener",
                        "files" to listOf(
                            Json.json(
                                "path" to "probe-report.txt",
                                "bytes" to reportBytes.size,
                                "sha256" to sha256(reportBytes),
                            ),
                        ) + snapshots.map { snapshot ->
                            Json.json(
                                "path" to snapshot.zipPath,
                                "bytes" to snapshot.bytes,
                                "sha256" to snapshot.sha256,
                            )
                        },
                    ),
                )
                writeEntry(zip, "provenance.json", manifest.toByteArray(Charsets.UTF_8))
            }
            check(temporary.renameTo(destination)) { "could not finalize evidence export" }
            temporary = null
            ExportResult.Saved(destination.absolutePath)
        } catch (error: Throwable) {
            temporary?.delete()
            ExportResult.Failed(error.message ?: error.javaClass.simpleName)
        }
    }

    private fun evidenceFiles(filesDir: File): List<Member> = listOf("sensor-records", "bench")
        .flatMap { directory -> filesIn(File(filesDir, directory), directory) }
        .sortedBy(Member::zipPath)

    private fun filesIn(directory: File, prefix: String): List<Member> {
        if (!directory.isDirectory) return emptyList()
        val root = directory.canonicalFile
        return directory.walkTopDown()
            .filter { candidate ->
                candidate.isFile && !java.nio.file.Files.isSymbolicLink(candidate.toPath()) &&
                    candidate.canonicalFile.toPath().startsWith(root.toPath())
            }
            .map { candidate ->
                Member(candidate, "$prefix/${candidate.canonicalFile.relativeTo(root).invariantSeparatorsPath}")
            }
            .toList()
    }

    private fun copyMember(zip: ZipOutputStream, member: Member, onMemberCopyStarted: (File) -> Unit): Snapshot {
        val limit = completeJsonlPrefix(member.file)
        onMemberCopyStarted(member.file)
        val digest = MessageDigest.getInstance("SHA-256")
        var bytes = 0L
        zip.putNextEntry(ZipEntry(member.zipPath))
        try {
            FileInputStream(member.file).use { input ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var remaining = limit
                while (remaining > 0) {
                    val count = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
                    if (count < 0) break
                    zip.write(buffer, 0, count)
                    digest.update(buffer, 0, count)
                    bytes += count
                    remaining -= count
                }
            }
        } finally {
            zip.closeEntry()
        }
        return Snapshot(member.zipPath, bytes, digest.digest().hex())
    }

    private fun completeJsonlPrefix(file: File): Long {
        val length = file.length()
        if (!file.name.endsWith(".jsonl")) return length
        RandomAccessFile(file, "r").use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            var offset = length
            while (offset > 0) {
                val start = maxOf(0, offset - buffer.size)
                input.seek(start)
                val count = input.read(buffer, 0, (offset - start).toInt())
                for (index in count - 1 downTo 0) {
                    if (buffer[index] == '\n'.code.toByte()) return start + index + 1
                }
                offset = start
            }
        }
        return 0
    }

    private fun writeEntry(zip: ZipOutputStream, path: String, bytes: ByteArray) {
        zip.putNextEntry(ZipEntry(path))
        zip.write(bytes)
        zip.closeEntry()
    }

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes)
        .hex()

    private fun ByteArray.hex(): String = joinToString("") { byte -> "%02x".format(byte) }

    private data class Member(val file: File, val zipPath: String)

    private data class Snapshot(val zipPath: String, val bytes: Long, val sha256: String)
}
