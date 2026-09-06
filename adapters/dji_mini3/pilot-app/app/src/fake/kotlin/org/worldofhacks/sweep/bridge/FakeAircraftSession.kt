package org.worldofhacks.sweep.bridge

import android.os.Build
import java.io.File
import kotlin.concurrent.fixedRateTimer
import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.core.frames.HardwareProfile
import org.worldofhacks.sweep.bridge.flight.FakeFlightAircraft
import org.worldofhacks.sweep.bridge.flight.FlightExecutor
import org.worldofhacks.sweep.bridge.flight.FlightNode
import org.worldofhacks.sweep.bridge.flight.FlightSimulation
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.CommandExecutor
import org.worldofhacks.sweep.bridge.node.FakeAircraft
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource
import org.worldofhacks.sweep.bridge.session.AircraftSession
import org.worldofhacks.sweep.bridge.session.ExportResult
import org.worldofhacks.sweep.bridge.session.ProbeReport
import org.worldofhacks.sweep.bridge.session.SessionModel
import org.worldofhacks.sweep.bridge.session.SessionState
import org.worldofhacks.sweep.bridge.session.SimulationControls
import org.worldofhacks.sweep.bridge.video.FakeFpv
import org.worldofhacks.sweep.bridge.video.FpvSessionHost

/**
 * Simulates the SDK callbacks so the registration and identity screen can be exercised on
 * any phone, and drives a [FakeAircraft] fixture as the relay link's aircraft: Connect and
 * Disconnect stand in for the aircraft and RC link, so the Phase C disconnect semantics
 * (readiness with `control_authority=false` while the socket stays up) can be shown without
 * hardware. The late-callback button replays an identity read stamped with the previous
 * generation, which the model must drop.
 */
class FakeAircraftSession(private val filesDir: File, phone: PhoneStatusSource? = null) : AircraftSession, SimulationControls, FpvSessionHost {
    private val model = SessionModel()
    private val fakeProductId = 1
    private val fake = FakeAircraft(
        hardware = HardwareProfile(
            aircraftModel = "fake-mini3",
            aircraftFirmware = "fake",
            rcFirmware = "fake",
            phoneModel = "${Build.MANUFACTURER} ${Build.MODEL}".trim().ifBlank { HardwareProfile.UNREPORTED },
            androidVersion = Build.VERSION.RELEASE?.ifBlank { null } ?: HardwareProfile.UNREPORTED,
            sdkVersion = "fake",
            measuredHfovDeg = null,
        ),
    )

    // Phase D hook: synthetic FPV picture, yaw sweep, and codec evidence for the flight display.
    override val fpv: FakeFpv = FakeFpv(filesDir, phone) { fake.yawDeg }

    override val state: StateFlow<SessionState> = model.state

    override val aircraft: AircraftSource
        get() = flightAircraft

    override val executor: CommandExecutor
        get() = flightExecutor

    // Phase E hook: the fixture flies through the same Virtual Stick loop the probe flavor
    // runs (FakeFlightAircraft, simple kinematics, driven by FlightExecutor), so the command
    // path, deadman, and takeover run end to end on a phone without an aircraft. Non-flight
    // commands still reach the Phase C fixture; the simulation buttons stand in for the RC.
    private val flightAircraft = FakeFlightAircraft(fake)
    private val flightExecutor = FlightExecutor(flightAircraft, flightAircraft, fallback = fake, log = { line -> model.event("Flight", line) })
    override val flight: FlightNode = FlightNode(
        flightExecutor,
        flightAircraft,
        filesDir,
        onStatus = flightAircraft::applyStatus,
        log = { line -> model.event("Probe", line) },
        simulation = object : FlightSimulation {
            override fun simulateRcStick() = flightExecutor.onTakeover("rc_takeover", "simulated left stick 45%")

            override fun simulateRcPause() = flightExecutor.onTakeover("rc_pause", "simulated pause button")

            override fun simulateVirtualStickDropped() = flightExecutor.onVirtualStickState(enabled = false, ownedBySdk = false, owner = "RC")
        },
    )

    init {
        model.initProgress("fake SDK ready")
        fixedRateTimer(name = "fake-aircraft", daemon = true, period = DRIFT_PERIOD_MS) {
            fake.advance(System.currentTimeMillis())
        }
    }

    override fun simulateRegister(success: Boolean) {
        model.initProgress("INITIALIZE_COMPLETE")
        model.registering()
        if (success) model.registerSucceeded() else model.registerFailed("simulated registration failure")
    }

    override fun simulateConnect() {
        val generation = model.productConnected(fakeProductId)
        deliverIdentity(generation)
        fake.setConnected(aircraft = true, rc = true)
        fpv.setConnected(true)
    }

    override fun simulateDisconnect() {
        model.productDisconnected(fakeProductId)
        fake.setConnected(aircraft = false, rc = false)
        fpv.setConnected(false)
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

    private companion object {
        const val DRIFT_PERIOD_MS = 100L
    }
}
