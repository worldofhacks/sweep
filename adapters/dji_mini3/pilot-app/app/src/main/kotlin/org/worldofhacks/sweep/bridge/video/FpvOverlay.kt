package org.worldofhacks.sweep.bridge.video

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sin
import org.worldofhacks.sweep.bridge.core.video.CaptureState
import org.worldofhacks.sweep.bridge.core.video.OverlayState
import org.worldofhacks.sweep.bridge.core.video.SectorMark
import org.worldofhacks.sweep.bridge.core.video.StreamEvidence

private val Scrim = Color(0xB3101418)
private val Ink = Color.White
private val Accent = Color(0xFFFFB300)
private val Accepted = Color(0xFF3DDC84)
private val Alert = Color(0xFFFF5252)

/**
 * The `visual_advisory` overlay: a reticle, the coverage compass drawn heading-up so the
 * next-heading marker's offset from twelve o'clock is the yaw the pilot still owes, the
 * capture state pill, the guidance mode and pose source, and the standing note that the
 * physical RC remains primary. Sector marks are told apart by stroke first (hollow, dashed,
 * solid) and color second. Nothing here is interactive, and no element ever suggests a
 * translation.
 */
@Composable
fun FpvOverlay(state: OverlayState, evidence: StreamEvidence?, fakeBanner: Boolean, modifier: Modifier = Modifier) {
    Box(modifier = modifier.fillMaxSize()) {
        Canvas(modifier = Modifier.fillMaxSize()) { drawCompass(state, stroke = 2.dp.toPx()) }
        state.deltaLabel?.let { label ->
            Text(
                text = label,
                color = Ink,
                fontSize = 28.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier
                    .align(Alignment.Center)
                    .offset(y = 52.dp),
            )
        }
        Column(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .fillMaxWidth()
                .background(Scrim)
                .padding(horizontal = 24.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(24.dp)) {
                Text("Authority ${state.authorityLabel}", color = Ink, fontSize = 18.sp)
                Text(state.videoLabel, color = if (state.videoLabel == "Video live") Ink else Alert, fontSize = 18.sp)
                Text(state.rcPrimaryNote, color = Ink, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
            }
            for (sentence in state.degraded) {
                Text(sentence, color = Alert, fontSize = 18.sp, fontWeight = FontWeight.SemiBold)
            }
            if (fakeBanner) Text("Fake SDK: synthetic picture, no aircraft", color = Accent, fontSize = 16.sp)
        }
        Column(
            modifier = Modifier
                .align(Alignment.BottomCenter)
                .fillMaxWidth()
                .background(Scrim)
                .padding(horizontal = 24.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(20.dp), verticalAlignment = Alignment.CenterVertically) {
                CapturePill(state.captureState)
                state.progressLabel?.let { Text(it, color = Ink, fontSize = 20.sp) }
                Text(state.guidanceMode.wire, color = Ink, fontSize = 20.sp, fontWeight = FontWeight.SemiBold, fontFamily = FontFamily.Monospace)
                Text(state.poseSource, color = Ink, fontSize = 16.sp, fontFamily = FontFamily.Monospace)
                Text(state.clearanceLabel, color = Ink, fontSize = 16.sp)
                state.nextHeadingDeg?.let { Text("next ${it.roundToInt()}°", color = Accent, fontSize = 18.sp) }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
                Text(sectorLabel(state), color = Ink, fontSize = 14.sp)
                evidence?.let { Text(streamLabel(it), color = Ink, fontSize = 14.sp, fontFamily = FontFamily.Monospace) }
            }
        }
    }
}

@Composable
private fun CapturePill(state: CaptureState) {
    val alert = state == CaptureState.NEEDS_RETAKE || state == CaptureState.DISCONNECTED
    Text(
        text = state.label,
        color = if (alert) Color.Black else Ink,
        fontSize = 22.sp,
        fontWeight = FontWeight.Bold,
        modifier = Modifier
            .background(if (alert) Alert else Color(0x33FFFFFF), RoundedCornerShape(50))
            .padding(horizontal = 16.dp, vertical = 4.dp),
    )
}

private fun sectorLabel(state: OverlayState): String {
    val width = state.sectorWidthDeg.roundToInt()
    return if (state.sectorsProvisional) {
        "sectors ${state.sectors.size} × $width° (field of view unmeasured)"
    } else {
        "sectors ${state.sectors.size} × $width° (measured field of view)"
    }
}

private fun streamLabel(evidence: StreamEvidence): String {
    val rate = evidence.cadence.measuredFrameRateHz?.let { "%.1f".format(it) } ?: "-"
    val codec = evidence.profile?.let { "$it ${evidence.level ?: ""}".trim() } ?: evidence.mimeType
    return "${evidence.width}×${evidence.height} $rate Hz $codec"
}

private fun DrawScope.drawCompass(state: OverlayState, stroke: Float) {
    val center = Offset(size.width / 2f, size.height / 2f)
    val radius = min(size.width, size.height) * 0.24f
    val heading = state.headingDeg ?: 0.0
    val ringTopLeft = Offset(center.x - radius, center.y - radius)
    val ringSize = Size(radius * 2f, radius * 2f)
    for (sector in state.sectors) {
        // Canvas angles start at three o'clock; heading-up puts the current heading at twelve.
        val start = (sector.startDeg - heading - 90.0).toFloat() + SECTOR_GAP_DEG / 2f
        val sweep = (sector.endDeg - sector.startDeg).toFloat() - SECTOR_GAP_DEG
        val color = if (sector.mark == SectorMark.ACCEPTED) Accepted else Ink
        val style = when (sector.mark) {
            SectorMark.UNSEEN -> Stroke(width = stroke)
            SectorMark.WEAK -> Stroke(width = stroke * 3f, pathEffect = PathEffect.dashPathEffect(floatArrayOf(stroke * 3f, stroke * 3f)))
            SectorMark.ACCEPTED -> Stroke(width = stroke * 3f)
        }
        drawArc(color = color, startAngle = start, sweepAngle = sweep, useCenter = false, topLeft = ringTopLeft, size = ringSize, style = style)
    }
    if (state.headingDeg != null) {
        drawLine(Ink, Offset(center.x, center.y - radius - stroke * 5f), Offset(center.x, center.y - radius + stroke * 5f), strokeWidth = stroke * 2f)
        val north = Math.toRadians(-heading - 90.0)
        val northRadius = radius + stroke * 9f
        drawCircle(Ink, radius = stroke * 2f, center = Offset(center.x + northRadius * cos(north).toFloat(), center.y + northRadius * sin(north).toFloat()))
    }
    state.nextHeadingDeg?.let { next ->
        val angle = Math.toRadians(next - heading - 90.0)
        val tipRadius = radius + stroke * 4f
        val baseRadius = radius + stroke * 14f
        val spread = Math.toRadians(4.0)
        val tip = Offset(center.x + tipRadius * cos(angle).toFloat(), center.y + tipRadius * sin(angle).toFloat())
        val left = Offset(center.x + baseRadius * cos(angle - spread).toFloat(), center.y + baseRadius * sin(angle - spread).toFloat())
        val right = Offset(center.x + baseRadius * cos(angle + spread).toFloat(), center.y + baseRadius * sin(angle + spread).toFloat())
        drawPath(
            Path().apply {
                moveTo(tip.x, tip.y)
                lineTo(left.x, left.y)
                lineTo(right.x, right.y)
                close()
            },
            Accent,
        )
    }
    val reticle = stroke * 10f
    drawCircle(Ink, radius = reticle, center = center, style = Stroke(width = stroke))
    drawLine(Ink, Offset(center.x - reticle * 2f, center.y), Offset(center.x - reticle * 0.6f, center.y), strokeWidth = stroke)
    drawLine(Ink, Offset(center.x + reticle * 0.6f, center.y), Offset(center.x + reticle * 2f, center.y), strokeWidth = stroke)
    drawLine(Ink, Offset(center.x, center.y - reticle * 2f), Offset(center.x, center.y - reticle * 0.6f), strokeWidth = stroke)
    drawLine(Ink, Offset(center.x, center.y + reticle * 0.6f), Offset(center.x, center.y + reticle * 2f), strokeWidth = stroke)
    drawCircle(Ink, radius = stroke, center = center)
}

private const val SECTOR_GAP_DEG = 3f
