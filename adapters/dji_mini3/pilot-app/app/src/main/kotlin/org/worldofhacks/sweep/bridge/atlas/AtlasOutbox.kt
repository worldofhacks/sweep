package org.worldofhacks.sweep.bridge.atlas

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import androidx.core.net.toUri
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.UUID

data class AtlasUpload(val id: String, val sessionId: String, val spaceId: String,
    val state: String, val metadata: String, val mime: String, val size: Long,
    val sent: Long, val checksum: String?, val error: String, val createdAt: Long,
    val importUri: String? = null, val displayName: String = "") {
    fun summary() = JSONObject().put("id", id).put("spaceId", spaceId).put("state", state)
        .put("kind", JSONObject(metadata).optString("kind")).put("bytes", size).put("sent", sent)
        .put("error", error).put("createdAt", createdAt)
        .put("source", JSONObject(metadata).optString("source"))
        .put("finalized", checksum != null)
        .put("displayName", displayName)
        .put("responseTo", JSONObject(metadata).optJSONObject("response_to"))
}

/** Files and queue rows survive activity/process death. Tokens never enter this database. */
class AtlasOutbox private constructor(context: Context) : SQLiteOpenHelper(context, "atlas-outbox.db", null, 2) {
    val root = File(context.filesDir, "atlas-captures").apply { mkdirs() }
    private val cacheGenerations = mutableMapOf<String, Long>()
    init {
        setWriteAheadLoggingEnabled(true)
        // This singleton initializes once per process, before any new camera capture.
        writableDatabase.execSQL("UPDATE uploads SET state='failed', error='Capture was interrupted before finalization. The local file is retained.' WHERE state='capturing'")
    }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE uploads (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, space_id TEXT NOT NULL, " +
            "state TEXT NOT NULL, metadata TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, " +
            "sent INTEGER NOT NULL DEFAULT 0, checksum TEXT, error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, import_uri TEXT, display_name TEXT NOT NULL DEFAULT '')")
        db.execSQL("CREATE TABLE cached_spaces (session_id TEXT, space_id TEXT, data TEXT, PRIMARY KEY(session_id,space_id))")
    }
    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) {
        if (old < 2) {
            db.execSQL("ALTER TABLE uploads ADD COLUMN import_uri TEXT")
            db.execSQL("ALTER TABLE uploads ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        }
    }
    fun file(item: AtlasUpload): File = File(root, item.id + when (item.mime) {
        "video/mp4" -> ".mp4"
        "video/webm" -> ".webm"
        "image/png" -> ".png"
        "image/webp" -> ".webp"
        else -> ".jpg"
    })
    fun get(id: String): AtlasUpload? = readableDatabase.rawQuery("SELECT * FROM uploads WHERE id=?", arrayOf(id)).use { cursor ->
        if (!cursor.moveToFirst()) null else read(cursor)
    }
    fun list(): List<AtlasUpload> = readableDatabase.rawQuery("SELECT * FROM uploads ORDER BY created_at DESC LIMIT 100", null).use { cursor ->
        buildList { while (cursor.moveToNext()) add(read(cursor)) }
    }
    private fun read(cursor: android.database.Cursor): AtlasUpload {
        fun string(key: String) = cursor.getString(cursor.getColumnIndexOrThrow(key))
        fun number(key: String) = cursor.getLong(cursor.getColumnIndexOrThrow(key))
        return AtlasUpload(string("id"), string("session_id"), string("space_id"), string("state"),
            string("metadata"), string("mime"), number("size"), number("sent"), string("checksum"),
            string("error"), number("created_at"), string("import_uri"), string("display_name"))
    }
    @Synchronized fun begin(session: AtlasSession, spaceId: String, metadata: JSONObject): AtlasUpload {
        return insert(session, spaceId, metadata,
            if (metadata.getString("kind") == "video") "video/mp4" else "image/jpeg", null)
    }
    @Synchronized fun beginImport(session: AtlasSession, spaceId: String, uri: String,
        mime: String, contributor: String, name: String, displayName: String = "", request: AtlasCaptureRequest? = null): AtlasUpload {
        require(uri.toUri().scheme == "content") { "Choose a file with Android's file picker." }
        require(mime in IMPORT_MIMES) { "Choose JPEG, PNG, WebP, MP4 or WebM. Other formats are not supported yet." }
        require(contributor.matches(Regex("[a-zA-Z0-9_-]{8,64}")))
        // Neither the picker time, a file's modification date nor today's GPS is
        // evidence of when or where an older image was captured.
        val metadata = JSONObject().put("contributor_id", contributor).put("name", name.take(40).ifBlank { "Contributor" })
            .put("kind", if (mime.startsWith("video/")) "video" else "photo")
            .put("source", "import").put("captured_at", JSONObject.NULL).put("position", JSONObject.NULL).put("note", "")
            .put("response_to", request?.target())
        return insert(session, spaceId, metadata, mime, uri,
            displayName.replace(Regex("[\\p{Cntrl}/\\\\]"), "_").take(160))
    }
    private fun insert(session: AtlasSession, spaceId: String, metadata: JSONObject, mime: String, uri: String?, displayName: String = ""): AtlasUpload {
        session.endpoint(spaceId) // Scope admission before creating any file.
        require(spaceId.matches(Regex("[a-zA-Z0-9_-]{1,64}")))
        require(list().size < 100) { "Your capture queue is full. Remove saved copies before capturing more." }
        reserveCapacity()
        val id = UUID.randomUUID().toString()
        writableDatabase.insertOrThrow("uploads", null, ContentValues().apply {
            put("id", id); put("session_id", session.id); put("space_id", spaceId)
            put("state", if (uri == null) "capturing" else "importing"); put("metadata", metadata.toString())
            put("mime", mime); put("import_uri", uri)
            put("display_name", displayName)
            put("created_at", System.currentTimeMillis())
        })
        return get(id)!!
    }
    private fun reserveCapacity(except: String? = null) {
        val pending = list().filter { it.id != except && it.state in setOf("capturing", "importing") }
        // One file-size snapshot: a concurrent copy must not grow between the
        // used-byte and reservation calculations and accidentally free capacity.
        val sizes = root.listFiles().orEmpty().associate { it.name to it.length() }
        val used = sizes.values.sum()
        val reserved = pending.sumOf { (MAX_BYTES - (sizes[file(it).name] ?: 0L)).coerceAtLeast(0L) }
        val replacing = except?.let { get(it)?.let(::file)?.name?.let(sizes::get) } ?: 0L
        require(used - replacing + reserved + MAX_BYTES <= 1024L * 1024 * 1024) {
            "Your capture queue is full. Remove saved copies before capturing more."
        }
    }
    @Synchronized fun retryImport(id: String) {
        val item = get(id) ?: error("This import no longer exists.")
        require(item.state == "failed" && item.importUri != null && item.checksum == null)
        reserveCapacity(id)
        state(id, "importing", sent = 0)
    }
    fun usesImportUri(uri: String) = readableDatabase.rawQuery(
        "SELECT 1 FROM uploads WHERE import_uri=? LIMIT 1", arrayOf(uri)).use { it.moveToFirst() }
    fun captureMetadata(id: String, metadata: JSONObject) {
        writableDatabase.update("uploads", ContentValues().apply { put("metadata", metadata.toString()) },
            "id=? AND state='capturing'", arrayOf(id))
    }
    fun finish(id: String) {
        val item = get(id) ?: error("Capture is no longer in the queue.")
        require(item.state == "capturing" || item.state == "importing") { "This original was already finalized." }
        val file = file(item)
        require(file.length() in 1..MAX_BYTES) { "Keep each capture under 64 MB. This file is retained on your device." }
        FileOutputStream(file, true).use { it.fd.sync() }
        val digest = checksum(file)
        writableDatabase.update("uploads", ContentValues().apply {
            put("state", "queued"); put("size", file.length()); put("checksum", digest); put("error", "")
            putNull("import_uri"); put("sent", 0)
        }, "id=?", arrayOf(id))
    }
    fun state(id: String, state: String, error: String = "", sent: Long? = null) {
        writableDatabase.update("uploads", ContentValues().apply {
            put("state", state); put("error", error.take(500)); if (sent != null) put("sent", sent)
        }, "id=?", arrayOf(id))
    }
    fun rebind(id: String, session: AtlasSession) {
        writableDatabase.update("uploads", ContentValues().apply { put("session_id", session.id) }, "id=?", arrayOf(id))
    }
    fun delete(id: String) {
        val item = get(id) ?: return
        check(!file(item).exists() || file(item).delete()) { "The local capture could not be removed." }
        writableDatabase.delete("uploads", "id=?", arrayOf(id))
    }
    @Synchronized fun cacheGeneration(sessionId: String): Long = cacheGenerations[sessionId] ?: 0L
    /** Only remote metadata is removed. Captures, upload bindings and private drafts stay intact. */
    @Synchronized fun invalidateCache(sessionId: String, spaceId: String? = null) {
        cacheGenerations[sessionId] = cacheGeneration(sessionId) + 1
        writableDatabase.delete("cached_spaces", "session_id=?" + if (spaceId == null) "" else " AND space_id=?",
            if (spaceId == null) arrayOf(sessionId) else arrayOf(sessionId, spaceId))
    }
    @Synchronized fun cache(sessionId: String, spaceId: String, json: String, observedGeneration: Long = cacheGeneration(sessionId)) {
        check(observedGeneration == cacheGeneration(sessionId)) { "Workspace access changed during this read. Refresh the space." }
        if (json.length > 1_500_000) return
        val db = writableDatabase
        db.insertWithOnConflict("cached_spaces", null, ContentValues().apply {
            put("session_id", sessionId); put("space_id", spaceId); put("data", json)
        }, SQLiteDatabase.CONFLICT_REPLACE)
        db.execSQL("DELETE FROM cached_spaces WHERE rowid NOT IN (SELECT rowid FROM cached_spaces ORDER BY rowid DESC LIMIT 20)")
    }
    @Synchronized fun cached(sessionId: String): JSONArray = readableDatabase.rawQuery(
        "SELECT data FROM cached_spaces WHERE session_id=?", arrayOf(sessionId)).use { cursor ->
        JSONArray().also { result -> while (cursor.moveToNext()) result.put(JSONObject(cursor.getString(0))) }
    }
    companion object {
        const val MAX_BYTES = 64L * 1024 * 1024
        val IMPORT_MIMES = setOf("image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm")
        @Volatile private var instance: AtlasOutbox? = null
        fun get(context: Context): AtlasOutbox = instance ?: synchronized(this) {
            instance ?: AtlasOutbox(context.applicationContext).also { instance = it }
        }
        fun checksum(file: File): String {
            val hash = MessageDigest.getInstance("SHA-256")
            FileInputStream(file).use { stream ->
                val buffer = ByteArray(64 * 1024)
                while (true) { val read = stream.read(buffer); if (read < 0) break; hash.update(buffer, 0, read) }
            }
            return hash.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
