package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Intent
import org.worldofhacks.sweep.bridge.publish.FakePublishSources
import org.worldofhacks.sweep.bridge.publish.PublishLaunchRequest
import org.worldofhacks.sweep.bridge.publish.PublishSourceFactory
import org.worldofhacks.sweep.bridge.session.AircraftSession

/** Fake flavor: no DJI dependency, nothing to install, a simulated session drives the screen. */
object AircraftVariant {
    /** Mirrors `adapters/dji_mini3/fake_node.py` so the console shows the same registry entry. */
    val capabilities: List<String> = listOf("flight", "pano_360", "reconstruct_8")

    fun installSdk(application: Application) = Unit

    fun createSession(application: Application): AircraftSession = FakeAircraftSession(application.filesDir)

    /** Phase F: the generated test pattern proves the WHIP path without an aircraft. */
    @Suppress("UNUSED_PARAMETER")
    fun publishSources(application: Application): PublishSourceFactory = FakePublishSources()

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

    /**
     * Fake flavor only (Phase F): `--es publish_host <host> --ei publish_port <port> --es publish
     * start|stop|auto` set the ground station and drive the publisher, so the WHIP path can be
     * proven against MediaMTX with the screen off. Null when the intent carries none of them.
     */
    fun debugPublish(intent: Intent): PublishLaunchRequest? {
        val host = intent.getStringExtra(EXTRA_PUBLISH_HOST)
        val port = if (intent.hasExtra(EXTRA_PUBLISH_PORT)) intent.getIntExtra(EXTRA_PUBLISH_PORT, 0) else null
        val action = intent.getStringExtra(EXTRA_PUBLISH)
        if (host == null && port == null && action == null) return null
        return PublishLaunchRequest(mediaHost = host, mediaPort = port, action = action)
    }

    private const val EXTRA_RELAY_URL = "relay_url"
    private const val EXTRA_SESSION = "session"
    private const val EXTRA_DRONE_ID = "drone_id"
    private const val EXTRA_TOKEN = "token"
    private const val EXTRA_PUBLISH_HOST = "publish_host"
    private const val EXTRA_PUBLISH_PORT = "publish_port"
    private const val EXTRA_PUBLISH = "publish"
}
