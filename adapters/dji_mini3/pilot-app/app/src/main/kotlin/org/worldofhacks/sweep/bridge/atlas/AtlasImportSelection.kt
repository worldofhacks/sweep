package org.worldofhacks.sweep.bridge.atlas

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import org.json.JSONObject

/** Destination is snapshotted before opening the picker, including across activity recreation. */
internal data class AtlasImportTarget(val session: String, val space: String, val contributor: String, val name: String,
    val request: AtlasCaptureRequest? = null) {
    fun json() = JSONObject().put("session", session).put("spaceId", space)
        .put("contributor", contributor).put("name", name).put("request", request?.json())
    companion object {
        fun parse(value: JSONObject): AtlasImportTarget {
            val contributor = value.getString("contributor")
            require(contributor.matches(Regex("[a-zA-Z0-9_-]{8,64}")))
            return AtlasImportTarget(value.getString("session"), value.getString("spaceId"),
                contributor, value.optString("name", "Contributor").take(40), AtlasCaptureRequest.fromPayload(value))
        }
    }
}

internal object AtlasImportSelection {
    const val MAX_SELECTION = 10
    fun picker() = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
        .setType("*/*").putExtra(Intent.EXTRA_MIME_TYPES, AtlasOutbox.IMPORT_MIMES.toTypedArray())
        .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)

    fun admit(context: Context, session: AtlasSession, target: AtlasImportTarget, uri: Uri): AtlasUpload {
        require(session.id == target.session) { "The original workspace connection is unavailable." }
        session.endpoint(target.space)
        require(uri.scheme == "content") { "Choose a document using Android's file picker." }
        val resolver = context.contentResolver
        val mime = resolver.getType(uri)?.lowercase()
        require(mime in AtlasOutbox.IMPORT_MIMES) { "Choose JPEG, PNG, WebP, MP4 or WebM. Other formats are not supported yet." }
        val label = runCatching {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else ""
            }.orEmpty()
        }.getOrDefault("")
        // No camera, GPS or broad photo-library permission is requested. Only
        // these user-selected documents are readable after app/process restart.
        val queue = AtlasOutbox.get(context)
        synchronized(queue) {
            resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            try {
                return queue.beginImport(session, target.space, uri.toString(), mime!!,
                    target.contributor, target.name, label, target.request)
            } catch (error: Exception) {
                AtlasImportWorker.releaseUnusedPermission(context, uri.toString())
                throw error
            }
        }
    }
}
