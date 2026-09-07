package org.worldofhacks.sweep.bridge.flight

import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.flight.AxisMapping
import org.worldofhacks.sweep.bridge.core.flight.FlightReason
import org.worldofhacks.sweep.bridge.core.flight.ReportSink
import org.worldofhacks.sweep.bridge.core.flight.FlightStatus
import org.worldofhacks.sweep.bridge.node.AircraftSource

data class GroundedAuthorityQualification(
    val active: Boolean = false,
    val detail: String = "Not run",
)

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
    /** Only the fake flavor provides one; the flight card shows its buttons when present. */
    val simulation: FlightSimulation? = null,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val probes = FlightProbes(executor, aircraft, File(filesDir, "bench"), log)
    private val _groundedAuthorityQualification = MutableStateFlow(GroundedAuthorityQualification())
    val groundedAuthorityQualification: StateFlow<GroundedAuthorityQualification> = _groundedAuthorityQualification.asStateFlow()

    init {
        scope.launch { executor.status.collect { status -> onStatus(status) } }
    }

    fun qualifyGroundedAuthority() {
        _groundedAuthorityQualification.value = GroundedAuthorityQualification(active = true, detail = "Verifying Virtual Stick mode; direct owner remains UNKNOWN")
        executor.qualifyGroundedAuthority(
            object : ReportSink {
                override fun executing(detail: String?) {
                    _groundedAuthorityQualification.value = GroundedAuthorityQualification(active = true, detail = detail ?: "Verifying Virtual Stick mode; direct owner remains UNKNOWN")
                }

                override fun completed(detail: String?) {
                    _groundedAuthorityQualification.value = GroundedAuthorityQualification(detail = detail ?: "Virtual Stick mode verified; direct owner remains UNKNOWN")
                }

                override fun failed(reason: FlightReason, detail: String?) {
                    _groundedAuthorityQualification.value = GroundedAuthorityQualification(detail = "${reason.wire}: ${detail ?: "unavailable"}")
                }
            },
        )
    }

    /** The #85 axis probe's answer, applied by the operator: flips the pitch/roll mapping in the bridge. */
    fun setTransposed(transposed: Boolean) = executor.setMapping(AxisMapping(transposed = transposed))
}

/** The fake flavor's stand-ins for the RC (stick, pause, the flight controller dropping Virtual Stick). */
interface FlightSimulation {
    fun simulateRcStick()

    fun simulateRcPause()

    fun simulateVirtualStickDropped()
}
