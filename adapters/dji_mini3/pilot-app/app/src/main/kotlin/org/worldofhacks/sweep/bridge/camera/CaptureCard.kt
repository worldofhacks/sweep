package org.worldofhacks.sweep.bridge.camera

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.worldofhacks.sweep.bridge.core.frames.RetrievalStatus
import org.worldofhacks.sweep.bridge.core.video.CapturePhase
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * The Phase G surface on the session page: what the camera path is doing, the camera and
 * storage facts it reports in `capabilities`, and every file captured this epoch with its
 * aircraft reference, its path on the phone once retrieved, and the SHA-256 the relay holds.
 * Rendered only when the session has a camera path.
 */
@Composable
fun CaptureCard(session: AircraftSession, now: Long) {
    val camera = session.camera ?: return
    val status by camera.status.collectAsStateWithLifecycle()
    val progress by camera.progress.collectAsStateWithLifecycle()
    val facts by camera.facts.collectAsStateWithLifecycle()
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Capture (camera and media path)", style = MaterialTheme.typography.titleMedium)
            val phaseWord = when (val phase = progress.phase) {
                CapturePhase.Idle -> "Ready"
                is CapturePhase.Capturing -> "Capturing ${phase.done} of ${phase.total}"
                is CapturePhase.Downloading -> "Downloading file ${phase.file} of ${phase.of}"
                is CapturePhase.NeedsRetake -> "Needs retake"
            }
            Text(
                "$phaseWord · ${status.phase}" + (status.activeOperation?.let { " $it" } ?: "") +
                    " · gimbal pitch ${status.gimbalPitchDeg?.let { "%.1f°".format(it) } ?: "unreported"}",
            )
            Text(
                "Camera ${if (facts.cameraConnected) "connected" else "absent"} · photo mode ${if (facts.photoMode) "yes" else "no"} · " +
                    "storage ${if (facts.storageInserted) "inserted" else "missing"} " +
                    (facts.storageRemainingBytes?.let { "${it / 1_000_000} MB free" } ?: "(space unreported)") +
                    " · gimbal range ${facts.gimbalPitchMinDeg?.let { "%.0f".format(it) } ?: "?"}..${facts.gimbalPitchMaxDeg?.let { "%.0f".format(it) } ?: "?"}°",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "Native panorama: not driven by this node (capture_panorama answers camera_unsupported); " +
                    if (facts.panoramaAdvertised.isEmpty()) "the camera advertises none." else "the camera advertises ${facts.panoramaAdvertised}.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (progress.acceptedHeadingsDeg.isNotEmpty()) {
                Text("Accepted headings: " + progress.acceptedHeadingsDeg.joinToString { "%.0f°".format(it) }, style = MaterialTheme.typography.bodySmall)
            }
            if (status.files.isEmpty()) {
                Text("No file captured this connection epoch.", style = MaterialTheme.typography.bodySmall)
            }
            for (file in status.files.asReversed().take(MAX_FILE_ROWS)) {
                val record = file.record
                val where = when (record.retrievalStatus) {
                    RetrievalStatus.PENDING -> "on the aircraft (${file.camera.name}, ${file.camera.sizeBytes} bytes), not downloaded"
                    RetrievalStatus.COMPLETED -> "${file.path} · sha256 ${record.checksumSha256}"
                    else -> record.retrievalStatus.wire
                }
                Text(
                    "${file.fileId} · heading ${"%.0f".format(record.actualYawDeg)}° gimbal ${"%.1f".format(record.gimbalPitchDeg)}° · " +
                        "pose ${"%.2f".format(record.pose.x)} ${"%.2f".format(record.pose.y)} ${"%.2f".format(record.pose.z)} · ${age(now, record.timestampMs)}",
                    style = MaterialTheme.typography.bodySmall,
                )
                Text("   $where", style = MaterialTheme.typography.bodySmall)
            }
            status.lastEvent?.let { Text("Last: $it", style = MaterialTheme.typography.bodySmall) }
        }
    }
}

private const val MAX_FILE_ROWS = 10

private fun age(now: Long, at: Long): String = "${((now - at).coerceAtLeast(0)) / 1000} s ago"
