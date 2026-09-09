package org.worldofhacks.sweep.bridge.atlas

import java.util.Locale

private val existingMediaTypes = setOf("image/jpeg", "image/png", "image/webp", "video/mp4",
    "video/webm", "model/gltf-binary", "application/octet-stream")
private val memoryAudioTypes = setOf("audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "audio/ogg", "audio/flac")
private val memoryAssetRoute = Regex("/atlas/spaces/[a-zA-Z0-9_-]{1,64}/captures/[a-zA-Z0-9_-]{1,64}/memory/assets/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/media")

/** MIME admission follows AtlasSession.api's credential/route check, never replaces it.
 * Audio is an inert memory attachment, not arbitrary content for our privileged WebView origin.
 */
internal fun atlasPlaybackMime(path: String, contentType: String): String? {
    val base = contentType.substringBefore(';').trim().lowercase(Locale.ROOT)
    val mime = when (base) { "audio/x-wav" -> "audio/wav"; "audio/x-m4a" -> "audio/mp4"; else -> base }
    return mime.takeIf { it in existingMediaTypes || memoryAssetRoute.matches(path) && it in memoryAudioTypes }
}
