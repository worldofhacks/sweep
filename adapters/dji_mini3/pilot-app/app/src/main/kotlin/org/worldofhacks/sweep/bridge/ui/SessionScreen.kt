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
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay
import org.worldofhacks.sweep.bridge.BridgeNode
import org.worldofhacks.sweep.bridge.SetupSummary
import org.worldofhacks.sweep.bridge.flight.FlightCards
import org.worldofhacks.sweep.bridge.node.AircraftSnapshot
import org.worldofhacks.sweep.bridge.node.CommandRecord
import org.worldofhacks.sweep.bridge.node.LinkState
import org.worldofhacks.sweep.bridge.node.ReadinessInput
import org.worldofhacks.sweep.bridge.node.RelayConnection
import org.worldofhacks.sweep.bridge.session.AircraftSession
import org.worldofhacks.sweep.bridge.session.ExportResult
import org.worldofhacks.sweep.bridge.session.SessionState
import org.worldofhacks.sweep.bridge.session.SimulationControls

/**
 * Phase C surfaces on one scrolling page: Setup (relay fields and the token entered once),
 * Connectivity (relay link state, epoch, thresholds, clock offset, refusals, reconnect),
 * Readiness (the three pilot toggles and the relay's answer), node status (watchdog,
 * authority, telemetry rate), the command log, then the Phase B4 registration and identity
 * cards. The flight display, capture, capabilities, and bench screens are later phases.
 */
@Composable
fun SessionScreen(node: BridgeNode, session: AircraftSession, variant: String, simulation: SimulationControls?) {
    val sdk by session.state.collectAsStateWithLifecycle()
    val setup by node.setup.collectAsStateWithLifecycle()
    val link by node.link.collectAsStateWithLifecycle()
    val running by node.running.collectAsStateWithLifecycle()
    val log by node.log.collectAsStateWithLifecycle()
    val aircraft by session.aircraft.snapshot.collectAsStateWithLifecycle()
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var exportMessage by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(500)
            now = System.currentTimeMillis()
        }
    }
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
                    if (variant == "fake") {
                        Text("Fake SDK: no aircraft is connected; the aircraft below is a kinematic fixture.", color = MaterialTheme.colorScheme.error)
                    }
                    Text("Aircraft variant: $variant. Physical RC remains primary.")
                }
            }
            item { SetupCard(setup, running, node) }
            item { ConnectivityCard(link, running, now, node) }
            item { ReadinessCard(link, node) }
            item { NodeStatusCard(link, aircraft, now) }
            item { FlightCards(session) } // Phase E: flight loop and #85 probe cards
            item { CommandsCard(link.commands, now) }
            item { StatusCard(sdk) }
            item { IdentityCard(sdk) }
            if (simulation != null) {
                item { SimulationCard(simulation) }
            }
            item {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
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
            item { Text("Relay link log", style = MaterialTheme.typography.titleMedium) }
            items(log.asReversed()) { line ->
                Text(line, style = MaterialTheme.typography.bodySmall)
            }
            item { Text("SDK events", style = MaterialTheme.typography.titleMedium) }
            items(sdk.events.asReversed(), key = { it.seq }) { event ->
                Text("${event.seq} · ${event.name} · ${event.detail}", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun SetupCard(setup: SetupSummary, running: Boolean, node: BridgeNode) {
    var relayUrl by remember(setup.loaded) { mutableStateOf(setup.relayUrl) }
    var session by remember(setup.loaded) { mutableStateOf(setup.session) }
    var droneId by remember(setup.loaded) { mutableStateOf(setup.droneId.toString()) }
    var token by remember { mutableStateOf("") }
    var replaceToken by remember { mutableStateOf(false) }
    val droneNumber = droneId.trim().toIntOrNull()
    val tokenReady = setup.tokenStored && !replaceToken || token.isNotBlank()
    val disabledReason = when {
        !setup.loaded -> "Loading the encrypted setup"
        !(relayUrl.startsWith("ws://") || relayUrl.startsWith("wss://")) -> "Relay URL must start with ws:// or wss://"
        session.isBlank() -> "Session id is required"
        droneNumber == null || droneNumber !in 1..4 -> "Aircraft number must be 1 to 4"
        !tokenReady -> "Enter the node token once"
        else -> null
    }
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Setup", style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(value = relayUrl, onValueChange = { relayUrl = it }, label = { Text("Relay URL") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(value = session, onValueChange = { session = it }, label = { Text("Session id") }, singleLine = true, modifier = Modifier.weight(2f))
                OutlinedTextField(
                    value = droneId,
                    onValueChange = { droneId = it },
                    label = { Text("Aircraft (D-0n)") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.weight(1f),
                )
            }
            if (setup.tokenStored && !replaceToken) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Node token: stored on this device (${setup.tokenLength} characters); never shown again.")
                    OutlinedButton(onClick = { replaceToken = true }) { Text("Replace token") }
                }
            } else {
                OutlinedTextField(
                    value = token,
                    onValueChange = { token = it },
                    label = { Text("Node token (entered once, stored encrypted)") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(
                    enabled = disabledReason == null,
                    onClick = {
                        node.saveSetup(relayUrl.trim(), session.trim(), droneNumber ?: 1, token.takeIf { it.isNotBlank() }, connect = true)
                        token = ""
                        replaceToken = false
                    },
                ) {
                    Text(if (running) "Save and reconnect" else "Save and connect")
                }
                if (running) OutlinedButton(onClick = node::disconnect) { Text("Disconnect") }
            }
            disabledReason?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            BatteryOptimizationRow()
        }
    }
}

@Composable
private fun BatteryOptimizationRow() {
    val context = LocalContext.current
    val power = context.getSystemService(PowerManager::class.java)
    var exempt by remember { mutableStateOf(power?.isIgnoringBatteryOptimizations(context.packageName) ?: true) }
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Column(modifier = Modifier.weight(1f)) {
            Text("Battery optimization: " + if (exempt) "exempt" else "not exempt")
            Text("Keeps the relay link alive when the screen is off; the system asks you to allow it.", style = MaterialTheme.typography.bodySmall)
        }
        if (!exempt) {
            OutlinedButton(onClick = {
                requestIgnoreBatteryOptimizations(context)
                exempt = power?.isIgnoringBatteryOptimizations(context.packageName) ?: true
            }) { Text("Allow") }
        }
    }
}

