package org.worldofhacks.sweep.bridge.publish

import android.content.Context
import android.content.SharedPreferences

/**
 * The publisher's Setup values. [mediaHost] blank means "the relay host"; [mediaPort] is
 * MediaMTX's WebRTC port; [autoStart] publishes as soon as the relay link is joined and the
 * aircraft is connected; [source] is the frame source the flavor offers as default.
 */
data class PublishSettings(
    val mediaHost: String = "",
    val mediaPort: Int = WhipEndpoint.DEFAULT_PORT,
    val autoStart: Boolean = true,
    val source: PublishSource,
)

/** Plain `SharedPreferences`: nothing here is a secret (the relay token stays in `BridgeSetupStore`). */
class PublishSettingsStore(context: Context, private val defaultSource: PublishSource) {
    private val prefs: SharedPreferences = context.applicationContext.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)

    fun load(): PublishSettings = PublishSettings(
        mediaHost = prefs.getString(KEY_MEDIA_HOST, "").orEmpty(),
        mediaPort = prefs.getInt(KEY_MEDIA_PORT, WhipEndpoint.DEFAULT_PORT).takeIf { it in 1..65535 } ?: WhipEndpoint.DEFAULT_PORT,
        autoStart = prefs.getBoolean(KEY_AUTO_START, true),
        source = prefs.getString(KEY_SOURCE, null)?.let(PublishSource::fromWire) ?: defaultSource,
    )

    fun save(settings: PublishSettings) {
        prefs.edit()
            .putString(KEY_MEDIA_HOST, settings.mediaHost.trim())
            .putInt(KEY_MEDIA_PORT, settings.mediaPort)
            .putBoolean(KEY_AUTO_START, settings.autoStart)
            .putString(KEY_SOURCE, settings.source.wire)
            .apply()
    }

    private companion object {
        const val FILE_NAME = "bridge-publish"
        const val KEY_MEDIA_HOST = "media_host"
        const val KEY_MEDIA_PORT = "media_port"
        const val KEY_AUTO_START = "auto_start"
        const val KEY_SOURCE = "source"
    }
}
