package org.worldofhacks.sweep.bridge.atlas

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** One immutable capture per job. WorkManager reschedules it after network loss/process death. */
class AtlasUploadWorker(context: Context, parameters: WorkerParameters) : Worker(context, parameters) {
    @Volatile private var activeCall: Call? = null
    override fun onStopped() { activeCall?.cancel() }
    override fun doWork(): Result {
        val id = inputData.getString("upload_id") ?: return Result.failure()
        val queue = AtlasOutbox.get(applicationContext)
        val item = queue.get(id) ?: return Result.success()
        if (item.state !in setOf("queued", "uploading")) return Result.success()
        val session = try {
            AtlasVault(applicationContext).load(item.sessionId)
                ?: error("The invitation is missing. Reconnect before retrying this capture.")
        } catch (error: Exception) {
            queue.state(id, "failed", error.message ?: "Reconnect before retrying this capture.")
            return Result.failure()
        }
        return deliver(item, session)
    }
    /** Shared by the worker and native HTTP acceptance tests; still enforces the row's credential. */
    internal fun deliver(item: AtlasUpload, session: AtlasSession): Result {
        val queue = AtlasOutbox.get(applicationContext)
        val id = item.id
        try {
            check(session.id == item.sessionId) { "This capture belongs to another invitation." }
            val file = queue.file(item)
            check(file.isFile && file.length() == item.size && AtlasOutbox.checksum(file) == item.checksum) {
                "The local capture failed its checksum check. It has not been uploaded."
            }
            queue.state(id, "uploading", sent = 0)
            val body = object : RequestBody() {
                override fun contentType() = item.mime.toMediaType()
                override fun contentLength() = item.size
                override fun writeTo(sink: BufferedSink) {
                    file.inputStream().use { input ->
                        val buffer = ByteArray(64 * 1024); var sent = 0L; var lastUpdate = 0L
                        while (true) {
                            if (isStopped) throw IOException("Upload paused")
                            val read = input.read(buffer); if (read < 0) break
                            sink.write(buffer, 0, read); sent += read
                            if (!isStopped && (sent - lastUpdate >= 256 * 1024 || sent == item.size)) {
                                queue.state(id, "uploading", sent = sent); lastUpdate = sent
                            }
                        }
                    }
                }
            }
            val request = Request.Builder().url(session.endpoint(item.spaceId, "captures"))
                .header("Authorization", "Bearer ${session.token}")
                .header("X-Sweep-Capture", asciiJson(item.metadata)).post(body).build()
            activeCall = HTTP.newCall(request)
            activeCall!!.execute().use { response ->
                if (!response.isSuccessful) AtlasMetadataCache(queue).accessRefused(session, item.spaceId, response.code)
                val raw = response.peekBody(64 * 1024).string()
                val result = runCatching { JSONObject(raw) }.getOrNull()
                if (isStopped) return Result.failure()
                if (!response.isSuccessful) {
                    val message = result?.optString("detail")?.takeIf { it.isNotBlank() }
                        ?: "The workspace refused the upload (${response.code})."
                    val retry = response.code in 500..599 || response.code == 408 || response.code == 429
                    queue.state(id, if (retry) "queued" else "failed", message)
                    return if (retry) Result.retry() else Result.failure()
                }
                check(result?.optString("sha256") == item.checksum) { "The saved capture checksum did not match. Keep the local copy and retry." }
                JSONObject(item.metadata).optJSONObject("response_to")?.let { expected ->
                    check(AtlasCaptureRequest.matches(expected, result.optJSONObject("response_to"))) {
                        "The upload was not confirmed against this request. Keep the original and retry."
                    }
                }
                queue.state(id, "saved", sent = item.size)
            }
            return Result.success()
        } catch (error: Exception) {
            if (isStopped) return Result.failure()
            val retry = error is IOException
            queue.state(id, if (retry) "queued" else "failed",
                if (retry) "Waiting for a connection. Your capture is saved on this device." else error.message ?: "The capture could not be sent.")
            return if (retry) Result.retry() else Result.failure()
        } finally { activeCall = null }
    }
    companion object {
        private val HTTP = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
            .connectTimeout(15, TimeUnit.SECONDS).callTimeout(90, TimeUnit.SECONDS).build()
        fun enqueue(context: Context, id: String, retryNow: Boolean = false) {
            val work = OneTimeWorkRequestBuilder<AtlasUploadWorker>()
                .setInputData(workDataOf("upload_id" to id))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS).build()
            WorkManager.getInstance(context).enqueueUniqueWork("atlas-upload-$id",
                if (retryNow) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP, work)
        }
        fun asciiJson(value: String) = buildString {
            value.forEach { character ->
                if (character.code > 127) append("\\u%04x".format(character.code)) else append(character)
            }
        }
    }
}
