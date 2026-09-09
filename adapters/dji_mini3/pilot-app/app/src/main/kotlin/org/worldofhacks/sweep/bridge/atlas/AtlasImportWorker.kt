package org.worldofhacks.sweep.bridge.atlas

import android.content.Context
import android.content.Intent
import androidx.core.net.toUri
import androidx.work.BackoffPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.TimeUnit

/** Persisted picker access -> private, checksummed original -> existing upload worker. */
class AtlasImportWorker(context: Context, parameters: WorkerParameters) : Worker(context, parameters) {
    @Volatile private var source: InputStream? = null
    override fun onStopped() { runCatching { source?.close() } }
    override fun doWork(): Result {
        val id = inputData.getString("upload_id") ?: return Result.failure()
        val item = AtlasOutbox.get(applicationContext).get(id) ?: return Result.success()
        // Recover process death between committing the private copy and scheduling its upload.
        if (item.state == "queued" || item.state == "uploading") {
            AtlasUploadWorker.enqueue(applicationContext, id)
            return Result.success()
        }
        if (item.state != "importing") return Result.success()
        val result = copyOriginal(item)
        if (result == Result.success()) AtlasUploadWorker.enqueue(applicationContext, id)
        return result
    }

    /** Tests exercise this same bounded ContentResolver stream and native SQLite finalization. */
    internal fun copyOriginal(item: AtlasUpload): Result {
        val queue = AtlasOutbox.get(applicationContext)
        try {
            require(item.state == "importing" && item.importUri != null && item.checksum == null)
            val uri = item.importUri.toUri()
            require(uri.scheme == "content")
            val input = applicationContext.contentResolver.openInputStream(uri)
                ?: throw FileNotFoundException("The selected file is no longer available. Choose it again.")
            source = input
            input.use { from ->
                // A resumed import starts from the source, never appends to an incomplete copy.
                queue.file(item).outputStream().use { to ->
                    val buffer = ByteArray(64 * 1024)
                    var copied = 0L
                    var reported = 0L
                    while (true) {
                        if (isStopped) throw IOException("Import paused")
                        val read = from.read(buffer)
                        if (read < 0) break
                        require(copied + read <= AtlasOutbox.MAX_BYTES) { "Choose a file under 64 MB. The selected source is unchanged." }
                        to.write(buffer, 0, read)
                        copied += read
                        if (copied - reported >= 256 * 1024) {
                            queue.state(item.id, "importing", sent = copied)
                            reported = copied
                        }
                    }
                }
            }
            if (isStopped) return Result.retry()
            queue.finish(item.id)
            releaseUnusedPermission(applicationContext, item.importUri)
            return Result.success()
        } catch (error: Exception) {
            if (isStopped) return Result.retry()
            val transient = error is IOException && error !is FileNotFoundException && runAttemptCount < 5
            queue.state(item.id, if (transient) "importing" else "failed", when {
                error is SecurityException || error is FileNotFoundException ->
                    "Access to the selected file is unavailable. Choose it again; its source is unchanged."
                transient -> "Copy paused. Android will retry; the selected source is unchanged."
                else -> error.message ?: "The import could not finish. Choose the file again."
            })
            return if (transient) Result.retry() else Result.failure()
        } finally { source = null }
    }

    companion object {
        fun enqueue(context: Context, id: String, retryNow: Boolean = false) {
            val request = OneTimeWorkRequestBuilder<AtlasImportWorker>()
                .setInputData(workDataOf("upload_id" to id))
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).build()
            // No network constraint: local documents can be copied while offline.
            WorkManager.getInstance(context).enqueueUniqueWork("atlas-import-$id",
                if (retryNow) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP, request)
        }
        fun releaseUnusedPermission(context: Context, uri: String?) {
            if (uri == null) return
            val queue = AtlasOutbox.get(context)
            synchronized(queue) {
                if (queue.usesImportUri(uri)) return
                runCatching { context.contentResolver.releasePersistableUriPermission(
                    uri.toUri(), Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            }
        }
    }
}