private fun requestIgnoreBatteryOptimizations(context: Context) {
    // The system dialog to allow the exemption; opens settings, never bypasses the prompt.
    val intent = runCatching {
        Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${context.packageName}"))
    }.getOrNull() ?: Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}

@Composable
private fun ConnectivityCard(link: LinkState, running: Boolean, now: Long, node: BridgeNode) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Connectivity", style = MaterialTheme.typography.titleMedium)
            val connection = when {
                !running -> "not started"
                else -> link.connection.wire + if (link.authenticated) ", authenticated" else ""
            }
            Text("Relay: $connection · attempts ${link.attempts}")
            link.relayNetwork?.let { Text("Relay network: $it") }
            Text("Membership: ${link.membership ?: "not joined"} · epoch ${link.connectionEpoch ?: "-"} · rejoins ${link.rejoins} · roster ${link.rosterVersion ?: "-"}")
            link.nodeSettings?.let {
                Text("Relay thresholds: command TTL ${it.commandTtlMs} ms · stick ${it.virtualStickHz} Hz · watchdog hold ${it.watchdogHoldMs} ms · failsafe ${it.watchdogFailsafeMs} ms")
            } ?: Text("Relay thresholds: not received (sent in auth.accepted)")
            Text("Clock offset (relay minus phone): ${link.relayOffsetMs?.let { "$it ms" } ?: "not measured"} · auth round-trip: ${link.authRoundTripMs?.let { "$it ms" } ?: "-"}")
            Text("Frames in ${link.framesIn} · out ${link.framesOut} · telemetry ${link.telemetrySent} at ${"%.1f".format(link.telemetryRateHz)} Hz · last relay frame ${age(now, link.lastRelayFrameAtMs)}")
            val nextAttempt = link.nextAttemptAtMs
            if (link.connection == RelayConnection.DISCONNECTED && nextAttempt != null) {
                Text("Next attempt in ${((nextAttempt - now).coerceAtLeast(0) / 1000)} s (backoff ${link.backoffMs} ms)")
            }
            link.lastAuthRefusal?.let {
                Text("Auth refused: ${it.reason}. ${sentence(it.reason)}", color = MaterialTheme.colorScheme.error)
            }
            if (link.halted) Text("Automatic reconnect is stopped; fix the setup or press Reconnect.", color = MaterialTheme.colorScheme.error)
            link.lastRefusal?.let { Text("Last relay refusal: ${it.reason} (${it.detail})", color = MaterialTheme.colorScheme.error) }
            link.lastError?.let { Text("Last link error: $it", style = MaterialTheme.typography.bodySmall) }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = node::reconnect) { Text("Reconnect relay") }
            }
        }
    }
}

@Composable
private fun ReadinessCard(link: LinkState, node: BridgeNode) {
    val readiness = link.readiness
    fun send(transform: (ReadinessInput) -> ReadinessInput) = node.setReadiness(transform(readiness))
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Readiness", style = MaterialTheme.typography.titleMedium)
            Text("Each toggle sends a signed readiness frame with the current connection epoch.", style = MaterialTheme.typography.bodySmall)
            ToggleRow("Home pose confirmed", "The relay records the current telemetry position as home.", readiness.homePoseConfirmed) { on ->
                send { it.copy(homePoseConfirmed = on) }
            }
            ToggleRow("Control authority", "Off: motion commands fail with authority_lost; the relay reports control_authority_missing.", readiness.controlAuthority) { on ->
                send { it.copy(controlAuthority = on) }
            }
            ToggleRow("RC safety operator present", "Off: the relay reports rc_safety_operator_missing and the arbiter refuses motion.", readiness.rcSafetyOperatorPresent) { on ->
                send { it.copy(rcSafetyOperatorPresent = on) }
            }
            Text("Relay answer: ${link.membership ?: "not joined"}" + (link.membershipReason?.let { " ($it)" } ?: ""))
            if (link.readinessReasons.isNotEmpty()) {
                Text("Readiness gates: ${link.readinessReasons.joinToString()}")
            }
            Text("Reported control authority: ${if (link.controlAuthority) "Sweep" else "RC"}" + (link.authorityChangeReason?.let { " ($it)" } ?: ""))
        }
    }
}

