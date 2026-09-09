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
    // Opening Atlas or retrying a background upload must not create a DJI session.
    // USB attach and the explicit Fleet entry still initialize these same singletons.
    val session: AircraftSession by lazy { AircraftVariant.createSession(this) }
    val node: BridgeNode by lazy { BridgeNode(this, session) }
    val publisher: Publisher by lazy { Publisher(this, node, session, AircraftVariant.publishSources(this)) }

    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        AircraftVariant.installSdk(this)
    }

}
