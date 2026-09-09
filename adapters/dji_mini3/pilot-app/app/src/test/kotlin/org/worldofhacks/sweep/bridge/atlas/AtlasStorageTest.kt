package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Before
import org.junit.After
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasStorageTest {
    @Before fun beginProcess() = resetAtlasTestProcess()
    @After fun endProcess() = resetAtlasTestProcess()
    private fun session(space: String? = null, token: String = "test-key-one", base: String = "http://127.0.0.1:8000") =
        AtlasSession.parse(JSONObject().put("baseUrl", base).put("sessionId", "room").put("token", token).put("space", space ?: JSONObject.NULL))
    private fun metadata() = JSONObject().put("kind", "photo").put("contributor_id", "person-one").put("name", "Example")
        .put("source", "camera").put("position", JSONObject.NULL).put("captured_at", 123456).put("note", "")

    @Test fun `scope cannot reach another space or fleet controls`() {
        val scoped = session("place")
        assertTrue(scoped.api("/atlas/spaces/place").toString().endsWith("/atlas/spaces/place"))
        assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces") }
        assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces/other") }
        assertThrows(IllegalArgumentException::class.java) { scoped.api("/control") }
        assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces/place/../../control") }
        assertThrows(IllegalArgumentException::class.java) { scoped.endpoint("other", "captures") }
    }
    @Test fun `credential identity includes original destination and invitation`() {
        assertNotEquals(session().id, session(token = "test-key-two").id)
        assertNotEquals(session().id, session("place").id)
        assertNotEquals(session().id, session(base = "https://relay.example").id)
        assertFalse(session().publicJson().has("token"))
        assertFalse(session().toString().contains("test-key-one"))
    }
    @Test fun `memory JSON and playback stay capture and space scoped`() {
        val scoped = session("place")
        val asset = "544db565-bec9-4bf9-aae9-5ef89a696d0d"
        listOf("", "/inspect", "/analyze", "/assets/$asset/media").forEach { suffix ->
            val path = "/atlas/spaces/place/captures/photo/memory$suffix"
            assertTrue(scoped.api(path).toString().endsWith(path))
            assertThrows(IllegalArgumentException::class.java) { scoped.api(path.replace("/place/", "/other/")) }
        }
        listOf("/assets", "/assets/$asset", "/admin", "/assets/$asset/../media").forEach { suffix ->
            assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces/place/captures/photo/memory$suffix") }
        }
    }
    @Test fun `surface review routes stay inside the original space and expose no worker internals`() {
        val scoped = session("place")
        val job = "544db565-bec9-4bf9-aae9-5ef89a696d0d"
        val region = "0123456789abcdef"
        listOf("surface-requests", "surface-requests/$job/$region/dismiss",
            "reconstruction/$job/manifest.json", "reconstruction/$job/cloud.glb").forEach { suffix ->
            assertTrue(scoped.api("/atlas/spaces/place/$suffix").toString().endsWith(suffix))
            assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces/other/$suffix") }
        }
        listOf("reconstruction/$job/worker.log", "surface-requests/$job/$region/approve",
            "surface-requests/$job/invalid/dismiss", "reconstruction/$job/../control").forEach { suffix ->
            assertThrows(IllegalArgumentException::class.java) { scoped.api("/atlas/spaces/place/$suffix") }
        }
    }
    @Test fun `plain HTTP is limited to explicit local addresses`() {
        assertTrue(AtlasSession.isLocalHost("192.168.1.1"))
        assertTrue(AtlasSession.isLocalHost("172.16.0.2"))
        assertFalse(AtlasSession.isLocalHost("172.32.0.1"))
        assertFalse(AtlasSession.isLocalHost("192.168.1.999"))
        assertThrows(IllegalArgumentException::class.java) { session(base = "http://relay.example") }
        assertThrows(IllegalArgumentException::class.java) { session(base = "https://user:secret@relay.example") }
        assertThrows(IllegalArgumentException::class.java) { session(base = "https://relay.example?token=secret") }
    }
    @Test fun `capture finalization preserves original bytes and metadata in SQLite`() {
        val queue = AtlasOutbox.get(RuntimeEnvironment.getApplication())
        val item = queue.begin(session("place"), "place", metadata())
        val original = "example-original-photo".toByteArray()
        queue.file(item).writeBytes(original)
        queue.finish(item.id)
        val saved = queue.get(item.id)!!
        assertEquals("queued", saved.state)
        assertEquals(original.size.toLong(), saved.size)
        assertEquals(AtlasSession.sha256(original), saved.checksum)
        assertEquals(123456, JSONObject(saved.metadata).getInt("captured_at"))
        assertTrue(JSONObject(saved.metadata).isNull("position"))
        assertArrayEquals(original, queue.file(saved).readBytes())
        queue.close()
        // Reopen the SQLite connection, not an in-memory fake.
        assertEquals(saved, queue.get(item.id))
    }
    @Test fun `failed and saved rows retain their original until explicitly removed`() {
        val queue = AtlasOutbox.get(RuntimeEnvironment.getApplication())
        val item = queue.begin(session("place"), "place", metadata())
        queue.file(item).writeText("retained-original")
        queue.finish(item.id)
        queue.state(item.id, "failed", "Network interrupted")
        assertTrue(queue.file(item).isFile)
        queue.state(item.id, "saved", sent = queue.file(item).length())
        assertTrue(queue.file(item).isFile)
        queue.delete(item.id)
        assertNull(queue.get(item.id))
        assertFalse(queue.file(item).exists())
    }
    @Test fun `fresh process recovery retains unfinished files and keeps finalized work queued`() {
        val context = RuntimeEnvironment.getApplication()
        val queue = AtlasOutbox.get(context)
        val interrupted = queue.begin(session("place"), "place", metadata())
        queue.file(interrupted).writeText("partial-original")
        val complete = queue.begin(session("place"), "place", metadata())
        queue.file(complete).writeText("finalized-original")
        queue.finish(complete.id)
        assertTrue(context.getDatabasePath("atlas-outbox.db").isFile)
        queue.close()
        // Drop the singleton to exercise the cold-process constructor against the same real DB.
        AtlasOutbox::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
        val reopened = AtlasOutbox.get(context)
        assertEquals("failed", reopened.get(interrupted.id)!!.state)
        assertEquals("partial-original", reopened.file(interrupted).readText())
        assertEquals("queued", reopened.get(complete.id)!!.state)
        assertEquals(AtlasSession.sha256("finalized-original".toByteArray()), reopened.get(complete.id)!!.checksum)
    }
    @Test fun `scope is checked before an offline capture row is created`() {
        val queue = AtlasOutbox.get(RuntimeEnvironment.getApplication())
        val count = queue.list().size
        assertThrows(IllegalArgumentException::class.java) { queue.begin(session("place"), "other", metadata()) }
        assertEquals(count, queue.list().size)
    }
    @Test fun `cached metadata never crosses credential boundaries`() {
        val queue = AtlasOutbox.get(RuntimeEnvironment.getApplication())
        queue.cache(session().id, "place", JSONObject().put("space", "place").toString())
        assertEquals(1, queue.cached(session().id).length())
        assertEquals(0, queue.cached(session(token = "test-key-two").id).length())
    }
    @Test fun `header metadata safely preserves Unicode`() {
        val value = JSONObject().put("name", "Renée 東京").put("note", "line\nbreak").toString()
        val header = AtlasUploadWorker.asciiJson(value)
        assertTrue(header.all { it.code in 32..126 })
        assertEquals("Renée 東京", JSONObject(header).getString("name"))
        assertEquals("line\nbreak", JSONObject(header).getString("note"))
    }
}

// Robolectric replaces Application between tests, but application-owned Kotlin singletons
// otherwise retain the prior test's context. Reset that process state, not the database file.
internal fun resetAtlasTestProcess() {
    val field = AtlasOutbox::class.java.getDeclaredField("instance").apply { isAccessible = true }
    (field.get(null) as? AtlasOutbox)?.close()
    field.set(null, null)
}
