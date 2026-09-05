package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Context
import org.worldofhacks.sweep.bridge.publish.Publisher
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * One SDK session and one relay node per process. `AircraftVariant` is flavor-specific: the
 * probe flavor installs the DJI runtime helper in [attachBaseContext] and starts the real
 * `SdkSession`; the fake flavor does neither and drives the same screen from a simulated
 * session. [node] owns the relay link; [BridgeService] keeps it alive in the foreground;
 * [publisher] owns the WHIP video session (Phase F) and follows the link and the aircraft.
 */
class BridgeApplication : Application() {
    lateinit var session: AircraftSession
        private set

    lateinit var node: BridgeNode
        private set

    lateinit var publisher: Publisher
        private set

    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        AircraftVariant.installSdk(this)
    }

    override fun onCreate() {
        super.onCreate()
        session = AircraftVariant.createSession(this)
        node = BridgeNode(this, session)
        publisher = Publisher(this, node, session, AircraftVariant.publishSources(this))
    }
}
