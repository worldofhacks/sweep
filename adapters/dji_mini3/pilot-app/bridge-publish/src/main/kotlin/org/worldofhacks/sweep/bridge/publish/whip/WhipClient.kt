package org.worldofhacks.sweep.bridge.publish.whip

import java.io.IOException
import java.util.concurrent.TimeUnit
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** A WHIP request that did not produce a session; [status] is null when no HTTP response arrived. */
class WhipException(val status: Int?, message: String, cause: Throwable? = null) : IOException(message, cause)

/** The SDP answer and, when the server sent a `Location`, the resource URL to DELETE on stop. */
data class WhipSession(val answerSdp: String, val resourceUrl: String?)

/**
 * The HTTP half of WHIP (WebRTC HTTP Ingest Protocol) against MediaMTX: POST the SDP offer
 * as `application/sdp`, take the answer from a 201 (any 2xx with a body is accepted) and the
 * resource URL from `Location`, and DELETE that resource on stop. Ported from WildBridge's
 * `WhipPublisher.postWhipOffer` / `deleteWhipResource` onto OkHttp so the app can bind it to
 * the Wi-Fi network like the relay socket and the tests can run it against MockWebServer.
 */
class WhipClient(
    client: OkHttpClient? = null,
    timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    private val credential: MediaCredential? = null,
    clientProvider: (() -> OkHttpClient?)? = null,
) : AutoCloseable {
    init {
        require(client == null || clientProvider == null) { "supply either a fixed client or a client provider, not both" }
    }

    private val ownedClient: OkHttpClient? = if (client == null && clientProvider == null) {
        OkHttpClient.Builder()
            .connectTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .writeTimeout(timeoutMs, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(false)
            .build()
    } else {
        null
    }
    private val clientProvider: () -> OkHttpClient? = clientProvider ?: { client ?: ownedClient }

    @Throws(WhipException::class)
    fun publish(whipUrl: String, offerSdp: String): WhipSession {
        val url = whipUrl.toHttpUrlOrNull() ?: throw WhipException(null, "invalid WHIP url: $whipUrl")
        // A byte body keeps the header exactly `application/sdp`; a String body would add a charset.
        val request = Request.Builder()
            .url(url)
            .post(offerSdp.toByteArray(Charsets.UTF_8).toRequestBody(SDP))
            .header("Accept", SDP_TYPE)
            .apply { credential?.let { header("Authorization", it.authorization()) } }
            .build()
        val response = try {
            val activeClient = clientProvider() ?: throw IOException("bound media network unavailable")
            activeClient.newCall(request).execute()
        } catch (error: RuntimeException) {
            throw WhipException(null, "WHIP POST $whipUrl: ${error.javaClass.simpleName}${error.message?.let { ": $it" } ?: ""}", error)
        } catch (error: IOException) {
            throw WhipException(null, "WHIP POST $whipUrl: ${error.javaClass.simpleName}${error.message?.let { ": $it" } ?: ""}", error)
        }
        response.use {
            val body = it.body?.string().orEmpty()
            if (it.code !in 200..299) {
                val snippet = body.trim().take(MAX_ERROR_BODY)
                throw WhipException(it.code, "WHIP POST $whipUrl: HTTP ${it.code}" + if (snippet.isEmpty()) "" else " $snippet")
            }
            if (body.isBlank()) throw WhipException(it.code, "WHIP POST $whipUrl: HTTP ${it.code} without an SDP answer")
            return WhipSession(answerSdp = body, resourceUrl = it.header("Location")?.let { location -> resolveResource(url, location) })
        }
    }

    /** Best effort: returns the HTTP status, or null when the request itself failed. Never throws. */
    fun delete(resourceUrl: String): Int? {
        val url = resourceUrl.toHttpUrlOrNull() ?: return null
        return try {
            val request = Request.Builder().url(url).delete()
                .apply { credential?.let { header("Authorization", it.authorization()) } }
                .build()
            val activeClient = clientProvider() ?: return null
            activeClient.newCall(request).execute().use { it.code }
        } catch (_: RuntimeException) {
            null
        } catch (_: IOException) {
            null
        }
    }

    override fun close() {
        ownedClient?.let {
            it.dispatcher.cancelAll()
            it.dispatcher.executorService.shutdown()
            it.connectionPool.evictAll()
        }
    }

    companion object {
        const val SDP_TYPE = "application/sdp"
        const val DEFAULT_TIMEOUT_MS = 10_000L
        private const val MAX_ERROR_BODY = 200
        private val SDP = SDP_TYPE.toMediaType()

        /**
         * `Location` may be absolute, an absolute path (`/drone1/whip/<id>`), or a bare id;
         * a bare id is a child of the WHIP endpoint, which is how MediaMTX names its sessions.
         */
        fun resolveResource(whipUrl: HttpUrl, location: String): String? {
            val trimmed = location.trim()
            if (trimmed.isEmpty()) return null
            val resolved = if ('/' !in trimmed) {
                whipUrl.newBuilder().addPathSegment(trimmed).build()
            } else {
                whipUrl.resolve(trimmed) ?: return null
            }
            // Never forward the Basic credential to an origin selected by a Location header.
            if (resolved.scheme != whipUrl.scheme || resolved.host != whipUrl.host || resolved.port != whipUrl.port) return null
            return resolved.toString()
        }
    }
}
