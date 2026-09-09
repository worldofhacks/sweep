package org.worldofhacks.sweep.bridge.atlas

import android.content.SharedPreferences
import android.annotation.SuppressLint
import org.json.JSONObject

/** Uses the existing encrypted vault, not WebView local storage. One draft per credential. */
class AtlasDrafts(private val preferences: SharedPreferences) {
    fun read(session: AtlasSession): JSONObject? = synchronized(LOCK) {
        require(session.space == null) { "A contribution invitation cannot create new spaces." }
        preferences.getString("draft-${session.id}", null)?.let { validate(JSONObject(it)) }
    }
    fun write(session: AtlasSession, value: JSONObject, previous: JSONObject?) = synchronized(LOCK) {
        val current = read(session)
        validate(value)
        // A failed disk commit or lost bridge reply may already have changed the in-memory
        // preferences. Re-commit the identical revision, but never accept a different edit.
        if (current?.toString() != value.toString()) {
            checkRevision(current, previous)
            require(value.getLong("revision") == (current?.getLong("revision") ?: 0) + 1)
            require(current == null || current.getString("id") == value.getString("id"))
        }
        commit { it.putString("draft-${session.id}", value.toString()) }
    }
    fun remove(session: AtlasSession, previous: JSONObject) = synchronized(LOCK) {
        val current = read(session)
        if (current != null) checkRevision(current, previous)
        commit { it.remove("draft-${session.id}") }
    }
    @SuppressLint("UseKtx") // The KTX helper discards the commit result required for a saved badge.
    private fun commit(change: (SharedPreferences.Editor) -> Unit) {
        // KTX edit(commit=true) discards the Boolean; durability must be acknowledged here.
        val editor = preferences.edit()
        change(editor)
        check(editor.commit()) { "The local draft change could not be saved on this device." }
    }
    private fun checkRevision(current: JSONObject?, previous: JSONObject?) {
        check(current?.getString("id") == previous?.getString("id") &&
            current?.getLong("revision") == previous?.getLong("revision")) {
            "This draft changed in another window. Reopen Spaces to load the saved version."
        }
    }
    companion object {
        private val LOCK = Any()
        fun validate(value: JSONObject): JSONObject {
            require(value.toString().length <= 32_768) { "The draft is too large." }
            require(value.getInt("version") == 1 && value.getLong("revision") >= 1 && value.getLong("updatedAt") > 0)
            require(value.getString("id").matches(Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")))
            checkSpace(value.getJSONObject("space"))
            val coordinates = value.getJSONArray("coordinates")
            require(coordinates.length() == 2 && (0..1).all { coordinates.getString(it).length <= 64 })
            require(value.has("submitted"))
            if (!value.isNull("submitted")) checkSpace(value.getJSONObject("submitted"))
            return value
        }
        private fun checkSpace(value: JSONObject) {
            require(value.getString("title").length <= 100 && value.getString("description").length <= 2000 && value.getString("place").length <= 120)
            require(value.getString("category") in setOf("incident", "hazard", "community", "survey"))
            require(value.getDouble("latitude").isFinite() && value.getDouble("longitude").isFinite())
            require(value.getDouble("radius") == value.getInt("radius").toDouble() && value.getInt("radius") in 20..500)
        }
    }
}
