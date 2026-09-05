package org.worldofhacks.sweep.bridge

import android.app.Application
import com.cySdkyc.clx.Helper
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * Probe flavor. `Helper.install` in `attachBaseContext` is the MSDK v5 runtime hook, ported
 * from techmexdev/drone-maps app/src/probe/.../BuildVariantDependencies.kt.
 */
object AircraftVariant {
    fun installSdk(application: Application) {
        Helper.install(application)
    }

    fun createSession(application: Application): AircraftSession = SdkSession(application)
}
