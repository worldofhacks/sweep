package org.worldofhacks.sweep.bridge.atlas

import org.junit.Assert.*
import org.junit.Test

class AtlasPlaybackMimeTest {
    private val asset = "/atlas/spaces/place/captures/photo/memory/assets/544db565-bec9-4bf9-aae9-5ef89a696d0d/media"

    @Test fun `memory audio types including recorder codec parameters are admitted`() {
        listOf("audio/mpeg", "audio/mp4", "audio/wav", "audio/webm", "audio/ogg", "audio/flac").forEach { mime ->
            assertEquals(mime, atlasPlaybackMime(asset, mime))
        }
        assertEquals("audio/webm", atlasPlaybackMime(asset, "audio/webm;codecs=opus"))
        assertEquals("audio/wav", atlasPlaybackMime(asset, "Audio/X-WAV"))
        assertEquals("audio/mp4", atlasPlaybackMime(asset, "audio/x-m4a"))
    }

    @Test fun `audio does not expand the adjacent routes or accept malformed attachment paths`() {
        listOf("/control", "/atlas/spaces/place/captures/photo/media",
            "/atlas/spaces/place/reconstruction/build/cloud.glb", asset + "/extra",
            asset.replace("/memory/assets/", "/memory/"), asset.replace("/media", "/../media"),
            asset + "?token=not-allowed", asset.replace("544db565-bec9-4bf9-aae9-5ef89a696d0d", "invalid")).forEach { path ->
            assertNull(atlasPlaybackMime(path, "audio/wav"))
        }
    }

    @Test fun `active content and unrecognized types remain blocked for every source`() {
        listOf("text/html", "application/xhtml+xml", "image/svg+xml", "application/javascript",
            "text/xml", "application/json", "audio/html", "", "audio/unknown").forEach { mime ->
            assertNull(atlasPlaybackMime(asset, mime))
            assertNull(atlasPlaybackMime("/atlas/spaces/place/captures/photo/media", mime))
        }
    }

    @Test fun `existing photo video and world media remain admitted`() {
        listOf("image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm").forEach { mime ->
            assertEquals(mime, atlasPlaybackMime("/atlas/spaces/place/captures/photo/media", mime))
        }
        listOf("model/gltf-binary", "application/octet-stream").forEach { mime ->
            assertEquals(mime, atlasPlaybackMime("/atlas/spaces/place/reconstruction/build/cloud.glb", mime))
        }
    }
}
