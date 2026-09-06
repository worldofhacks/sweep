package org.worldofhacks.sweep.bridge

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.worldofhacks.sweep.bridge.core.flight.NavigationConfig
import org.worldofhacks.sweep.bridge.core.flight.NavigationConfigJson
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPins
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPinsJson

/** The four relay Setup values plus optional diagnostic pins. The token is also the HMAC key. */
data class BridgeSetup(
    val relayUrl: String,
    val session: String,
    val droneId: Int,
    val token: String,
    val localizationPins: LocalizationPins? = null,
    val navigationConfig: NavigationConfig? = null,
) {
    /** Never includes the token. */
    override fun toString(): String = "BridgeSetup(relayUrl=$relayUrl, session=$session, droneId=$droneId, token=<redacted>)"
}

/** What the Setup screen may show: everything except the token itself. */
data class SetupSummary(
    val relayUrl: String = BridgeSetupStore.DEFAULT_RELAY_URL,
    val session: String = BridgeSetupStore.DEFAULT_SESSION,
    val droneId: Int = 1,
    val tokenStored: Boolean = false,
    val tokenLength: Int = 0,
    val loaded: Boolean = false,
    val localizationPins: LocalizationPins? = null,
    val navigationConfig: NavigationConfig? = null,
) {
    val complete: Boolean
        get() = tokenStored && relayUrl.isNotBlank() && session.isNotBlank() && droneId > 0
}

/**
 * Setup values entered once and kept in androidx `EncryptedSharedPreferences` (AES256-SIV keys,
 * AES256-GCM values under an Android Keystore master key). The token is stored, compared, and
 * handed to the relay link, never logged, never shown again in full, and never placed in a URL.
 */
class BridgeSetupStore(context: Context) {
    private val application = context.applicationContext
    private val prefs: SharedPreferences by lazy { open() }

    fun load(): BridgeSetup? {
        val token = prefs.getString(KEY_TOKEN, null) ?: return null
        return BridgeSetup(
            relayUrl = prefs.getString(KEY_RELAY_URL, DEFAULT_RELAY_URL) ?: DEFAULT_RELAY_URL,
            session = prefs.getString(KEY_SESSION, DEFAULT_SESSION) ?: DEFAULT_SESSION,
            droneId = prefs.getInt(KEY_DRONE_ID, 1),
            token = token,
            localizationPins = localizationPins(),
            navigationConfig = navigationConfig(),
        )
    }

    fun summary(): SetupSummary {
        val token = prefs.getString(KEY_TOKEN, null)
        return SetupSummary(
            relayUrl = prefs.getString(KEY_RELAY_URL, DEFAULT_RELAY_URL) ?: DEFAULT_RELAY_URL,
            session = prefs.getString(KEY_SESSION, DEFAULT_SESSION) ?: DEFAULT_SESSION,
            droneId = prefs.getInt(KEY_DRONE_ID, 1),
            tokenStored = !token.isNullOrEmpty(),
            tokenLength = token?.length ?: 0,
            loaded = true,
            localizationPins = localizationPins(),
            navigationConfig = navigationConfig(),
        )
    }

    /** Saves the relay fields; a null [token] keeps the stored one. */
    fun save(relayUrl: String, session: String, droneId: Int, token: String?) {
        prefs.edit().apply {
            putString(KEY_RELAY_URL, relayUrl.trim())
            putString(KEY_SESSION, session.trim())
            putInt(KEY_DRONE_ID, droneId)
            if (!token.isNullOrEmpty()) putString(KEY_TOKEN, token)
        }.apply()
    }

    fun clearToken() {
        prefs.edit().remove(KEY_TOKEN).apply()
    }

    fun saveNavigationConfig(config: NavigationConfig?) {
        val editor = prefs.edit()
        if (config == null) editor.remove(KEY_NAVIGATION_CONFIG)
        else editor.putString(KEY_NAVIGATION_CONFIG, NavigationConfigJson.encode(config))
        editor.apply()
    }

    fun saveLocalizationPins(pins: LocalizationPins?) {
        val editor = prefs.edit()
        if (pins == null) editor.remove(KEY_LOCALIZATION_CONFIG)
        else editor.putString(KEY_LOCALIZATION_CONFIG, LocalizationPinsJson.encode(pins))
        editor.apply()
    }

    private fun localizationPins(): LocalizationPins? = prefs.getString(KEY_LOCALIZATION_CONFIG, null)
        ?.let { runCatching { LocalizationPinsJson.parse(it) }.getOrNull() }

    private fun navigationConfig(): NavigationConfig? = prefs.getString(KEY_NAVIGATION_CONFIG, null)
        ?.let { runCatching { NavigationConfigJson.parse(it) }.getOrNull() }

    @Suppress("DEPRECATION")
    private fun open(): SharedPreferences {
        val masterKey = MasterKey.Builder(application)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            application,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    companion object {
        const val DEFAULT_RELAY_URL = "ws://127.0.0.1:8000"
        const val DEFAULT_SESSION = "demo"
        private const val FILE_NAME = "bridge-setup"
        private const val KEY_RELAY_URL = "relay_url"
        private const val KEY_SESSION = "session"
        private const val KEY_DRONE_ID = "drone_id"
        private const val KEY_TOKEN = "token"
        private const val KEY_LOCALIZATION_CONFIG = "localization_diagnostic_pins_json"
        private const val KEY_NAVIGATION_CONFIG = "measured_navigation_config_json"
    }
}
