package org.worldofhacks.sweep.bridge.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.worldofhacks.sweep.bridge.session.AircraftSession
import org.worldofhacks.sweep.bridge.session.ExportResult
import org.worldofhacks.sweep.bridge.session.SessionState
import org.worldofhacks.sweep.bridge.session.SimulationControls

/** Registration, product connection, identity, and the probe-report export (Phase B4 evidence). */
@Composable
fun SessionScreen(session: AircraftSession, variant: String, simulation: SimulationControls?) {
    val state by session.state.collectAsStateWithLifecycle()
    var exportMessage by remember { mutableStateOf<String?>(null) }
    Scaffold(modifier = Modifier.fillMaxSize()) { contentPadding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(contentPadding),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("Sweep bridge node", style = MaterialTheme.typography.headlineSmall)
                    Text("Aircraft variant: $variant. Physical RC remains primary.")
                    Button(onClick = {
                        exportMessage = when (val result = session.exportProbeReport()) {
                            is ExportResult.Saved -> "Saved ${result.path}"
                            is ExportResult.Failed -> "Export failed: ${result.reason}"
                        }
                    }) {
                        Text("Export probe report")
                    }
                    exportMessage?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                }
            }
            item { StatusCard(state) }
            item { IdentityCard(state) }
            if (simulation != null) {
                item { SimulationCard(simulation) }
            }
            item { Text("Events", style = MaterialTheme.typography.titleMedium) }
            items(state.events.asReversed(), key = { it.seq }) { event ->
                Text("${event.seq} · ${event.name} · ${event.detail}", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun StatusCard(state: SessionState) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("SDK session", style = MaterialTheme.typography.titleMedium)
            Text("Registration: ${state.registration}" + (state.registrationDetail?.let { " ($it)" } ?: ""))
            Text("Init stage: ${state.initStage}")
            Text("Product: ${state.product}" + (state.productId?.let { " · id $it" } ?: ""))
            Text("Connection generation: ${state.generation} · dropped late callbacks: ${state.droppedCallbacks}")
        }
    }
}

@Composable
private fun IdentityCard(state: SessionState) {
    val identity = state.identity
    val mini3 = when (identity.isMini3) {
        null -> "not read"
        true -> "confirmed DJI_MINI_3"
        false -> "UNEXPECTED ${identity.productType}"
    }
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Identity", style = MaterialTheme.typography.titleMedium)
            Text("Product type: ${identity.productType ?: "-"} · $mini3")
            Text("Aircraft firmware: ${identity.aircraftFirmware ?: "-"}")
            Text("RC firmware type: ${identity.rcFirmwareType ?: "-"}")
            Text("RC firmware: ${identity.rcFirmware ?: "-"}")
            Text("RC firmware versions: ${identity.rcFirmwareVersions.ifEmpty { listOf("-") }.joinToString()}")
        }
    }
}

@Composable
private fun SimulationCard(simulation: SimulationControls) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Fake aircraft controls", style = MaterialTheme.typography.titleMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = { simulation.simulateRegister(success = true) }) { Text("Register") }
                OutlinedButton(onClick = { simulation.simulateRegister(success = false) }) { Text("Register fails") }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = simulation::simulateConnect) { Text("Connect") }
                OutlinedButton(onClick = simulation::simulateDisconnect) { Text("Disconnect") }
                OutlinedButton(onClick = simulation::simulateLateCallback) { Text("Late callback") }
            }
        }
    }
}
