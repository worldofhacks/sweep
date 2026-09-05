package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Intent
import org.worldofhacks.sweep.bridge.session.AircraftSession

/** Fake flavor: no DJI dependency, nothing to install, a simulated session drives the screen. */
object AircraftVariant {
    /** Mirrors `adapters/dji_mini3/fake_node.py` so the console shows the same registry entry. */
    val capabilities: List<String> = listOf("flight", "pano_360", "reconstruct_8")

    fun installSdk(application: Application) = Unit

    fun createSession(application: Application): AircraftSession =
        FakeAircraftSession(application.filesDir, AndroidPhoneStatus(application))

    /**
     * Fake flavor only: the setup values may arrive as launch extras so a bench run can be
     * scripted with `adb shell am start` (`--es relay_url --es session --ei drone_id --es token`).
     * The values go straight into the encrypted store and are never logged.
     */
    fun debugSetup(intent: Intent): BridgeSetup? {
        val relayUrl = intent.getStringExtra(EXTRA_RELAY_URL) ?: return null
        val session = intent.getStringExtra(EXTRA_SESSION) ?: return null
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return null
        val droneId = intent.getIntExtra(EXTRA_DRONE_ID, 0).takeIf { it > 0 } ?: return null
        return BridgeSetup(relayUrl = relayUrl, session = session, droneId = droneId, token = token)
    }

    private const val EXTRA_RELAY_URL = "relay_url"
    private const val EXTRA_SESSION = "session"
    private const val EXTRA_DRONE_ID = "drone_id"
    private const val EXTRA_TOKEN = "token"
}
