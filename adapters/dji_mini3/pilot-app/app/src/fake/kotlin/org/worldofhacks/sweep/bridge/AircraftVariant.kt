package org.worldofhacks.sweep.bridge

import android.app.Application
import org.worldofhacks.sweep.bridge.session.AircraftSession

/** Fake flavor: no DJI dependency, nothing to install, a simulated session drives the screen. */
object AircraftVariant {
    fun installSdk(application: Application) = Unit

    fun createSession(application: Application): AircraftSession = FakeAircraftSession(application.filesDir)
}
