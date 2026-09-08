package org.worldofhacks.sweep.bridge.node

import kotlin.math.sin
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.worldofhacks.sweep.bridge.core.frames.CameraProbe
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.HardwareProfile

/**
 * The `fake` flavor's aircraft: a kinematic fixture with the same command semantics as
 * `adapters/dji_mini3/fake_node.py` (takeoff lifts to `z_mm`, goto teleports, hover and
 * estop hold an airborne fixture, land returns home) so the relay's remote adapter sees the
 * same round trip from the phone as from the Python fake node. It is not a flight model and
 * its hardware profile says so.
 */
class FakeAircraft(
    hardware: HardwareProfile = FAKE_PROFILE,
    camera: CameraProbe = FAKE_CAMERA,
    connected: Boolean = false,
) : AircraftSource, CommandExecutor {
    private val startedAtMs = System.currentTimeMillis()
    private val _snapshot = MutableStateFlow(
        AircraftSnapshot(
            aircraftConnected = connected,
            rcConnected = connected,
            positionAvailable = connected,
            positionMeasuredAtMs = startedAtMs.takeIf { connected },
            velocityAvailable = connected,
            velocityMeasuredAtMs = startedAtMs.takeIf { connected },
            battery = 0.82,
            state = FlightStates.LANDED,
            link = 0.9,
            posQuality = 0.95,
            hardware = hardware,
            camera = camera,
            attitudeAvailable = connected,
            attitudeMeasuredAtMs = startedAtMs.takeIf { connected },
        ),
    )
    override val snapshot: StateFlow<AircraftSnapshot> = _snapshot.asStateFlow()

    var yawDeg: Double = 0.0
        private set

    var gimbalPitchDeg: Double = 0.0
        private set

    fun setConnected(aircraft: Boolean, rc: Boolean = aircraft) {
        val now = System.currentTimeMillis()
        _snapshot.update {
            it.copy(
                aircraftConnected = aircraft,
                rcConnected = rc,
                positionAvailable = aircraft,
                positionMeasuredAtMs = now.takeIf { aircraft },
                velocityAvailable = aircraft,
                velocityMeasuredAtMs = now.takeIf { aircraft },
                attitudeAvailable = aircraft,
                attitudeMeasuredAtMs = now.takeIf { aircraft },
            )
        }
    }

    fun setHardware(hardware: HardwareProfile) {
        _snapshot.update { it.copy(hardware = hardware) }
    }

    /** Phase E hook: the kinematic flight fixture writes its position, velocity, heading, and flight state here. */
    fun update(transform: (AircraftSnapshot) -> AircraftSnapshot) {
        val now = System.currentTimeMillis()
        _snapshot.update { stampMeasurements(transform(it), now) }
    }

    /** Deterministic drift so consecutive telemetry frames differ: slow drain, link ripple. */
    fun advance(nowMs: Long) {
        _snapshot.update { current ->
            stampMeasurements(
                current.copy(
                    battery = (current.battery - 0.00002).coerceAtLeast(0.05),
                    link = (0.9 + 0.03 * sin(nowMs / 3000.0)).coerceIn(0.0, 1.0),
                ),
                nowMs,
            )
        }
    }

    override fun execute(command: CommandFrame, report: CommandReport) {
        report.executing()
        val failure = apply(command.args)
        if (failure == null) report.completed() else report.failed(failure.first, failure.second)
    }

    private fun apply(args: CommandArgs): Pair<String, String>? {
        when (args) {
            is CommandArgs.Takeoff -> update { it.copy(z = args.zMm / 1000.0, state = FlightStates.HOVERING) }
            is CommandArgs.Goto -> update {
                it.copy(x = args.xMm / 1000.0, y = args.yMm / 1000.0, z = args.zMm / 1000.0, state = FlightStates.HOVERING)
            }
            is CommandArgs.RotateTo -> yawDeg = args.yawMdeg / 1000.0
            CommandArgs.Hover, CommandArgs.Estop -> update {
                if (it.state == FlightStates.LANDED) it else it.copy(state = FlightStates.HOVERING)
            }
            CommandArgs.Land -> update { it.copy(z = HOME_Z, state = FlightStates.LANDED) }
            CommandArgs.CameraCapabilities, CommandArgs.CameraReady -> Unit
            is CommandArgs.SetGimbalPitch -> {
                val pitch = args.pitchMdeg / 1000.0
                val camera = _snapshot.value.camera
                if (pitch < camera.gimbalPitchMinDeg || pitch > camera.gimbalPitchMaxDeg) {
                    return "camera_failure" to "gimbal pitch is outside the fixture range"
                }
                gimbalPitchDeg = pitch
            }
            is CommandArgs.CapturePanorama, is CommandArgs.CapturePhoto, is CommandArgs.RetrieveMedia ->
                return "unsupported" to "the media path lands with Phase G"
        }
        return null
    }

    private fun stampMeasurements(snapshot: AircraftSnapshot, nowMs: Long): AircraftSnapshot =
        snapshot.copy(
            positionMeasuredAtMs = nowMs.takeIf { snapshot.positionAvailable },
            velocityMeasuredAtMs = nowMs.takeIf { snapshot.velocityAvailable },
            attitudeMeasuredAtMs = nowMs.takeIf { snapshot.attitudeAvailable },
        )

    companion object {
        const val HOME_Z = 0.0

        val FAKE_PROFILE = HardwareProfile(
            aircraftModel = "fake-mini3",
            aircraftFirmware = "fake",
            rcFirmware = "fake",
            phoneModel = "fake-node",
            androidVersion = "fake",
            sdkVersion = "fake",
            measuredHfovDeg = 66.0,
        )

        val FAKE_CAMERA = CameraProbe(
            nativePanoramaModes = listOf("pano_360"),
            photoCapture = true,
            gimbalPitchMinDeg = -90.0,
            gimbalPitchMaxDeg = 30.0,
            horizontalFovDeg = 66.0,
            storageRemainingBytes = 50_000_000,
            mediaRetrieval = true,
        )
    }
}
