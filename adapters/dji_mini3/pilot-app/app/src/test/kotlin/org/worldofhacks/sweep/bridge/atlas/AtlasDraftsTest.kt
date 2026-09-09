package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasDraftsTest {
    private fun session(token: String = "test-key-one", space: String? = null) = AtlasSession.parse(JSONObject()
        .put("baseUrl", "https://relay.example").put("sessionId", "Austin").put("token", token).put("space", space ?: JSONObject.NULL))
    private fun space() = JSONObject().put("title", "Austin creek access").put("description", "Private report with an unfinished location")
        .put("place", "Shoal Creek, Austin").put("category", "survey").put("latitude", 0).put("longitude", -97.748).put("radius", 80)
    private fun draft() = JSONObject().put("version", 1).put("id", UUID.randomUUID().toString())
        .put("revision", 1).put("updatedAt", 1788900000000L).put("space", space())
        .put("coordinates", JSONArray().put("").put("-97.748")).put("submitted", JSONObject.NULL)
    private fun preferences() = RuntimeEnvironment.getApplication().getSharedPreferences("draft-test-${UUID.randomUUID()}", Context.MODE_PRIVATE)

    @Test fun `fresh store retains partial text and null submission isolated by original credentials`() {
        val prefs = preferences()
        val original = draft()
        AtlasDrafts(prefs).write(session(), original, null)
        val read = AtlasDrafts(prefs).read(session())!!
        assertEquals(original.toString(), read.toString())
        assertEquals("", read.getJSONArray("coordinates").getString(0))
        assertTrue(read.isNull("submitted"))
        assertNull(AtlasDrafts(prefs).read(session(token = "test-key-two")))
        assertFalse(prefs.all.toString().contains("test-key-one"))
    }
    @Test fun `stale editor and discard cannot overwrite or erase a newer revision`() {
        val prefs = preferences()
        val store = AtlasDrafts(prefs)
        val first = draft()
        store.write(session(), first, null)
        val latest = JSONObject(first.toString()).put("revision", 2).put("submitted", space())
        store.write(session(), latest, first)
        val conflicting = JSONObject(latest.toString()).put("updatedAt", 1788900000001L)
        assertThrows(IllegalStateException::class.java) { AtlasDrafts(prefs).write(session(), conflicting, first) }
        assertThrows(IllegalStateException::class.java) { store.remove(session(), first) }
        assertEquals(latest.toString(), AtlasDrafts(prefs).read(session()).toString())
        store.remove(session(), latest)
        assertNull(AtlasDrafts(prefs).read(session()))
    }
    @Test fun `identical save and removal can be retried after their replies were lost`() {
        val store = AtlasDrafts(preferences())
        val value = draft()
        store.write(session(), value, null)
        store.write(session(), value, null)
        assertEquals(value.toString(), store.read(session()).toString())
        store.remove(session(), value)
        store.remove(session(), value)
        assertNull(store.read(session()))
    }
    @Test fun `scoped invitation cannot read save or publish new-space drafts`() {
        val store = AtlasDrafts(preferences())
        val scoped = session(space = "place")
        assertThrows(IllegalArgumentException::class.java) { store.read(scoped) }
        assertThrows(IllegalArgumentException::class.java) { store.write(scoped, draft(), null) }
        val path = "/atlas/spaces/drafts/${UUID.randomUUID()}/publish"
        assertTrue(session().api(path).toString().endsWith(path))
        assertThrows(IllegalArgumentException::class.java) { scoped.api(path) }
        assertThrows(IllegalArgumentException::class.java) { session(space = "drafts").api(path) }
        assertThrows(IllegalArgumentException::class.java) { session().api("/atlas/spaces/drafts/../control") }
    }
    @Test fun `malformed or oversized draft never replaces the saved version`() {
        val store = AtlasDrafts(preferences())
        val original = draft()
        store.write(session(), original, null)
        val bad = JSONObject(original.toString()).put("revision", 2).put("coordinates", JSONArray().put("x"))
        assertThrows(IllegalArgumentException::class.java) { store.write(session(), bad, original) }
        val large = JSONObject(original.toString()).put("revision", 2).put("extra", "x".repeat(33_000))
        assertThrows(IllegalArgumentException::class.java) { store.write(session(), large, original) }
        assertEquals(original.toString(), store.read(session()).toString())
    }
}
