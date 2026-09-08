package org.worldofhacks.sweep.bridge.atlas

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.UUID

data class AtlasUpload(val id: String, val sessionId: String, val spaceId: String,
    val state: String, val metadata: String, val mime: String, val size: Long,
    val sent: Long, val checksum: String?, val error: String, val createdAt: Long) {
    fun summary() = JSONObject().put("id", id).put("spaceId", spaceId).put("state", state)
        .put("kind", JSONObject(metadata).optString("kind")).put("bytes", size).put("sent", sent)
        .put("error", error).put("createdAt", createdAt)
}

/** Files and queue rows survive activity/process death. Tokens never enter this database. */
class AtlasOutbox private constructor(context: Context) : SQLiteOpenHelper(context, "atlas-outbox.db", null, 1) {
    val root = File(context.filesDir, "atlas-captures").apply { mkdirs() }
    init {
        setWriteAheadLoggingEnabled(true)
        // This singleton initializes once per process, before any new camera capture.
        writableDatabase.execSQL("UPDATE uploads SET state='failed', error='Capture was interrupted before finalization. The local file is retained.' WHERE state='capturing'")
    }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE uploads (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, space_id TEXT NOT NULL, " +
            "state TEXT NOT NULL, metadata TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, " +
            "sent INTEGER NOT NULL DEFAULT 0, checksum TEXT, error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE cached_spaces (session_id TEXT, space_id TEXT, data TEXT, PRIMARY KEY(session_id,space_id))")
    }
    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) = Unit
    fun file(item: AtlasUpload): File = File(root, item.id + if (item.mime == "video/mp4") ".mp4" else ".jpg")
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
            string("error"), number("created_at"))
    }
    @Synchronized fun begin(session: AtlasSession, spaceId: String, metadata: JSONObject): AtlasUpload {
        session.endpoint(spaceId) // Scope admission before creating any file.
        require(spaceId.matches(Regex("[a-zA-Z0-9_-]{1,64}")))
        val items = list()
        require(items.size < 100 && root.listFiles().orEmpty().sumOf { it.length() } < 1024L * 1024 * 1024) {
            "Your capture queue is full. Remove saved copies before capturing more."
        }
        val id = UUID.randomUUID().toString()
        writableDatabase.insertOrThrow("uploads", null, ContentValues().apply {
            put("id", id); put("session_id", session.id); put("space_id", spaceId)
            put("state", "capturing"); put("metadata", metadata.toString())
            put("mime", if (metadata.getString("kind") == "video") "video/mp4" else "image/jpeg")
            put("created_at", System.currentTimeMillis())
        })
        return get(id)!!
    }
    fun captureMetadata(id: String, metadata: JSONObject) {
        writableDatabase.update("uploads", ContentValues().apply { put("metadata", metadata.toString()) },
            "id=? AND state='capturing'", arrayOf(id))
    }
    fun finish(id: String) {
        val item = get(id) ?: error("Capture is no longer in the queue.")
        val file = file(item)
        require(file.length() in 1..MAX_BYTES) { "Keep each capture under 64 MB. This file is retained on your device." }
        FileOutputStream(file, true).use { it.fd.sync() }
        val digest = checksum(file)
        writableDatabase.update("uploads", ContentValues().apply {
            put("state", "queued"); put("size", file.length()); put("checksum", digest); put("error", "")
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
    fun cache(sessionId: String, spaceId: String, json: String) {
        if (json.length > 1_500_000) return
        val db = writableDatabase
        db.insertWithOnConflict("cached_spaces", null, ContentValues().apply {
            put("session_id", sessionId); put("space_id", spaceId); put("data", json)
        }, SQLiteDatabase.CONFLICT_REPLACE)
        db.execSQL("DELETE FROM cached_spaces WHERE rowid NOT IN (SELECT rowid FROM cached_spaces ORDER BY rowid DESC LIMIT 20)")
    }
    fun cached(sessionId: String): JSONArray = readableDatabase.rawQuery(
        "SELECT data FROM cached_spaces WHERE session_id=?", arrayOf(sessionId)).use { cursor ->
        JSONArray().also { result -> while (cursor.moveToNext()) result.put(JSONObject(cursor.getString(0))) }
    }
    companion object {
        const val MAX_BYTES = 64L * 1024 * 1024
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
