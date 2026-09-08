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
    private const val MAX_MEMBERS = 64
    private const val MAX_MEMBER_BYTES = 64L * 1024 * 1024
    private const val MAX_SOURCE_BYTES = 128L * 1024 * 1024
    private const val MAX_PROBE_REPORT_BYTES = 1024 * 1024
    private const val MAX_COMPLETED_EXPORTS = 4

    fun write(
        filesDir: File,
        probeReport: String,
        exportedAtMs: Long,
        onMemberCopyStarted: (File) -> Unit = {},
    ): ExportResult {
        var temporary: File? = null
        return try {
            require(exportedAtMs >= 0) { "evidence export time is invalid" }
            val exports = File(filesDir, "evidence-exports").apply {
                check(mkdirs() || isDirectory) { "could not create evidence export directory" }
            }
            val completedExports = exports.listFiles { candidate ->
                candidate.isFile && candidate.name.startsWith("raw-evidence-") && candidate.name.endsWith(".zip")
            } ?: throw IllegalStateException("could not inspect completed evidence exports")
            check(completedExports.size < MAX_COMPLETED_EXPORTS) {
                "evidence export limit reached; share and remove an older ZIP first"
            }
            val destination = File(exports, "raw-evidence-$exportedAtMs.zip")
            check(!destination.exists()) { "evidence export already exists" }
            temporary = File(exports, "raw-evidence-$exportedAtMs.zip.tmp")
            check(!temporary.exists()) { "evidence export is already in progress" }
            val reportBytes = probeReport.toByteArray(Charsets.UTF_8)
            require(reportBytes.size <= MAX_PROBE_REPORT_BYTES) { "probe report exceeds the evidence export bound" }
            val members = evidenceFiles(filesDir)
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
        } catch (error: Exception) {
            temporary?.delete()
            ExportResult.Failed(error.message ?: error.javaClass.simpleName)
        }
    }

    private fun evidenceFiles(filesDir: File): List<Member> {
        val candidates = listOf("sensor-records", "bench")
            .flatMap { directory -> filesIn(File(filesDir, directory), directory) }
            .sortedBy(Member::zipPath)
        require(candidates.size <= MAX_MEMBERS) { "evidence export has too many source files" }
        var total = 0L
        return candidates.map { member ->
            val limit = completeJsonlPrefix(member.file)
            require(limit <= MAX_MEMBER_BYTES) { "evidence source ${member.zipPath} exceeds the per-file bound" }
            total = try {
                Math.addExact(total, limit)
            } catch (_: ArithmeticException) {
                throw IllegalArgumentException("evidence export source size overflow")
            }
            require(total <= MAX_SOURCE_BYTES) { "evidence export exceeds the total source bound" }
            member.copy(limit = limit)
        }
    }

    private fun filesIn(directory: File, prefix: String): List<Member> {
        if (!directory.isDirectory) return emptyList()
        val root = directory.canonicalFile
        return directory.walkTopDown().maxDepth(2)
            .filter { candidate ->
                // java.nio.file requires API 26; the pilot app also supports API 24/25.
                // Canonicalize the parent separately to reject file symlinks, then enforce
                // a separator-delimited boundary so sibling directories cannot match.
                candidate.isFile && candidate.name.endsWith(".jsonl") &&
                    candidate.canonicalFile == File(candidate.parentFile!!.canonicalFile, candidate.name) &&
                    candidate.canonicalPath.startsWith(root.path + File.separator)
            }
            .map { candidate ->
                Member(candidate, "$prefix/${candidate.canonicalFile.relativeTo(root).invariantSeparatorsPath}")
            }
            .take(MAX_MEMBERS + 1)
            .toList()
    }

    private fun copyMember(zip: ZipOutputStream, member: Member, onMemberCopyStarted: (File) -> Unit): Snapshot {
        onMemberCopyStarted(member.file)
        val digest = MessageDigest.getInstance("SHA-256")
        var bytes = 0L
        zip.putNextEntry(ZipEntry(member.zipPath))
        try {
            FileInputStream(member.file).use { input ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var remaining = member.limit
                while (remaining > 0) {
                    val count = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
                    if (count < 0) break
                    zip.write(buffer, 0, count)
                    digest.update(buffer, 0, count)
                    bytes += count
                    remaining -= count
                }
                check(remaining == 0L) { "evidence source changed while it was copied" }
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

    private data class Member(val file: File, val zipPath: String, val limit: Long = 0)

    private data class Snapshot(val zipPath: String, val bytes: Long, val sha256: String)
}
