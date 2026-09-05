package org.worldofhacks.sweep.bridge.flight

import kotlinx.coroutines.flow.StateFlow
import org.worldofhacks.sweep.bridge.core.flight.AxisMapping
import org.worldofhacks.sweep.bridge.core.flight.FakeFlightModel
import org.worldofhacks.sweep.bridge.core.flight.FlightPort
import org.worldofhacks.sweep.bridge.core.flight.FlightStatus
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.flight.StickFrame
import org.worldofhacks.sweep.bridge.node.AircraftSnapshot
import org.worldofhacks.sweep.bridge.node.AircraftSource
import org.worldofhacks.sweep.bridge.node.FakeAircraft

/**
 * The fake flavor's aircraft for Phase E: [FakeFlightModel] kinematics written into the
 * Phase C [FakeAircraft] fixture's snapshot, so the relay sees a kinematic telemetry stream
 * and the same loop, deadman, and takeover path run end to end on a phone without an
 * aircraft. Non-flight commands still go to [fake], which keeps `fake_node.py`'s camera and
 * gimbal semantics.
 */
class FakeFlightAircraft(
    val fake: FakeAircraft = FakeAircraft(),
    interpretation: AxisMapping = AxisMapping(),
) : AircraftSource, FlightPort {
    val model = FakeFlightModel(interpretation)

    @Volatile
    private var status = FlightStatus()

    override val snapshot: StateFlow<AircraftSnapshot>
        get() = fake.snapshot

    /** Stands in for the aircraft and RC link, as the Phase C Connect and Disconnect buttons do. */
    fun setConnected(aircraft: Boolean, rc: Boolean = aircraft) {
        fake.setConnected(aircraft, rc)
        mirrorConnection()
        publish()
    }

    fun place(xEast: Double = 0.0, yNorth: Double = 0.0, zUp: Double = 0.0, yawDeg: Double = 0.0, flying: Boolean = false) {
        model.place(xEast, yNorth, zUp, yawDeg, flying)
        publish()
    }

    /** The loop's status feeds the snapshot fields `node_status` and readiness report. */
    fun applyStatus(next: FlightStatus) {
        status = next
        publish()
    }

    override fun advance(nowMs: Long) {
        mirrorConnection()
        model.advance(nowMs)
        publish()
    }

    /** The Phase C fixture's own Connect and Disconnect drive the model's link too. */
    private fun mirrorConnection() {
        val current = fake.snapshot.value
        model.connected = current.aircraftConnected && current.rcConnected
    }

    override fun enableVirtualStick(onResult: (PortResult) -> Unit) = model.enableVirtualStick(onResult)

    override fun disableVirtualStick(onResult: (PortResult) -> Unit) = model.disableVirtualStick(onResult)

    override fun setAdvancedMode(enabled: Boolean) = model.setAdvancedMode(enabled)

    override fun sendStick(frame: StickFrame) = model.sendStick(frame)

    override fun startTakeoff(onResult: (PortResult) -> Unit) = model.startTakeoff(onResult)

    override fun startLanding(onResult: (PortResult) -> Unit) = model.startLanding(onResult)

    private fun publish() {
        val current = status
        fake.update { snapshot ->
            snapshot.copy(
                x = model.xEast,
                y = model.yNorth,
                z = model.zUp,
                vx = model.vEast,
                vy = model.vNorth,
                vz = model.vUp,
                state = model.flightState,
                yawDeg = model.yawDeg,
                virtualStickEnabled = current.virtualStickEnabled,
                authorityLostReason = current.authorityLostReason,
            )
        }
    }
}
