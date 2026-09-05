package org.worldofhacks.sweep.bridge.video

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

/**
 * The codec evidence on the session page, beside Connectivity: what the receive-stream
 * listener reports (mime type, size, nominal and measured frame rate, keyframe cadence) and
 * what the SPS says (profile, level, tier). None of it is a `node_status` field.
 */
@Composable
fun StreamEvidenceCard(fpv: FpvSession, now: Long, onOpenFlightDisplay: () -> Unit) {
    val evidence by fpv.cameraStream.evidence.collectAsStateWithLifecycle()
    val logPath by fpv.cameraStream.logPath.collectAsStateWithLifecycle()
    val lastFrameAt by fpv.cameraStream.lastFrameAtMs.collectAsStateWithLifecycle()
    val attitude by fpv.attitude.collectAsStateWithLifecycle()
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Camera stream", style = MaterialTheme.typography.titleMedium)
            Text(
                "Local FPV renders on the Flight display; the evidence below is what the stream listener and the SPS report, not a node_status field.",
                style = MaterialTheme.typography.bodySmall,
            )
            val current = evidence
            val last = lastFrameAt
            if (current == null && last == null) {
                Text("No frame received yet. Open the Flight display with the aircraft powered (fake flavor: after Connect).")
            } else if (current == null) {
                Text("Stream reset (aircraft disconnected or Surface released); last frame ${age(now, last)}.", color = MaterialTheme.colorScheme.error)
            } else {
                val cadence = current.cadence
                val measured = cadence.measuredFrameRateHz?.let { "%.1f".format(it) } ?: "-"
                Text(
                    "Stream: ${current.mimeType}${current.codec?.let { " ($it)" } ?: ""} · ${current.width}×${current.height} · " +
                        "nominal ${current.nominalFrameRateHz} Hz · measured $measured Hz",
                )
                Text(
                    "Frames ${cadence.frames} · keyframes ${cadence.keyframes} · keyframe every ${cadence.keyframeIntervalMs ?: "-"} ms " +
                        "(${cadence.keyframeIntervalFrames ?: "-"} frames; min ${cadence.keyframeIntervalMinMs ?: "-"}, max ${cadence.keyframeIntervalMaxMs ?: "-"}) · " +
                        "${current.bytes / 1024} KiB · last frame ${age(now, cadence.lastFrameAtMs)}",
                )
                val sps = current.sps
                if (sps != null) {
                    Text(
                        "SPS: ${sps.codec} profile ${sps.profileName} (idc ${sps.profileIdc}) level ${sps.level} (idc ${sps.levelIdc})" +
                            (sps.tier?.let { " tier $it" } ?: "") +
                            (sps.constraintFlags?.let { " constraints 0x%02X".format(it) } ?: ""),
                    )
                } else {
                    Text("SPS: not parsed yet" + (current.spsError?.let { " ($it)" } ?: ""), color = MaterialTheme.colorScheme.error)
                }
            }
            Text("Yaw: ${attitude.yawDeg?.let { "%.1f°".format(it) } ?: "unknown"} · overlay compass sectors follow it")
            Text("Bench log: ${logPath ?: "opens when the Flight display attaches its Surface"}", style = MaterialTheme.typography.bodySmall)
            Button(onClick = onOpenFlightDisplay) { Text("Flight display") }
        }
    }
}

private fun age(now: Long, at: Long?): String = if (at == null) "never" else "${((now - at).coerceAtLeast(0)) / 1000} s ago"
