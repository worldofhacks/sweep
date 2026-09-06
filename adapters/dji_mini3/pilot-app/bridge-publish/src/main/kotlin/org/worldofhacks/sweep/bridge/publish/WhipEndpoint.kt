package org.worldofhacks.sweep.bridge.publish

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
        mediaHost?.trim()?.takeIf { it.isNotEmpty() } ?: hostOf(relayUrl)

    fun whipUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}/whip"

    fun whepUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}/whep"

    /** MediaMTX's built-in WHEP page for a first look in any browser. */
    fun playerUrl(relayUrl: String, mediaHost: String?, mediaPort: Int, droneId: Int): String =
        "${origin(relayUrl, mediaHost, mediaPort)}/${streamName(droneId)}"

    fun origin(relayUrl: String, mediaHost: String?, mediaPort: Int): String {
        require(mediaPort in 1..65535) { "ground-station port must be between 1 and 65535" }
        val host = groundHost(relayUrl, mediaHost)
        require(host.isNotEmpty()) { "ground-station host is empty" }
        val formatted = if (':' in host) "[$host]" else host
        return "http://$formatted:$mediaPort"
    }
}
