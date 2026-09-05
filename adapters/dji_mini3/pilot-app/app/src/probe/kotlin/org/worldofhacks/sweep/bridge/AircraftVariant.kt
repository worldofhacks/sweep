package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Intent
import com.cySdkyc.clx.Helper
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * Probe flavor. `Helper.install` in `attachBaseContext` is the MSDK v5 runtime hook, ported
 * from techmexdev/drone-maps app/src/probe/.../BuildVariantDependencies.kt.
 */
object AircraftVariant {
    /** Camera patterns are claimed only once the Phase G probe proves them on this hardware. */
    val capabilities: List<String> = listOf("flight")

    fun installSdk(application: Application) {
        Helper.install(application)
    }

    fun createSession(application: Application): AircraftSession = SdkSession(application)

    /** The probe flavor takes setup values from the Setup screen only. */
    @Suppress("UNUSED_PARAMETER")
    fun debugSetup(intent: Intent): BridgeSetup? = null
}
