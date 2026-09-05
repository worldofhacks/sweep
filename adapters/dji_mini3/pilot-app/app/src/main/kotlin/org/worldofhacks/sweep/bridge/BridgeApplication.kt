package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Context
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * One SDK session per process. `AircraftVariant` is flavor-specific: the probe flavor
 * installs the DJI runtime helper in [attachBaseContext] and starts the real `SdkSession`;
 * the fake flavor does neither and drives the same screen from a simulated session.
 */
class BridgeApplication : Application() {
    lateinit var session: AircraftSession
        private set

    override fun attachBaseContext(base: Context) {
        super.attachBaseContext(base)
        AircraftVariant.installSdk(this)
    }

    override fun onCreate() {
        super.onCreate()
        session = AircraftVariant.createSession(this)
    }
}
