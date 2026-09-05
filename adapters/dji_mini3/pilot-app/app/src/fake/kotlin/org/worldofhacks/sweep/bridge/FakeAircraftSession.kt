package org.worldofhacks.sweep.bridge

import android.os.Build
import java.io.File
import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.session.AircraftSession
import org.worldofhacks.sweep.bridge.session.ExportResult
import org.worldofhacks.sweep.bridge.session.ProbeReport
import org.worldofhacks.sweep.bridge.session.SessionModel
import org.worldofhacks.sweep.bridge.session.SessionState
import org.worldofhacks.sweep.bridge.session.SimulationControls

/**
 * Simulates the SDK callbacks so the registration and identity screen can be exercised on
 * any phone. The late-callback button replays an identity read stamped with the previous
 * generation, which the model must drop.
 */
class FakeAircraftSession(private val filesDir: File) : AircraftSession, SimulationControls {
    private val model = SessionModel()
    private val fakeProductId = 1

    override val state: StateFlow<SessionState> = model.state

    init {
        model.initProgress("fake SDK ready")
    }

    override fun simulateRegister(success: Boolean) {
        model.initProgress("INITIALIZE_COMPLETE")
        model.registering()
        if (success) model.registerSucceeded() else model.registerFailed("simulated registration failure")
    }

    override fun simulateConnect() {
        val generation = model.productConnected(fakeProductId)
        deliverIdentity(generation)
    }

    override fun simulateDisconnect() {
        model.productDisconnected(fakeProductId)
    }

    override fun simulateLateCallback() {
        deliverIdentity(model.current.generation - 1)
    }

    private fun deliverIdentity(generation: Long) {
        model.identity(generation, "Product identity", "DJI_MINI_3 (fake)") {
            it.copy(productType = "DJI_MINI_3", isMini3 = true)
        }
        model.identity(generation, "Aircraft firmware", "fake aircraft firmware") {
            it.copy(aircraftFirmware = "fake-aircraft-firmware")
        }
        model.identity(generation, "Remote controller identity", "RC firmware profile DJI_MINI_3 (fake)") {
            it.copy(
                rcFirmwareType = "DJI_MINI_3",
                rcFirmwareVersions = listOf("fake-rc-firmware"),
                rcFirmware = "fake-rc-firmware",
            )
        }
    }

    override fun exportProbeReport(): ExportResult = ProbeReport.write(
        directory = filesDir,
        state = model.current,
        environment = ProbeReport.Environment(
            aircraftVariant = BuildConfig.AIRCRAFT,
            applicationId = BuildConfig.APPLICATION_ID,
            appVersion = "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
            msdkVersion = "none (fake flavor)",
            phone = "${Build.MANUFACTURER} ${Build.MODEL}".trim(),
            android = "${Build.VERSION.RELEASE} / API ${Build.VERSION.SDK_INT} / build ${Build.DISPLAY}",
        ),
        exportedAtMs = System.currentTimeMillis(),
    )
}
