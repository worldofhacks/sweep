package org.worldofhacks.sweep.bridge.publish.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState
import org.worldofhacks.sweep.bridge.publish.PublishRequest
import org.worldofhacks.sweep.bridge.publish.PublishSource
import org.worldofhacks.sweep.bridge.publish.Publisher
import org.worldofhacks.sweep.bridge.publish.WhipEndpoint

/**
 * The ground-station fields on the Setup card (Phase F): host (blank means the relay host),
 * MediaMTX WebRTC port, and the auto-start switch. Values save as they change; nothing here
 * is a secret.
 */
@Composable
fun PublishSetupFields(publisher: Publisher, relayUrl: String) {
    val settings by publisher.settings.collectAsStateWithLifecycle()
    val endpoints by publisher.endpoints.collectAsStateWithLifecycle()
    var host by remember { mutableStateOf(settings.mediaHost) }
    var port by remember { mutableStateOf(settings.mediaPort.toString()) }
    val derivedHost = runCatching { WhipEndpoint.hostOf(relayUrl) }.getOrDefault("")
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(
            value = host,
            onValueChange = {
                host = it
                publisher.saveGroundStation(it, port.trim().toIntOrNull())
            },
            label = { Text("Ground station (MediaMTX) host") },
            placeholder = { Text(derivedHost.ifBlank { "relay host" }) },
            singleLine = true,
            modifier = Modifier.weight(2f),
        )
        OutlinedTextField(
            value = port,
            onValueChange = {
                port = it
                publisher.saveGroundStation(host, it.trim().toIntOrNull())
            },
            label = { Text("WebRTC port") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            modifier = Modifier.weight(1f),
        )
    }
    Text(
        endpoints.whipUrl?.let { "Video publishes to $it" } ?: "Video publish URL: ${endpoints.error ?: "not derivable yet"}",
        style = MaterialTheme.typography.bodySmall,
    )
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.weight(1f)) {
            Text("Publish video automatically")
            Text("Starts once the relay link is joined and the aircraft is connected; stops with either.", style = MaterialTheme.typography.bodySmall)
        }
        Switch(checked = settings.autoStart, onCheckedChange = publisher::setAutoStart)
    }
}

/** The publish state, reason, metrics, and controls on the Connectivity card. */
@Composable
fun PublishRow(publisher: Publisher, now: Long) {
    val status by publisher.status.collectAsStateWithLifecycle()
    val metrics by publisher.metrics.collectAsStateWithLifecycle()
    val request by publisher.request.collectAsStateWithLifecycle()
    val settings by publisher.settings.collectAsStateWithLifecycle()
    val endpoints by publisher.endpoints.collectAsStateWithLifecycle()
    val benchFile by publisher.benchFile.collectAsStateWithLifecycle()
    val lastStop by publisher.lastStopReason.collectAsStateWithLifecycle()
    val log by publisher.log.collectAsStateWithLifecycle()
    val failed = status.state == VideoPublishState.FAILED
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            "Video publish: ${status.state.wire}" +
                (status.source?.let { " · ${it.wire}" } ?: "") +
                (status.codec?.let { " · $it" } ?: "") +
                " · attempts ${status.attempts}" +
                (status.publishingSinceMs?.let { " · up ${((now - it).coerceAtLeast(0)) / 1000} s" } ?: ""),
            color = if (failed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
        )
        if (failed) {
            Text("Publish failed: ${status.reason}" + (status.detail?.let { " ($it)" } ?: ""), color = MaterialTheme.colorScheme.error)
            status.nextAttemptAtMs?.let {
                Text("Next publish attempt in ${((it - now).coerceAtLeast(0) / 1000)} s", style = MaterialTheme.typography.bodySmall)
            } ?: Text("Not retrying until the source, aircraft, or setup changes.", style = MaterialTheme.typography.bodySmall)
        }
        metrics?.let { Text(it.compactLabel(), style = MaterialTheme.typography.bodySmall) }
        if (status.state == VideoPublishState.STOPPED) {
            lastStop?.let { Text("Last publish stop: $it", style = MaterialTheme.typography.bodySmall) }
        }
        Text(
            "Mode: " + when (request) {
                PublishRequest.AUTO -> if (settings.autoStart) "automatic" else "automatic (auto-start off)"
                PublishRequest.FORCE_ON -> "started by the pilot"
                PublishRequest.FORCE_OFF -> "stopped by the pilot"
            },
            style = MaterialTheme.typography.bodySmall,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = publisher::startNow, enabled = request != PublishRequest.FORCE_ON) { Text("Start publish") }
            OutlinedButton(onClick = publisher::stopNow, enabled = request != PublishRequest.FORCE_OFF) { Text("Stop publish") }
            if (request != PublishRequest.AUTO) OutlinedButton(onClick = publisher::resumeAuto) { Text("Auto") }
        }
        if (publisher.availableSources.size > 1) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Source:", style = MaterialTheme.typography.bodySmall)
                for (source in publisher.availableSources) {
                    val selected = settings.source == source
                    if (selected) {
                        Button(onClick = {}) { Text(source.label) }
                    } else {
                        OutlinedButton(onClick = { publisher.setSource(source) }) { Text(source.label) }
                    }
                }
            }
            if (settings.source == PublishSource.REENCODE) {
                Text("Re-encoding on the phone is an explicit choice: it decodes the SDK stream and encodes again, adding latency and load.", style = MaterialTheme.typography.bodySmall)
            }
        }
        endpoints.playerUrl?.let { Text("First look: $it (MediaMTX's WHEP page)", style = MaterialTheme.typography.bodySmall) }
        benchFile?.let { Text("Bench log: $it", style = MaterialTheme.typography.bodySmall) }
        for (line in log.asReversed().take(MAX_LOG_LINES)) {
            Text(line, style = MaterialTheme.typography.bodySmall)
        }
    }
}

private const val MAX_LOG_LINES = 4
