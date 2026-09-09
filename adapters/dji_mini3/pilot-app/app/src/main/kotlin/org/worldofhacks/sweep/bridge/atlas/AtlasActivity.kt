package org.worldofhacks.sweep.bridge.atlas

import android.Manifest
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.GeolocationPermissions
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import android.widget.Toast
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import androidx.work.WorkManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import org.worldofhacks.sweep.bridge.BuildConfig
import org.worldofhacks.sweep.bridge.MainActivity
import java.io.ByteArrayInputStream
import java.io.FilterInputStream
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/** Bundled shared UI + origin-restricted native capabilities. No robot session is started here. */
class AtlasActivity : ComponentActivity() {
    private lateinit var web: WebView
    private val vault by lazy { AtlasVault(this) }
    private val queue by lazy { AtlasOutbox.get(this) }
    private var locationReply: Pair<String, GeolocationPermissions.Callback>? = null
    private var exporting: String? = null
    private var importing: AtlasImportTarget? = null
    private val importDocuments = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val target = importing
        importing = null
        if (result.resultCode == RESULT_OK && target != null) {
            val selected = buildList {
                result.data?.data?.let(::add)
                result.data?.clipData?.let { clip ->
                    repeat(minOf(clip.itemCount, AtlasImportSelection.MAX_SELECTION + 1)) { add(clip.getItemAt(it).uri) }
                }
            }.distinct()
            val context = applicationContext
            // This small admission step outlives Activity teardown. The actual
            // copies belong to WorkManager, not to an Activity coroutine.
            IMPORTS.execute {
                var accepted = 0
                var failure = if (selected.size > AtlasImportSelection.MAX_SELECTION) "Choose up to 10 files at a time." else ""
                val access = runCatching { AtlasVault(context).load(target.session) }.getOrNull()
                selected.take(AtlasImportSelection.MAX_SELECTION).forEach { uri ->
                    runCatching {
                        val item = AtlasImportSelection.admit(context, access ?: error("Reconnect the original workspace."), target, uri)
                        AtlasImportWorker.enqueue(context, item.id)
                        accepted++
                    }.onFailure { failure = it.message ?: "A selected file could not be imported." }
                }
                runOnUiThread { Toast.makeText(context,
                    "$accepted selected for import. Follow progress in Uploads. $failure".trim(), Toast.LENGTH_LONG).show() }
            }
        }
    }
    private val exportDocument = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        val id = exporting
        exporting = null
        val uri = result.data?.data
        if (result.resultCode == RESULT_OK && id != null && uri != null) lifecycleScope.launch(Dispatchers.IO) {
            val outcome = runCatching {
                val item = queue.get(id) ?: error("The capture no longer exists.")
                val output = contentResolver.openOutputStream(uri, "wt") ?: error("The destination could not be opened.")
                output.use { target -> queue.file(item).inputStream().use { source -> source.copyTo(target) } }
            }
            withContext(Dispatchers.Main) { Toast.makeText(this@AtlasActivity,
                if (outcome.isSuccess) "Local file exported." else "Export failed. The local file is still in your queue.", Toast.LENGTH_LONG).show() }
        }
    }
    private val locationPermission = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        val reply = locationReply
        locationReply = null
        reply?.second?.invoke(reply.first, hasLocation(), false)
    }
    private fun hasLocation() = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        exporting = savedInstanceState?.getString("atlas-export")
        importing = savedInstanceState?.getString("atlas-import")?.let {
            runCatching { AtlasImportTarget.parse(JSONObject(it)) }.getOrNull()
        }
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            setContentView(TextView(this).apply {
                text = "Update Android System WebView to open Sweep Atlas. Your saved captures are safe."
                setPadding(32, 64, 32, 32)
            })
            return
        }
        web = WebView(this)
        val host = FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        }
        setContentView(host)
        ViewCompat.setOnApplyWindowInsetsListener(host) { view, insets ->
            applyAtlasWindowInsets(view, insets)
        }
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        web.setBackgroundColor(0xfff6f7f2.toInt())
        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            mediaPlaybackRequiresUserGesture = true
            setSupportMultipleWindows(false)
            setGeolocationEnabled(true)
        }
        val assets = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this)).build()
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // All HTML remains bundled. Attribution opens outside the privileged WebView.
                if (request.hasGesture() && request.url.toString() == "https://www.openstreetmap.org/copyright")
                    startActivity(Intent(Intent.ACTION_VIEW, request.url))
                return true
            }
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                val uri = request.url
                if (uri.scheme == "https" && uri.host == HOST && uri.port == -1) {
                    if (uri.path?.startsWith("/atlas-data/") == true) return media(uri, request.method)
                    return assets.shouldInterceptRequest(uri) ?: blocked()
                }
                if (uri.scheme == "https" && uri.host == "tile.openstreetmap.org" && uri.port == -1 &&
                    uri.path.orEmpty().matches(Regex("/[0-9]+/[0-9]+/[0-9]+\\.png")) && request.method == "GET") return null
                return blocked()
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                if (origin.trimEnd('/') != ORIGIN) { callback.invoke(origin, false, false); return }
                locationReply?.let { it.second.invoke(it.first, false, false) }
                locationReply = origin to callback
                if (hasLocation()) {
                    callback.invoke(origin, true, false); locationReply = null
                } else locationPermission.launch(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
            }
            override fun onGeolocationPermissionsHidePrompt() {
                locationReply?.let { it.second.invoke(it.first, false, false) }; locationReply = null
            }
        }
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            WebViewCompat.addWebMessageListener(web, "SweepAtlasNative", setOf(ORIGIN)) { _, message, origin, mainFrame, reply ->
                if (!mainFrame || origin.toString().trimEnd('/') != ORIGIN) return@addWebMessageListener
                val raw = message.data ?: return@addWebMessageListener
                if (raw.length > 2_000_000) return@addWebMessageListener
                val value = runCatching { JSONObject(raw) }.getOrNull() ?: return@addWebMessageListener
                val id = value.optString("id")
                if (!id.matches(Regex("[a-zA-Z0-9_-]{1,64}"))) return@addWebMessageListener
                lifecycleScope.launch {
                    val response = JSONObject().put("id", id)
                    try {
                        response.put("result", operation(value.getString("op"), value.optJSONObject("payload") ?: JSONObject()) ?: JSONObject.NULL)
                    } catch (error: Exception) {
                        response.put("error", error.message?.take(500) ?: "This action could not be completed.")
                        response.put("code", if (error is java.io.IOException) "network" else "action")
                    }
                    if (!isDestroyed && WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) reply.postMessage(response.toString())
                }
            }
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // Fixed trusted script only; page handles the open dialog/space before app exit.
                web.evaluateJavascript("window.dispatchEvent(new Event('atlas-back'))", null)
            }
        })
        lifecycleScope.launch(Dispatchers.IO) {
            queue.list().filter { it.state == "queued" || it.state == "uploading" }.forEach { AtlasUploadWorker.enqueue(applicationContext, it.id) }
            queue.list().filter { it.state == "importing" }.forEach { AtlasImportWorker.enqueue(applicationContext, it.id) }
        }
        web.loadUrl("$ORIGIN/assets/atlas/atlas-native.html")
    }

    private suspend fun operation(op: String, payload: JSONObject): Any? = when (op) {
        "getSession" -> withContext(Dispatchers.IO) { vault.current()?.publicJson() }
        "readDraft", "writeDraft", "removeDraft" -> withContext(Dispatchers.IO) {
            val session = vault.current() ?: error("Reconnect your workspace.")
            require(session.id == payload.getString("session")) { "Return to the draft's original workspace connection." }
            when (op) {
                "readDraft" -> vault.drafts.read(session)
                "writeDraft" -> { vault.drafts.write(session, payload.getJSONObject("draft"), payload.optJSONObject("previous")); true }
                else -> { vault.drafts.remove(session, payload.getJSONObject("previous")); true }
            }
        }
        "saveSession" -> withContext(Dispatchers.IO) {
            val session = AtlasSession.parse(payload)
            val path = "/atlas/spaces" + (session.space?.let { "/$it" } ?: "")
            val probe = request(session, path, "GET", null)
            check(probe.getInt("status") in 200..299) { "The workspace did not accept this invitation or key." }
            vault.save(session)
            session.publicJson()
        }
        "request" -> withContext(Dispatchers.IO) {
            val session = vault.load(payload.getString("session")) ?: error("Reconnect your workspace.")
            request(session, payload.getString("path"), payload.optString("method", "GET"), payload.optString("body").takeIf { it.isNotEmpty() })
        }
        "capture" -> {
            val request = AtlasCaptureRequest.fromPayload(payload)
            val session = withContext(Dispatchers.IO) { vault.load(payload.getString("session")) } ?: error("Reconnect your workspace.")
            session.endpoint(payload.getString("spaceId"))
            val contributor = payload.getString("contributor")
            require(contributor.matches(Regex("[a-zA-Z0-9_-]{8,64}")))
            startActivity(Intent(this, AtlasCaptureActivity::class.java)
                .putExtra("session", session.id).putExtra("space", payload.getString("spaceId"))
                .putExtra("title", payload.optString("title").take(100))
                .putExtra("request", request?.json()?.toString())
                .putExtra("contributor", contributor).putExtra("name", payload.optString("name", "Contributor").take(40)))
            true
        }
        "importMedia" -> {
            check(importing == null) { "Finish choosing the current files first." }
            val target = AtlasImportTarget.parse(payload)
            val session = withContext(Dispatchers.IO) { vault.load(target.session) } ?: error("Reconnect your workspace.")
            session.endpoint(target.space)
            importing = target
            try { importDocuments.launch(AtlasImportSelection.picker()) }
            catch (error: Exception) { importing = null; throw error }
            true
        }
        "getUploads" -> withContext(Dispatchers.IO) { JSONArray().also { result -> queue.list().forEach { result.put(it.summary()) } } }
        "retryUpload" -> withContext(Dispatchers.IO) {
            val item = queue.get(payload.getString("id")) ?: error("This capture no longer exists.")
            require(item.state == "failed" || item.state == "queued") { "This capture is already uploading or saved." }
            val previous = vault.load(item.sessionId) ?: error("The original connection is missing.")
            val current = vault.current() ?: error("Reconnect to the original workspace.")
            require(previous.baseUrl == current.baseUrl && previous.workspace == current.workspace) { "Reconnect to this capture’s original workspace before retrying." }
            current.endpoint(item.spaceId)
            require(item.checksum != null || item.importUri != null) { "This capture was interrupted before it was finalized. Export or remove the local copy." }
            queue.rebind(item.id, current)
            if (item.checksum == null) {
                queue.retryImport(item.id)
                AtlasImportWorker.enqueue(applicationContext, item.id, retryNow = true)
            } else {
                queue.state(item.id, "queued", sent = 0)
                AtlasUploadWorker.enqueue(applicationContext, item.id, retryNow = true)
            }
            true
        }
        "exportUpload" -> {
            val item = withContext(Dispatchers.IO) { queue.get(payload.getString("id")) } ?: error("This capture no longer exists.")
            require(item.state !in setOf("capturing", "importing", "uploading")) { "Wait for capture, import or upload to finish first." }
            require(queue.file(item).isFile) { "There is no local file to export." }
            exporting = item.id
            val exportName = if (item.displayName.isBlank()) "atlas-${queue.file(item).name}"
                else item.displayName.substringBeforeLast('.').ifBlank { "atlas-${item.id}" } + "." + queue.file(item).extension
            exportDocument.launch(Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType(item.mime).putExtra(Intent.EXTRA_TITLE, exportName))
            true
        }
        "removeUpload" -> {
            val id = payload.getString("id")
            val item = withContext(Dispatchers.IO) { queue.get(id) } ?: error("This capture no longer exists.")
            require(item.state !in setOf("uploading", "capturing", "importing")) { "Wait for this capture to finish first." }
            AlertDialog.Builder(this).setTitle("Remove local capture?")
                .setMessage(if (item.state == "saved") "The verified workspace copy stays available. Only this device’s copy will be removed."
                    else "This capture has not been confirmed saved to the workspace. Removing it deletes this device’s original.")
                .setNegativeButton("Keep capture", null).setPositiveButton("Remove") { _, _ ->
                    lifecycleScope.launch(Dispatchers.IO) {
                        val removed = runCatching {
                            WorkManager.getInstance(applicationContext).cancelUniqueWork("atlas-upload-$id").result.get()
                            WorkManager.getInstance(applicationContext).cancelUniqueWork("atlas-import-$id").result.get()
                            queue.delete(id)
                            AtlasImportWorker.releaseUnusedPermission(applicationContext, item.importUri)
                        }
                        if (removed.isFailure) withContext(Dispatchers.Main) {
                            Toast.makeText(this@AtlasActivity, "The local copy could not be removed.", Toast.LENGTH_LONG).show()
                        }
                    }
                }.show()
            true
        }
        "cachedSpaces" -> withContext(Dispatchers.IO) { queue.cached(payload.getString("session")) }
        "openFleet" -> { startActivity(Intent(this, MainActivity::class.java)); true }
        "exit" -> { finish(); true }
        else -> error("This capability is not available in Atlas.")
    }

    private fun request(session: AtlasSession, path: String, method: String, body: String?): JSONObject {
        require(method == "GET" || method == "POST")
        require(body == null || body.length <= 64_000)
        val request = Request.Builder().url(session.api(path)).header("Authorization", "Bearer ${session.token}")
            .method(method, if (method == "POST") (body ?: "{}").toRequestBody("application/json".toMediaType()) else null).build()
        val observedGeneration = queue.cacheGeneration(session.id)
        HTTP.newCall(request).execute().use { response ->
            val metadataCache = AtlasMetadataCache(queue)
            if (!response.isSuccessful) metadataCache.response(session, path, method, response.code, "", observedGeneration)
            val raw = response.peekBody(2_000_001).string()
            require(raw.toByteArray().size <= 2_000_000) { "The workspace response is too large." }
            if (response.isSuccessful) metadataCache.response(session, path, method, response.code, raw, observedGeneration)
            return JSONObject().put("status", response.code).put("body", raw)
        }
    }

    private fun media(uri: Uri, method: String): WebResourceResponse = try {
        require(method == "GET")
        val segments = uri.pathSegments
        val session = vault.load(segments[1]) ?: error("Missing connection")
        val path = "/" + segments.drop(2).joinToString("/")
        require(path.endsWith("/media") || path.endsWith("/cloud.glb"))
        val response = HTTP.newCall(Request.Builder().url(session.api(path))
            .header("Authorization", "Bearer ${session.token}").build()).execute()
        if (!response.isSuccessful || response.body == null) {
            response.use { AtlasMetadataCache(queue).accessRefused(session, segments.getOrNull(4), it.code) }
            blocked()
        }
        else {
            val maximum = if (path.endsWith(".glb")) 16L * 1024 * 1024 else AtlasOutbox.MAX_BYTES
            if (response.body!!.contentLength() > maximum) { response.close(); blocked() }
            else {
                val stream = object : FilterInputStream(response.body!!.byteStream()) {
                    var readBytes = 0L
                    override fun read(): Int = super.read().also { if (it >= 0) checkBound(1) }
                    override fun read(buffer: ByteArray, offset: Int, length: Int): Int =
                        `in`.read(buffer, offset, length).also { if (it > 0) checkBound(it) }
                    private fun checkBound(count: Int) { readBytes += count; if (readBytes > maximum) { close(); throw java.io.IOException("Media exceeds limit") } }
                    override fun close() { super.close(); response.close() }
                }
                // Upstream cannot return executable HTML into our privileged app origin.
                val mime = atlasPlaybackMime(path, response.body!!.contentType()?.toString().orEmpty())
                if (mime == null) {
                    stream.close(); blocked()
                } else WebResourceResponse(mime, null, 200, "OK", mapOf("Cache-Control" to "no-store", "X-Content-Type-Options" to "nosniff"), stream)
            }
        }
    } catch (_: Exception) { blocked() }

    override fun onResume() { super.onResume(); if (::web.isInitialized) web.onResume() }
    override fun onSaveInstanceState(outState: Bundle) {
        outState.putString("atlas-export", exporting)
        outState.putString("atlas-import", importing?.json()?.toString())
        super.onSaveInstanceState(outState)
    }
    override fun onPause() {
        if (::web.isInitialized) { web.evaluateJavascript("window.dispatchEvent(new Event('atlas-background'))", null); web.onPause() }
        super.onPause()
    }
    override fun onDestroy() {
        locationReply?.let { it.second.invoke(it.first, false, false) }
        if (::web.isInitialized) { web.stopLoading(); web.destroy() }
        super.onDestroy()
    }
    companion object {
        private val IMPORTS = Executors.newSingleThreadExecutor()
        const val HOST = "appassets.androidplatform.net"
        const val ORIGIN = "https://$HOST"
        private val HTTP = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
            .connectTimeout(10, TimeUnit.SECONDS).callTimeout(60, TimeUnit.SECONDS).build()
        private fun blocked() = WebResourceResponse("text/plain", "UTF-8", 403, "Unavailable",
            mapOf("Cache-Control" to "no-store"), ByteArrayInputStream("This resource is unavailable.".toByteArray()))
    }
}

internal fun applyAtlasWindowInsets(view: View, insets: WindowInsetsCompat): WindowInsetsCompat {
    // A display cutout can be taller than the status bar, especially after rotation.
    val safe = insets.getInsets(WindowInsetsCompat.Type.systemBars() or
        WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime())
    view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
    return insets
}
