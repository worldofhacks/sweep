package org.worldofhacks.sweep.bridge.atlas

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject
import java.security.MessageDigest

/** Atlas access is deliberately separate from bridge setup and the aircraft HMAC key. */
data class AtlasSession(val id: String, val baseUrl: String, val workspace: String,
    val token: String, val space: String?) {
    override fun toString() = "AtlasSession(id=$id, workspace=$workspace, token=<redacted>)"
    fun endpoint(spaceId: String, suffix: String? = null): HttpUrl {
        require(space == null || space == spaceId) { "This invitation is for another space." }
        val url = baseUrl.toHttpUrlOrNull()!!.newBuilder()
            .addPathSegments("api/sessions").addPathSegment(workspace)
            .addPathSegments("atlas/spaces").addPathSegment(spaceId)
        if (suffix != null) url.addPathSegment(suffix)
        return url.build()
    }
    fun json() = JSONObject().put("id", id).put("baseUrl", baseUrl)
        .put("sessionId", workspace).put("token", token).put("space", space ?: JSONObject.NULL)
    fun publicJson() = json().apply { remove("token") }

    /** The web surface can reach Atlas only, never the adjacent control APIs. */
    fun api(path: String): HttpUrl {
        val draftPublish = path.matches(Regex("/atlas/spaces/drafts/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/publish"))
        val memory = path.matches(Regex("/atlas/spaces/[a-zA-Z0-9_-]{1,64}/captures/[a-zA-Z0-9_-]{1,64}/memory(?:/(?:inspect|analyze|review|assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/media))?"))
        require(memory || draftPublish || path.matches(Regex("/atlas/spaces(?:/[a-zA-Z0-9_-]{1,64}(?:/(?:status|invitation|requests|presence|leave|reconstruction|surface-requests|surface-requests/[0-9a-f-]{36}/[0-9a-f]{16}/dismiss|captures/[a-zA-Z0-9_-]{1,64}/media|reconstruction/[a-zA-Z0-9_-]{1,64}/(?:cloud\\.glb|manifest\\.json)))?)?"))) {
            "This endpoint is not available in Atlas."
        }
        val segments = path.removePrefix("/atlas/spaces").split('/').filter { it.isNotEmpty() }
        require(space == null || !draftPublish && segments.firstOrNull() == space) { "This invitation is for one space only." }
        return baseUrl.toHttpUrlOrNull()!!.newBuilder().addPathSegments("api/sessions")
            .addPathSegment(workspace).addPathSegments(path.removePrefix("/")).build()
    }

    companion object {
        fun parse(value: JSONObject): AtlasSession {
            val rawUrl = value.getString("baseUrl").trim().replaceFirst(Regex("^ws:"), "http:")
                .replaceFirst(Regex("^wss:"), "https:")
            val url = rawUrl.toHttpUrlOrNull() ?: error("Use a valid workspace address.")
            require(url.username.isEmpty() && url.password.isEmpty() && url.query == null && url.fragment == null)
            require(url.isHttps || isLocalHost(url.host)) { "Use HTTPS, or an explicit local-network address for a development relay." }
            val workspace = value.getString("sessionId").trim()
            val token = value.getString("token")
            val space = if (value.isNull("space")) null else value.optString("space").takeIf { it.isNotBlank() }
            require(workspace.length in 1..128 && token.length in 8..512 && token.all { it.code in 33..126 })
            require(space == null || space.matches(Regex("[a-zA-Z0-9_-]{1,64}")))
            val base = url.toString().trimEnd('/')
            val id = sha256("$base\n$workspace\n$token\n${space.orEmpty()}".toByteArray())
            return AtlasSession(id, base, workspace, token, space)
        }
        fun isLocalHost(host: String): Boolean {
            if (host == "localhost" || host == "::1" || host.endsWith(".local")) return true
            val ip = host.split('.').map { it.toIntOrNull() ?: return false }
            if (ip.size != 4 || ip.any { it !in 0..255 }) return false
            return ip[0] == 127 || ip[0] == 10 || ip[0] == 192 && ip[1] == 168 || ip[0] == 172 && ip[1] in 16..31
        }
        fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }
    }
}

/** Existing Android Keystore-backed storage, with immutable per-credential queue bindings. */
class AtlasVault(context: Context) {
    @Suppress("DEPRECATION")
    private val preferences = EncryptedSharedPreferences.create(context.applicationContext,
        "atlas-access", MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)

    val drafts by lazy { AtlasDrafts(preferences) }

    fun current(): AtlasSession? = preferences.getString("current", null)?.let(::load)
    fun load(id: String): AtlasSession? = preferences.getString("session-$id", null)
        ?.let { AtlasSession.parse(JSONObject(it)) }
    @Synchronized fun save(session: AtlasSession) {
        val known = preferences.all.keys.count { it.startsWith("session-") }
        require(known < 16 || preferences.contains("session-${session.id}")) {
            "This device has reached its saved-invitation limit. Finish queued uploads before clearing app data."
        }
        check(preferences.edit().putString("session-${session.id}", session.json().toString())
            .putString("current", session.id).commit()) { "The connection could not be saved." }
    }
}
