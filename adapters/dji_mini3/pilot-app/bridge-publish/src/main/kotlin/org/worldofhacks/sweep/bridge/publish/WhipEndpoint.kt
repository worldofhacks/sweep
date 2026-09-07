package org.worldofhacks.sweep.bridge.publish

import java.net.URI

/**
 * MediaMTX endpoints for one aircraft. Stream names follow the console's `drone{droneId}`
 * mapping (`console/src/media/playback.ts`), so the phone publishes to
 * `http://<ground-station>:8889/drone{id}/whip` and the console reads
 * `http://<ground-station>:8889/drone{id}/whep`. The ground-station host defaults to the
 * relay host: both run on the ground station's LAN address unless the Setup screen says otherwise.
 */
object WhipEndpoint {
    const val DEFAULT_PORT = 8889

    fun streamName(droneId: Int): String {
        require(droneId > 0) { "drone id must be a positive integer" }
        return "drone$droneId"
    }

    /** Host of a `ws://`, `wss://`, `http://`, or `https://` URL, without port or brackets. */
    fun hostOf(url: String): String =
        url.substringAfter("://", url).substringBefore('/').substringBefore('?').let { authority ->
            val noUser = authority.substringAfterLast('@')
            if (noUser.startsWith("[")) noUser.substringAfter('[').substringBefore(']') else noUser.substringBefore(':')
        }.trim()

    /** The Setup screen's ground-station host when given, otherwise the relay's host. */
    fun groundHost(relayUrl: String, mediaHost: String?): String =
        configuredOrigin(relayUrl, mediaHost).host

    fun whipUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}/whip"

    fun whepUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}/whep"

    /** MediaMTX's built-in WHEP page for a first look in any browser. */
    fun playerUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}"

    fun origin(relayUrl: String, mediaHost: String?, mediaPort: Int): String {
        require(mediaPort in 1..65535) { "ground-station port must be between 1 and 65535" }
        val origin = configuredOrigin(relayUrl, mediaHost)
        val formatted = if (':' in origin.host) "[${origin.host}]" else origin.host
        return "${origin.scheme}://$formatted:$mediaPort"
    }

    private fun configuredOrigin(relayUrl: String, mediaHost: String?): MediaOrigin {
        val value = mediaHost?.trim()?.takeIf { it.isNotEmpty() }
        if (value == null) {
            val host = hostOf(relayUrl)
            require(host.isNotEmpty()) { "ground-station host is empty" }
            return MediaOrigin("http", host)
        }
        if (value.startsWith("https://")) {
            val uri = runCatching { URI(value) }.getOrElse { throw IllegalArgumentException("ground-station HTTPS origin is invalid") }
            require(uri.scheme == "https" && uri.userInfo == null && uri.port == -1 && uri.rawPath.isNullOrEmpty() && uri.rawQuery == null && uri.rawFragment == null) {
                "ground-station HTTPS origin must contain only a host"
            }
            val host = uri.host?.removePrefix("[")?.removeSuffix("]")
            require(!host.isNullOrEmpty()) { "ground-station HTTPS origin must contain a host" }
            return MediaOrigin("https", host)
        }
        require("://" !in value && value.none { it in "/?#@" || it.isWhitespace() }) {
            "ground-station host must be bare or an HTTPS origin"
        }
        return MediaOrigin("http", value)
    }

    private data class MediaOrigin(val scheme: String, val host: String)
}
