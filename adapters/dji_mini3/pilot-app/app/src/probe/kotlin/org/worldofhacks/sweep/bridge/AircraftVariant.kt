package org.worldofhacks.sweep.bridge

import android.app.Application
import android.content.Intent
import android.util.Log
import com.cySdkyc.clx.Helper
import org.worldofhacks.sweep.bridge.publish.DjiPublishSources
import org.worldofhacks.sweep.bridge.publish.PublishLaunchRequest
import org.worldofhacks.sweep.bridge.publish.PublishSourceFactory
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * Probe flavor. `Helper.install` in `attachBaseContext` is the MSDK v5 runtime hook, ported
 * from techmexdev/drone-maps app/src/probe/.../BuildVariantDependencies.kt.
 */
object AircraftVariant {
    /**
     * `reconstruct_8` is the pattern the Phase G camera path drives (photo per heading, media
     * retrieval); `pano_360` stays unclaimed because a native panorama yaws the aircraft
     * outside the Virtual Stick loop and `capture_panorama` answers `camera_unsupported`.
     */
    val capabilities: List<String> = listOf("flight", "reconstruct_8")

    fun installSdk(application: Application) {
        Helper.install(application)
    }

    fun createSession(application: Application): AircraftSession = SdkSession(application)

    /** Phase F: the SDK's encoded frames by default, re-encode on the phone by explicit choice. */
    @Suppress("UNUSED_PARAMETER")
    fun publishSources(application: Application): PublishSourceFactory = DjiPublishSources { Log.i("SweepPublish", it) }

    /** The probe flavor takes setup values from the Setup screen only. */
    @Suppress("UNUSED_PARAMETER")
    fun debugSetup(intent: Intent): BridgeSetup? = null

    /** The probe flavor takes the publish values from the Setup screen only. */
    @Suppress("UNUSED_PARAMETER")
    fun debugPublish(intent: Intent): PublishLaunchRequest? = null
}
