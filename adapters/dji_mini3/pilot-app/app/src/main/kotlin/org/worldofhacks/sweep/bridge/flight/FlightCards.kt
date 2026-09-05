package org.worldofhacks.sweep.bridge.flight

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.worldofhacks.sweep.bridge.core.flight.AxisProbe
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * The Phase E surfaces: the Flight card (the loop's phase, Virtual Stick state, deadman,
 * authority latch and re-arm, axis mapping, stick rate) and the First-flight probes card
 * (issue #85 procedures with sign-off). Rendered only when the session has a flight node.
 */
@Composable
fun FlightCards(session: AircraftSession) {
    val flight = session.flight ?: return
    FlightCard(flight, session as? FlightSimulation)
    ProbesCard(flight)
}

@Composable
private fun FlightCard(flight: FlightNode, simulation: FlightSimulation?) {
    val status by flight.executor.status.collectAsStateWithLifecycle()
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Flight (Virtual Stick loop)", style = MaterialTheme.typography.titleMedium)
            Text(
                "Phase: ${status.phase}" + (status.activeOperation?.let { " · $it ${shortId(status.activeCommandId)}" } ?: "") +
                    " · virtual stick ${if (status.virtualStickEnabled) "ENABLED" else "off"}",
                color = if (status.virtualStickEnabled) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
            )
            Text(
                "Loop deadman: ${status.watchdog}" + (status.settings?.let { " · stick ${it.clampedStickHz} Hz · hold ${it.holdMs} ms · failsafe ${it.failsafeMs} ms (land, never RTH)" } ?: " · thresholds not received"),
                color = if (status.watchdog == "hold" || status.watchdog == "failsafe") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
            )
            Text("Sticks sent ${status.sticksSent} at ${"%.1f".format(status.stickRateHz)} Hz" + (status.lastFrame?.let { " · last pitch ${"%.2f".format(it.pitch)} roll ${"%.2f".format(it.roll)} yaw ${"%.1f".format(it.yaw)} (${it.yawMode.name.lowercase()}) vertical ${"%.2f".format(it.verticalThrottle)}" } ?: ""), style = MaterialTheme.typography.bodySmall)
            status.landingReason?.let { Text("Landing: $it", color = MaterialTheme.colorScheme.error) }
            if (status.estopLatched) Text("Network stop latched: motion refused; landing if it stays asserted.", color = MaterialTheme.colorScheme.error)
            status.failsafeSetting?.let { Text("Flight controller failsafe setting (read only): $it", style = MaterialTheme.typography.bodySmall) }
            val lost = status.authorityLostReason
            if (lost != null) {
                Text("Control authority LOST: $lost. The RC has the aircraft; readiness reports control_authority=false until re-armed.", color = MaterialTheme.colorScheme.error)
                Button(onClick = flight.executor::rearmAuthority) { Text("Re-arm control authority") }
            } else {
                Text("Control authority: armed (RC input, pause, mode switch, or the flight controller dropping virtual stick cancels the loop).", style = MaterialTheme.typography.bodySmall)
            }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.weight(1f)) {
                    Text("Axis transpose (#85)")
                    Text(
                        if (status.mapping.transposed) "pitch = forward, roll = right (transposed from DJI's documented convention)" else "roll = forward, pitch = right (DJI's documented convention)",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Switch(checked = status.mapping.transposed, onCheckedChange = flight::setTransposed)
            }
            status.lastEvent?.let { Text("Last: $it", style = MaterialTheme.typography.bodySmall) }
            if (simulation != null) {
                Text("Fake RC (takeover drills without an aircraft)", style = MaterialTheme.typography.bodySmall)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = simulation::simulateRcStick) { Text("Stick 45%") }
                    OutlinedButton(onClick = simulation::simulateRcPause) { Text("Pause") }
                    OutlinedButton(onClick = simulation::simulateVirtualStickDropped) { Text("FC drops VS") }
                }
            }
        }
    }
}

@Composable
private fun ProbesCard(flight: FlightNode) {
    val probes = flight.probes
    val state by probes.state.collectAsStateWithLifecycle()
    var operator by remember { mutableStateOf("") }
    var note by remember { mutableStateOf("") }
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("First-flight probes (#85)", style = MaterialTheme.typography.titleMedium)
            Text("RC operator present, guarded hover, thumbs on the sticks. Each procedure writes signed-off entries to the bench log.", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { probes.benchTakeoff() }) { Text("Takeoff 1.2 m") }
                OutlinedButton(onClick = probes::benchLand) { Text("Land") }
                OutlinedButton(onClick = probes::stop) { Text("Stop hold") }
            }
            Text("Axis-transpose probe: pure pitch, then pure roll, 0.3 m/s for 1.5 s in BODY frame.", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { probes.axisProbe(AxisProbe.Field.PITCH) }) { Text("Pure pitch") }
                OutlinedButton(onClick = { probes.axisProbe(AxisProbe.Field.ROLL) }) { Text("Pure roll") }
            }
            Text("Hover drills: neutral sticks under virtual stick; then kill the relay, move a stick, or pull the LAN.", style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { probes.hoverDrill("deadman") }) { Text("Deadman") }
                OutlinedButton(onClick = { probes.hoverDrill("rc-takeover") }) { Text("RC takeover") }
                OutlinedButton(onClick = { probes.hoverDrill("relay-kill") }) { Text("Relay kill") }
            }
            state.running?.let { Text("Running: $it", color = MaterialTheme.colorScheme.error) }
            state.error?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error) }
            state.lastResult?.let { Text("Result: $it") }
            for (line in state.transitions) Text(line, style = MaterialTheme.typography.bodySmall)
            if (state.pendingSignOff != null) {
                OutlinedTextField(value = operator, onValueChange = { operator = it }, label = { Text("RC operator") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = note, onValueChange = { note = it }, label = { Text("Observed (what the aircraft did)") }, modifier = Modifier.fillMaxWidth())
                Button(enabled = operator.isNotBlank(), onClick = {
                    probes.signOff(operator.trim(), note.trim())
                    note = ""
                }) { Text("Sign off") }
            }
            Text("Signed-off entries: ${state.signedOff}" + (state.logPath?.let { " · log $it" } ?: ""), style = MaterialTheme.typography.bodySmall)
        }
    }
}

private fun shortId(id: String?): String = when {
    id == null -> ""
    id.length <= 13 -> id
    else -> id.take(8) + "…" + id.takeLast(4)
}
