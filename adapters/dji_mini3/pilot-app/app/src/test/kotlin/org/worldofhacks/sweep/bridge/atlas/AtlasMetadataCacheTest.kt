package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasMetadataCacheTest {
    @Before fun beginProcess() = resetAtlasTestProcess()
    @After fun endProcess() = resetAtlasTestProcess()
    private val queue get() = AtlasOutbox.get(RuntimeEnvironment.getApplication())
    private fun session(token: String = "test-cache-key", space: String? = null) = AtlasSession.parse(JSONObject()
        .put("baseUrl", "https://relay.example").put("sessionId", "Austin").put("token", token).put("space", space ?: JSONObject.NULL))
    private fun detail(id: String = "place") = JSONObject().put("space", JSONObject().put("id", id).put("contributors", 2))
        .put("people", JSONArray().put(JSONObject().put("name", "Live contributor"))).put("captures", JSONArray()).toString()
    private fun observe(access: AtlasSession, id: String = "place", status: Int = 200,
        generation: Long = queue.cacheGeneration(access.id)) {
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/$id", "GET", status, detail(id), generation)
    }

    @Test fun `native successful detail caches only its own space and strips live presence from the copy`() {
        val access = session(space = "place")
        val raw = detail()
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/place", "GET", 200, raw, 0)
        val cached = queue.cached(access.id).getJSONObject(0)
        assertEquals("place", cached.getJSONObject("space").getString("id"))
        assertEquals(0, cached.getJSONArray("people").length())
        assertEquals(0, cached.getJSONObject("space").getInt("contributors"))
        assertEquals(1, JSONObject(raw).getJSONArray("people").length())
        assertFalse(cached.toString().contains(access.token))
    }

    @Test fun `space read refusal removes only that space for that credential`() {
        val access = session()
        val other = session("another-cache-key")
        observe(access); observe(access, "second"); observe(other)
        observe(access, status = 403)
        assertEquals(1, queue.cached(access.id).length())
        assertEquals("second", queue.cached(access.id).getJSONObject(0).getJSONObject("space").getString("id"))
        assertEquals(1, queue.cached(other.id).length())
    }

    @Test fun `authentication refusal clears the credential cache including on a write`() {
        val access = session()
        val other = session("another-cache-key")
        observe(access); observe(access, "second"); observe(other)
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/place/status", "POST", 401, "", 0)
        assertEquals(0, queue.cached(access.id).length())
        assertEquals(1, queue.cached(other.id).length())
    }

    @Test fun `operation refusal and temporary server failure do not withdraw read access`() {
        val access = session()
        observe(access)
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/place/reconstruction", "POST", 403, "", 0)
        observe(access, status = 503)
        assertEquals(1, queue.cached(access.id).length())
        assertEquals(0, queue.cacheGeneration(access.id))
    }

    @Test fun `invalidation retires older observations but a fresh authorized read can cache again`() {
        val access = session()
        val previousRead = queue.cacheGeneration(access.id)
        observe(access)
        observe(access, status = 403)
        assertThrows(IllegalStateException::class.java) { observe(access, generation = previousRead) }
        assertEquals(0, queue.cached(access.id).length())
        observe(access)
        assertEquals(1, queue.cached(access.id).length())
    }

    @Test fun `metadata invalidation survives process restart without removing captured originals`() {
        val access = session(space = "place")
        observe(access)
        val item = queue.begin(access, "place", JSONObject().put("kind", "photo"))
        val original = "private-original".toByteArray()
        queue.file(item).writeBytes(original)
        queue.finish(item.id)
        observe(access, status = 403)
        resetAtlasTestProcess()
        assertEquals(0, queue.cached(access.id).length())
        assertEquals("queued", queue.get(item.id)!!.state)
        assertArrayEquals(original, queue.file(item).readBytes())
    }

    @Test fun `a mismatched detail is not cached under another space`() {
        val access = session()
        assertThrows(IllegalStateException::class.java) {
            AtlasMetadataCache(queue).response(access, "/atlas/spaces/place", "GET", 200, detail("second"), 0)
        }
        assertEquals(0, queue.cached(access.id).length())
    }

    @Test fun `media and upload access refusals retire the affected cached space`() {
        val access = session()
        observe(access); observe(access, "second")
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/place/captures/photo/media", "GET", 403, "", 0)
        assertEquals(1, queue.cached(access.id).length())
        AtlasMetadataCache(queue).accessRefused(access, "second", 403)
        assertEquals(0, queue.cached(access.id).length())
    }

    @Test fun `missing source or derivative retires its snapshot and rejects older reads`() {
        val access = session()
        val other = session("another-cache-key")
        val resourcePaths = listOf("", "/captures/photo/media", "/captures/photo/memory",
            "/reconstruction/build/cloud.glb")
        resourcePaths.forEach { suffix ->
            observe(access); observe(access, "second"); observe(other)
            val previousRead = queue.cacheGeneration(access.id)
            AtlasMetadataCache(queue).response(access, "/atlas/spaces/place$suffix", "GET", 404, "", previousRead)
            assertEquals("second", queue.cached(access.id).getJSONObject(0).getJSONObject("space").getString("id"))
            assertEquals(1, queue.cached(access.id).length())
            assertEquals(1, queue.cached(other.id).length())
            assertThrows(IllegalStateException::class.java) { observe(access, generation = previousRead) }
        }
    }

    @Test fun `a missing optional write does not erase the readable snapshot`() {
        val access = session()
        observe(access)
        AtlasMetadataCache(queue).response(access, "/atlas/spaces/place/reconstruction", "POST", 404, "", 0)
        assertEquals(1, queue.cached(access.id).length())
        assertEquals(0, queue.cacheGeneration(access.id))
    }

    @Test fun `missing media invalidation survives restart and preserves local upload originals`() {
        val access = session(space = "place")
        observe(access)
        val item = queue.begin(access, "place", JSONObject().put("kind", "photo"))
        val original = "owned-local-original".toByteArray()
        queue.file(item).writeBytes(original)
        queue.finish(item.id)
        AtlasMetadataCache(queue).accessRefused(access, "place", 404)
        resetAtlasTestProcess()
        assertEquals(0, queue.cached(access.id).length())
        assertEquals("queued", queue.get(item.id)!!.state)
        assertArrayEquals(original, queue.file(item).readBytes())
    }
}
