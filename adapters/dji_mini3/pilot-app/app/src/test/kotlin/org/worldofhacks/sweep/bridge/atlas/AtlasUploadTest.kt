package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import androidx.work.ListenableWorker
import androidx.work.testing.TestWorkerBuilder
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Before
import org.junit.After
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasUploadTest {
    @Before fun beginProcess() = resetAtlasTestProcess()
    @After fun endProcess() = resetAtlasTestProcess()
    private val context get() = RuntimeEnvironment.getApplication()
    private fun session(server: MockWebServer, token: String = "test-upload-key") = AtlasSession.parse(JSONObject()
        .put("baseUrl", server.url("/").toString()).put("sessionId", "room").put("token", token).put("space", "place"))
    private fun capture(session: AtlasSession): AtlasUpload {
        val queue = AtlasOutbox.get(context)
        val metadata = JSONObject().put("kind", "photo").put("contributor_id", "person-one").put("name", "Renée")
            .put("captured_at", 123456).put("source", "camera").put("position", JSONObject.NULL).put("note", "")
        val item = queue.begin(session, "place", metadata)
        queue.file(item).writeBytes(ByteArray(100_000) { (it % 251).toByte() })
        queue.finish(item.id)
        return queue.get(item.id)!!
    }
    private fun worker() = TestWorkerBuilder.from(context, AtlasUploadWorker::class.java, Executor { it.run() }).build()

    @Test fun `worker sends exact original and metadata and requires the server checksum`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            server.enqueue(MockResponse().setBody(JSONObject().put("sha256", item.checksum).toString()))
            assertEquals(ListenableWorker.Result.success(), worker().deliver(item, access))
            val received = server.takeRequest(5, TimeUnit.SECONDS)!!
            assertEquals("/api/sessions/room/atlas/spaces/place/captures", received.path)
            assertEquals("Bearer test-upload-key", received.getHeader("Authorization"))
            assertEquals("Renée", JSONObject(received.getHeader("X-Sweep-Capture")!!).getString("name"))
            assertEquals(item.checksum, AtlasSession.sha256(received.body.readByteArray()))
            assertEquals("saved", AtlasOutbox.get(context).get(item.id)!!.state)
            assertTrue(AtlasOutbox.get(context).file(item).isFile)
        }
    }
    @Test fun `temporary server failure retries the identical file`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            server.enqueue(MockResponse().setResponseCode(503))
            assertEquals(ListenableWorker.Result.retry(), worker().deliver(item, access))
            assertEquals("queued", AtlasOutbox.get(context).get(item.id)!!.state)
            server.enqueue(MockResponse().setBody(JSONObject().put("sha256", item.checksum).toString()))
            assertEquals(ListenableWorker.Result.success(), worker().deliver(item, access))
            val first = server.takeRequest().body.readByteArray()
            assertArrayEquals(first, server.takeRequest().body.readByteArray())
        }
    }
    @Test fun `refused invitation is actionable failure with the original retained`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            server.enqueue(MockResponse().setResponseCode(403).setBody("{\"detail\":\"Invitation revoked\"}"))
            assertEquals(ListenableWorker.Result.failure(), worker().deliver(item, access))
            val result = AtlasOutbox.get(context).get(item.id)!!
            assertEquals("failed", result.state)
            assertEquals("Invitation revoked", result.error)
            assertTrue(AtlasOutbox.get(context).file(item).isFile)
        }
    }
    @Test fun `wrong server checksum never produces a saved badge`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            server.enqueue(MockResponse().setBody("{\"sha256\":\"wrong\"}"))
            assertEquals(ListenableWorker.Result.failure(), worker().deliver(item, access))
            assertEquals("failed", AtlasOutbox.get(context).get(item.id)!!.state)
            assertTrue(AtlasOutbox.get(context).file(item).isFile)
        }
    }
    @Test fun `redirects do not forward the capture or credential`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            server.enqueue(MockResponse().setResponseCode(302).addHeader("Location", server.url("/elsewhere")))
            assertEquals(ListenableWorker.Result.failure(), worker().deliver(item, access))
            assertEquals(1, server.requestCount)
        }
    }
    @Test fun `a changed connection cannot silently retarget an old capture`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            assertEquals(ListenableWorker.Result.failure(), worker().deliver(item, session(server, "new-test-key")))
            assertEquals(0, server.requestCount)
        }
    }
    @Test fun `corrupt local bytes are rejected before any network request`() {
        MockWebServer().use { server ->
            val access = session(server); val item = capture(access)
            AtlasOutbox.get(context).file(item).appendText("changed")
            assertEquals(ListenableWorker.Result.failure(), worker().deliver(item, access))
            assertEquals(0, server.requestCount)
        }
    }
}
