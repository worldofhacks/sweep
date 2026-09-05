package org.worldofhacks.sweep.bridge

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** The four Setup values. The token is the per-node relay credential and HMAC key. */
data class BridgeSetup(val relayUrl: String, val session: String, val droneId: Int, val token: String) {
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
    }
}
