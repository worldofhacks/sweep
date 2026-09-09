package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Intent
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.provider.OpenableColumns
import androidx.work.Configuration
import androidx.work.ListenableWorker
import androidx.work.WorkManager
import androidx.work.testing.TestWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import androidx.work.workDataOf
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.File
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasImportTest {
    @Before fun beginProcess() = resetAtlasTestProcess()
    @After fun endProcess() = resetAtlasTestProcess()
    private val context get() = RuntimeEnvironment.getApplication()
    private val queue get() = AtlasOutbox.get(context)
    private val uri = Uri.parse("content://atlas-import-test/photo")
    private val original = ByteArray(100_003) { (it % 251).toByte() }
    private fun session(base: String = "http://127.0.0.1:8000", token: String = "import-test-key") =
        AtlasSession.parse(JSONObject().put("baseUrl", base).put("sessionId", "workspace")
            .put("token", token).put("space", "austin-space"))
    private fun target(access: AtlasSession = session()) = AtlasImportTarget(access.id, "austin-space", "person-one", "Renée")
    private fun captureRequest() = AtlasCaptureRequest.parse(JSONObject()
        .put("label", "Creek edge").put("note", "Add context from the public path.")
        .put("target", JSONObject().put("kind", "surface").put("job_id", "11111111-1111-1111-1111-111111111111")
            .put("artifact_sha256", "a".repeat(64)).put("region_id", "0123456789abcdef")))
    private fun item(mime: String = "image/png", access: AtlasSession = session()) =
        queue.beginImport(access, "austin-space", uri.toString(), mime, "person-one", "Renée")
    private fun worker(id: String = "unused") = TestWorkerBuilder.from(context, AtlasImportWorker::class.java, Executor { it.run() })
        .setInputData(workDataOf("upload_id" to id)).build()
    private fun source(bytes: ByteArray = original) {
        shadowOf(context.contentResolver).registerInputStreamSupplier(uri) { bytes.inputStream() }
    }
    private fun provider(mime: String) {
        ShadowContentResolver.registerProviderInternal(uri.authority!!, object : ContentProvider() {
            override fun onCreate() = true
            override fun getType(uri: Uri) = mime
            override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, order: String?): Cursor =
                MatrixCursor(arrayOf(OpenableColumns.DISPLAY_NAME)).apply { addRow(arrayOf("Austin creek – original.png")) }
            override fun insert(uri: Uri, values: ContentValues?): Uri? = null
            override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
            override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
        })
    }

    @Test fun `system picker requests only selected readable supported documents`() {
        val intent = AtlasImportSelection.picker()
        assertEquals(Intent.ACTION_OPEN_DOCUMENT, intent.action)
        assertTrue(intent.categories.contains(Intent.CATEGORY_OPENABLE))
        assertTrue(intent.getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE, false))
        assertEquals(AtlasOutbox.IMPORT_MIMES, intent.getStringArrayExtra(Intent.EXTRA_MIME_TYPES)!!.toSet())
        assertEquals(0, intent.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        assertTrue(intent.flags and Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION != 0)
        assertEquals(target(), AtlasImportTarget.parse(JSONObject(target().json().toString())))
    }
    @Test fun `request survives picker recreation and cold outbox reopening without gaining GPS`() {
        val request = captureRequest()
        val chosen = target().copy(request = request)
        assertEquals(chosen, AtlasImportTarget.parse(JSONObject(chosen.json().toString())))
        val imported = queue.beginImport(session(), chosen.space, uri.toString(), "image/png", chosen.contributor, chosen.name, request = request)
        resetAtlasTestProcess()
        val recovered = queue.get(imported.id)!!
        source()
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(recovered))
        val saved = queue.get(imported.id)!!
        assertTrue(AtlasCaptureRequest.matches(request.target(), JSONObject(saved.metadata).getJSONObject("response_to")))
        assertTrue(JSONObject(saved.metadata).isNull("captured_at"))
        assertTrue(JSONObject(saved.metadata).isNull("position"))
        assertTrue(AtlasCaptureRequest.matches(request.target(), saved.summary().getJSONObject("responseTo")))
        assertArrayEquals(original, queue.file(saved).readBytes())
    }
    @Test fun `request is immutable and rejects unknown target fields or retargeted acknowledgments`() {
        val raw = captureRequest().json()
        val request = AtlasCaptureRequest.parse(raw)
        raw.getJSONObject("target").put("region_id", "fedcba9876543210")
        assertEquals("0123456789abcdef", request.target().getString("region_id"))
        assertFalse(AtlasCaptureRequest.matches(request.target(), raw.getJSONObject("target")))
        raw.getJSONObject("target").put("latitude", 30.2672)
        assertThrows(IllegalArgumentException::class.java) { AtlasCaptureRequest.parse(raw) }
        assertFalse(AtlasCaptureRequest.matches(request.target(), null))
    }
    @Test fun `request upload requires matching acknowledgment and byte identical retries keep the original`() {
        MockWebServer().use { server ->
            val access = session(server.url("/").toString())
            val request = captureRequest()
            val imported = queue.beginImport(access, "austin-space", uri.toString(), "image/png", "person-one", "Sam", request = request)
            source()
            assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(imported))
            val saved = queue.get(imported.id)!!
            val upload = TestWorkerBuilder.from(context, AtlasUploadWorker::class.java, Executor { it.run() }).build()
            listOf(null, request.target().put("artifact_sha256", "b".repeat(64)), request.target()).forEachIndexed { index, acknowledgment ->
                server.enqueue(MockResponse().setBody(JSONObject().put("sha256", saved.checksum).put("response_to", acknowledgment).toString()))
                assertEquals(if (index == 2) ListenableWorker.Result.success() else ListenableWorker.Result.failure(), upload.deliver(queue.get(saved.id)!!, access))
                val received = server.takeRequest(5, TimeUnit.SECONDS)!!
                assertArrayEquals(original, received.body.readByteArray())
                assertTrue(AtlasCaptureRequest.matches(request.target(), JSONObject(received.getHeader("X-Sweep-Capture")!!).getJSONObject("response_to")))
                assertEquals(if (index == 2) "saved" else "failed", queue.get(saved.id)!!.state)
                assertArrayEquals(original, queue.file(saved).readBytes())
            }
        }
    }
    @Test fun `picker admission persists read permission and binds the original destination`() {
        provider("image/png")
        val access = session()
        val imported = AtlasImportSelection.admit(context, access, target(access), uri)
        assertEquals(access.id, imported.sessionId)
        assertEquals("austin-space", imported.spaceId)
        val permission = context.contentResolver.persistedUriPermissions.single()
        assertEquals(uri, permission.uri)
        assertTrue(permission.isReadPermission)
        assertFalse(permission.isWritePermission)
        assertFalse(imported.summary().has("importUri"))
        assertFalse(imported.summary().has("sessionId"))
        assertEquals("Austin creek – original.png", imported.displayName)
        assertEquals(imported.displayName, imported.summary().getString("displayName"))
    }
    @Test fun `another workspace or an unsupported source creates no import row or grant`() {
        provider("image/png")
        assertThrows(IllegalArgumentException::class.java) {
            AtlasImportSelection.admit(context, session(token = "another-key"), target(), uri)
        }
        assertThrows(IllegalArgumentException::class.java) {
            AtlasImportSelection.admit(context, session(), target().copy(space = "another-space"), uri)
        }
        assertThrows(IllegalArgumentException::class.java) {
            AtlasImportSelection.admit(context, session(), target(), Uri.parse("file:///unselected.jpg"))
        }
        provider("image/heic")
        assertThrows(IllegalArgumentException::class.java) {
            AtlasImportSelection.admit(context, session(), target(), uri)
        }
        assertTrue(queue.list().isEmpty())
        assertTrue(context.contentResolver.persistedUriPermissions.isEmpty())
    }
    @Test fun `import retains exact bytes without fabricating a capture date or location`() {
        source()
        provider("image/png")
        val imported = AtlasImportSelection.admit(context, session(), target(), uri)
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(imported))
        val saved = queue.get(imported.id)!!
        assertEquals("queued", saved.state)
        assertEquals("image/png", saved.mime)
        assertTrue(queue.file(saved).name.endsWith(".png"))
        assertArrayEquals(original, queue.file(saved).readBytes())
        assertEquals(AtlasSession.sha256(original), saved.checksum)
        assertNull(saved.importUri)
        assertTrue(saved.summary().getBoolean("finalized"))
        assertTrue(context.contentResolver.persistedUriPermissions.isEmpty())
        val metadata = JSONObject(saved.metadata)
        assertEquals("import", metadata.getString("source"))
        assertTrue(metadata.isNull("captured_at"))
        assertTrue(metadata.isNull("position"))
        assertEquals("Renée", metadata.getString("name"))
    }
    @Test fun `all accepted containers retain their MIME and file suffix`() {
        val suffixes = mapOf("image/jpeg" to ".jpg", "image/png" to ".png", "image/webp" to ".webp",
            "video/mp4" to ".mp4", "video/webm" to ".webm")
        suffixes.forEach { (mime, suffix) ->
            val imported = item(mime)
            assertEquals(mime, imported.mime)
            assertTrue(queue.file(imported).name.endsWith(suffix))
            assertEquals(if (mime.startsWith("video/")) "video" else "photo", JSONObject(imported.metadata).getString("kind"))
        }
        val labelled = queue.beginImport(session(), "austin-space", uri.toString(), "image/png", "person-one", "Sam", "../device\\photos\nview.png")
        assertFalse(labelled.displayName.contains('/'))
        assertFalse(labelled.displayName.contains('\\'))
        assertFalse(labelled.displayName.contains('\n'))
        assertEquals(labelled.id + ".png", queue.file(labelled).name)
    }
    @Test fun `interrupted copy resumes from original bytes after cold SQLite reopening`() {
        val imported = item()
        queue.file(imported).writeText("partial-and-invalid-copy")
        queue.close()
        AtlasOutbox::class.java.getDeclaredField("instance").apply { isAccessible = true }.set(null, null)
        val recovered = queue.get(imported.id)!!
        assertEquals("importing", recovered.state)
        assertEquals(uri.toString(), recovered.importUri)
        source()
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(recovered))
        assertArrayEquals(original, queue.file(recovered).readBytes())
    }
    @Test fun `temporary provider failure retries and never queues a partial file for upload`() {
        val imported = item()
        shadowOf(context.contentResolver).registerInputStreamSupplier(uri) {
            object : InputStream() { override fun read(): Int = throw IOException("Cloud provider temporarily offline") }
        }
        assertEquals(ListenableWorker.Result.retry(), worker().copyOriginal(imported))
        assertEquals("importing", queue.get(imported.id)!!.state)
        assertNull(queue.get(imported.id)!!.checksum)
        source()
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(queue.get(imported.id)!!))
        assertArrayEquals(original, queue.file(imported).readBytes())
    }
    @Test fun `a moved document fails clearly and retains no invented completed original`() {
        val imported = item()
        shadowOf(context.contentResolver).registerInputStreamSupplier(uri) { throw FileNotFoundException("Moved") }
        assertEquals(ListenableWorker.Result.failure(), worker().copyOriginal(imported))
        val failed = queue.get(imported.id)!!
        assertEquals("failed", failed.state)
        assertTrue(failed.error.contains("Choose it again"))
        assertNull(failed.checksum)
        assertFalse(failed.summary().getBoolean("finalized"))
    }
    @Test fun `empty and oversized documents do not become queued originals`() {
        val empty = item()
        source(byteArrayOf())
        assertEquals(ListenableWorker.Result.failure(), worker().copyOriginal(empty))
        val large = item()
        shadowOf(context.contentResolver).registerInputStreamSupplier(uri) {
            object : InputStream() {
                override fun read(): Int = 0
                override fun read(bytes: ByteArray, offset: Int, length: Int): Int = length
            }
        }
        assertEquals(ListenableWorker.Result.failure(), worker().copyOriginal(large))
        assertEquals(AtlasOutbox.MAX_BYTES, queue.file(large).length())
        assertNull(queue.get(large.id)!!.checksum)
        assertEquals("failed", queue.get(large.id)!!.state)
    }
    @Test fun `pending imports reserve space so concurrent copies cannot exceed the device quota`() {
        repeat(16) { item() }
        assertThrows(IllegalArgumentException::class.java) { item() }
        assertEquals(16, queue.list().size)
    }
    @Test fun `a shared source grant stays until the final import no longer needs it`() {
        provider("image/png"); source()
        val first = AtlasImportSelection.admit(context, session(), target(), uri)
        val second = AtlasImportSelection.admit(context, session(), target(), uri)
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(first))
        assertEquals(1, context.contentResolver.persistedUriPermissions.size)
        queue.delete(second.id)
        AtlasImportWorker.releaseUnusedPermission(context, second.importUri)
        assertTrue(context.contentResolver.persistedUriPermissions.isEmpty())
    }
    @Test fun `database upgrade keeps an existing camera original and adds nullable import state`() {
        val db = context.openOrCreateDatabase("atlas-outbox.db", 0, null)
        db.execSQL("CREATE TABLE uploads (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, space_id TEXT NOT NULL, " +
            "state TEXT NOT NULL, metadata TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, " +
            "sent INTEGER NOT NULL DEFAULT 0, checksum TEXT, error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE cached_spaces (session_id TEXT, space_id TEXT, data TEXT, PRIMARY KEY(session_id,space_id))")
        db.execSQL("INSERT INTO uploads VALUES ('old-photo','original-session','austin-space','queued','{}','image/jpeg',3,0,'old-hash','',1234)")
        db.version = 1; db.close()
        val file = File(context.filesDir, "atlas-captures/old-photo.jpg")
        file.parentFile!!.mkdirs(); file.writeBytes(byteArrayOf(1, 2, 3))
        val old = queue.get("old-photo")!!
        assertEquals("queued", old.state)
        assertEquals("original-session", old.sessionId)
        assertEquals("old-hash", old.checksum)
        assertNull(old.importUri)
        assertArrayEquals(byteArrayOf(1, 2, 3), queue.file(old).readBytes())
        assertEquals(2, queue.readableDatabase.version)
    }
    @Test fun `finalized import uploads exact bytes to its original space and retains the private copy`() {
        MockWebServer().use { server ->
            val access = session(server.url("/").toString())
            val imported = item(access = access)
            source()
            assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(imported))
            val saved = queue.get(imported.id)!!
            server.enqueue(MockResponse().setBody(JSONObject().put("sha256", saved.checksum).toString()))
            val upload = TestWorkerBuilder.from(context, AtlasUploadWorker::class.java, Executor { it.run() }).build()
            assertEquals(ListenableWorker.Result.success(), upload.deliver(saved, access))
            val received = server.takeRequest(5, TimeUnit.SECONDS)!!
            assertEquals("/api/sessions/workspace/atlas/spaces/austin-space/captures", received.path)
            assertEquals("image/png", received.getHeader("Content-Type"))
            assertArrayEquals(original, received.body.readByteArray())
            assertTrue(JSONObject(received.getHeader("X-Sweep-Capture")!!).isNull("captured_at"))
            assertEquals("saved", queue.get(saved.id)!!.state)
            assertArrayEquals(original, queue.file(saved).readBytes())
        }
    }
    @Test fun `worker recovers the committed copy to upload scheduling after a process interruption`() {
        WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
            .setExecutor(Executor { }).setTaskExecutor(Executor { it.run() }).build())
        val imported = item(); source()
        assertEquals(ListenableWorker.Result.success(), worker().copyOriginal(imported))
        assertEquals(ListenableWorker.Result.success(), worker(imported.id).doWork())
        val scheduled = WorkManager.getInstance(context).getWorkInfosForUniqueWork("atlas-upload-${imported.id}").get(5, TimeUnit.SECONDS)
        assertEquals(1, scheduled.size)
    }
}
