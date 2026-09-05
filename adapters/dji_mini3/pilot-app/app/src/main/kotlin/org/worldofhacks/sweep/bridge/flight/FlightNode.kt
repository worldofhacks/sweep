package org.worldofhacks.sweep.bridge.flight

import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.worldofhacks.sweep.bridge.core.flight.AxisMapping
import org.worldofhacks.sweep.bridge.core.flight.FlightStatus
import org.worldofhacks.sweep.bridge.node.AircraftSource

/**
 * The Phase E objects one session owns: the [FlightExecutor] (the loop on its own thread),
 * the [FlightProbes] runner for the issue #85 first-flight procedures, and the mirror that
 * feeds the loop's status back into the aircraft snapshot the relay link reports
 * (`node_status.virtual_stick_enabled`, readiness `control_authority` after a takeover).
 */
class FlightNode(
    val executor: FlightExecutor,
    aircraft: AircraftSource,
    filesDir: File,
    onStatus: (FlightStatus) -> Unit,
    log: (String) -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val probes = FlightProbes(executor, aircraft, File(filesDir, "bench"), log)

    init {
        scope.launch { executor.status.collect { status -> onStatus(status) } }
    }

    /** The #85 axis probe's answer, applied by the operator: flips the pitch/roll mapping in the bridge. */
    fun setTransposed(transposed: Boolean) = executor.setMapping(AxisMapping(transposed = transposed))
}

/** Only the fake flavor's session implements this; the flight card shows its buttons when present. */
interface FlightSimulation {
    fun simulateRcStick()

    fun simulateRcPause()

    fun simulateVirtualStickDropped()
}
