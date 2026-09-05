package org.worldofhacks.sweep.bridge

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.worldofhacks.sweep.bridge.core.flight.LocalizationConfig

/** The four Setup values. The token is the per-node relay credential and HMAC key. */
data class BridgeSetup(val relayUrl: String, val session: String, val droneId: Int, val token: String, val localization: LocalizationConfig? = null) {
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
    val localization: LocalizationConfig? = null,
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
            localization = localization(),
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
            localization = localization(),
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

    /** Stores a complete, versioned import; partial pin sets never enable localized navigation. */
    fun saveLocalization(config: LocalizationConfig?) {
        prefs.edit().apply {
            if (config == null) {
                LOCALIZATION_KEYS.forEach(::remove)
            } else {
                putString(KEY_MAP_ID, config.mapId)
                putString(KEY_GEOMETRY_ID, config.geometryId)
                putString(KEY_CAMERA_CALIBRATION_ID, config.cameraCalibrationId)
                putString(KEY_BODY_EXTRINSICS_ID, config.bodyExtrinsicsId)
                putLong(KEY_FIX_FRESHNESS_MS, config.fixFreshnessMs)
                putLong(KEY_POSE_FRESHNESS_MS, config.poseFreshnessMs)
                putLong(KEY_TRACKING_TUBE_MM, config.trackingTubeMm)
                putLong(KEY_TARGET_TOLERANCE_MM, config.targetToleranceMm)
                putLong(KEY_SETTLED_HOLD_MS, config.settledHoldMs)
                putLong(KEY_TAG_LOSS_LAND_AFTER_MS, config.tagLossLandAfterMs)
            }
        }.apply()
    }

    private fun localization(): LocalizationConfig? {
        if (!LOCALIZATION_KEYS.all(prefs::contains)) return null
        return runCatching {
            LocalizationConfig(
                mapId = prefs.getString(KEY_MAP_ID, null) ?: return null,
                geometryId = prefs.getString(KEY_GEOMETRY_ID, null) ?: return null,
                cameraCalibrationId = prefs.getString(KEY_CAMERA_CALIBRATION_ID, null) ?: return null,
                bodyExtrinsicsId = prefs.getString(KEY_BODY_EXTRINSICS_ID, null) ?: return null,
                fixFreshnessMs = prefs.getLong(KEY_FIX_FRESHNESS_MS, 0),
                poseFreshnessMs = prefs.getLong(KEY_POSE_FRESHNESS_MS, 0),
                trackingTubeMm = prefs.getLong(KEY_TRACKING_TUBE_MM, 0),
                targetToleranceMm = prefs.getLong(KEY_TARGET_TOLERANCE_MM, 0),
                settledHoldMs = prefs.getLong(KEY_SETTLED_HOLD_MS, -1),
                tagLossLandAfterMs = prefs.getLong(KEY_TAG_LOSS_LAND_AFTER_MS, 0),
            )
        }.getOrNull()
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
        private const val KEY_MAP_ID = "localization_map_id"
        private const val KEY_GEOMETRY_ID = "localization_geometry_id"
        private const val KEY_CAMERA_CALIBRATION_ID = "localization_camera_calibration_id"
        private const val KEY_BODY_EXTRINSICS_ID = "localization_body_extrinsics_id"
        private const val KEY_FIX_FRESHNESS_MS = "localization_fix_freshness_ms"
        private const val KEY_POSE_FRESHNESS_MS = "localization_pose_freshness_ms"
        private const val KEY_TRACKING_TUBE_MM = "localization_tracking_tube_mm"
        private const val KEY_TARGET_TOLERANCE_MM = "localization_target_tolerance_mm"
        private const val KEY_SETTLED_HOLD_MS = "localization_settled_hold_ms"
        private const val KEY_TAG_LOSS_LAND_AFTER_MS = "localization_tag_loss_land_after_ms"
        private val LOCALIZATION_KEYS = listOf(
            KEY_MAP_ID, KEY_GEOMETRY_ID, KEY_CAMERA_CALIBRATION_ID, KEY_BODY_EXTRINSICS_ID,
            KEY_FIX_FRESHNESS_MS, KEY_POSE_FRESHNESS_MS, KEY_TRACKING_TUBE_MM,
            KEY_TARGET_TOLERANCE_MM, KEY_SETTLED_HOLD_MS, KEY_TAG_LOSS_LAND_AFTER_MS,
        )
    }
}