@Composable
private fun ToggleRow(title: String, consequence: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title)
            Text(consequence, style = MaterialTheme.typography.bodySmall)
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun NodeStatusCard(link: LinkState, aircraft: AircraftSnapshot, now: Long) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Node status", style = MaterialTheme.typography.titleMedium)
            val watchdogWord = link.watchdog.toNodeStatus().wire
            val watchdogSentence = when (watchdogWord) {
                "hold" -> "No relay activity past the hold threshold; Phase E holds neutral sticks here."
                "failsafe" -> "No relay activity past the failsafe threshold; Phase E lands indoors here, never return to home."
                else -> "Relay activity is fresh."
            }
            Text(
                "Watchdog: $watchdogWord (${link.watchdog.name.lowercase()}) · last relay activity ${age(now, link.lastRelayFrameAtMs)}. $watchdogSentence",
                color = if (watchdogWord == "nominal") MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error,
            )
            if (link.estop) Text("Network stop active (relay state estop=true).", color = MaterialTheme.colorScheme.error)
            link.nodeStatus?.let {
                Text("Last node_status: authority ${it.controlAuthority}${it.authorityChangeReason?.let { reason -> " ($reason)" } ?: ""} · virtual stick ${it.virtualStickEnabled} · video ${it.videoPublishState.wire} · phone ${it.phoneBatteryPercent}% ${it.phoneThermalState.wire}")
            }
            Text("Aircraft: ${if (aircraft.aircraftConnected) "connected" else "disconnected"} · RC: ${if (aircraft.rcConnected) "connected" else "disconnected"}")
            Text(
                "Telemetry: state ${aircraft.state} · battery ${(aircraft.battery * 100).toInt()}% · link ${(aircraft.link * 100).toInt()}% · " +
                    "position quality ${(aircraft.posQuality * 100).toInt()}% (provisional) · z ${"%.2f".format(aircraft.z)} m",
            )
            if (aircraft.keyRatesHz.isNotEmpty()) {
                Text("Measured key rates: " + aircraft.keyRatesHz.entries.sortedBy { it.key }.joinToString { "${it.key} ${"%.1f".format(it.value)} Hz" }, style = MaterialTheme.typography.bodySmall)
            }
            Text("Hardware: ${aircraft.hardware.aircraftModel} · firmware ${aircraft.hardware.aircraftFirmware} · RC ${aircraft.hardware.rcFirmware} · SDK ${aircraft.hardware.sdkVersion}", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun CommandsCard(commands: List<CommandRecord>, now: Long) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Commands (newest first)", style = MaterialTheme.typography.titleMedium)
            if (commands.isEmpty()) Text("No command received this epoch.", style = MaterialTheme.typography.bodySmall)
            for (command in commands.take(MAX_COMMAND_ROWS)) {
                Text(
                    "${command.operation} · seq ${command.seq} · epoch ${command.connectionEpoch} · roster ${command.rosterVersion} · " +
                        "${command.outcome}${command.reason?.let { " ($it)" } ?: ""} · ${shortId(command.commandId)} · ${age(now, command.updatedAtMs)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (command.outcome == "failed" || command.outcome == "dropped") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
                )
                command.detail?.let { Text("   $it", style = MaterialTheme.typography.bodySmall) }
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
            Text("Connect and Disconnect stand in for the aircraft and RC link: a disconnect sends readiness with control_authority=false while the relay socket stays up.", style = MaterialTheme.typography.bodySmall)
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

private const val MAX_COMMAND_ROWS = 20

private fun age(now: Long, at: Long?): String = if (at == null) "never" else "${((now - at).coerceAtLeast(0)) / 1000} s ago"

private fun shortId(id: String): String = if (id.length <= 13) id else id.take(8) + "…" + id.takeLast(4)

private fun sentence(reason: String): String = when (reason) {
    "session_closed" -> "The relay restarted; this session id is replay-only. Enter a new session id."
    "authentication_failed" -> "The relay did not accept this aircraft number and token."
    "adapter_already_connected" -> "Another socket is still bound to this aircraft; the relay releases it when that socket closes."
    "auth_timeout" -> "The relay did not receive the auth frame in time."
    else -> "The relay refused the connection."
}
