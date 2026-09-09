package org.worldofhacks.sweep.bridge.atlas

import org.json.JSONArray
import org.json.JSONObject

/** Cache only native HTTP observations, never a later JavaScript copy of a response. */
internal class AtlasMetadataCache(private val queue: AtlasOutbox) {
    fun accessRefused(session: AtlasSession, spaceId: String?, status: Int) {
        if (status == 401) queue.invalidateCache(session.id)
        else if (status == 403) queue.invalidateCache(session.id, spaceId)
    }

    fun response(session: AtlasSession, path: String, method: String, status: Int, body: String,
        observedGeneration: Long) {
        val spaceId = SPACE_PATH.matchEntire(path)?.groupValues?.get(1)
        // A denied write (for example, contributor reconstruction) is not loss of read access.
        if (status == 401 || method == "GET") accessRefused(session, spaceId, status)
        if (method != "GET" || status !in 200..299 || spaceId == null || path != "/atlas/spaces/$spaceId") return
        session.endpoint(spaceId)
        val detail = JSONObject(body)
        check(detail.getJSONObject("space").getString("id") == spaceId) { "The workspace returned another space." }
        detail.put("people", JSONArray())
        detail.getJSONObject("space").put("contributors", 0)
        queue.cache(session.id, spaceId, detail.toString(), observedGeneration)
    }

    companion object {
        private val SPACE_PATH = Regex("/atlas/spaces/([a-zA-Z0-9_-]{1,64})(?:/.*)?")
    }
}
