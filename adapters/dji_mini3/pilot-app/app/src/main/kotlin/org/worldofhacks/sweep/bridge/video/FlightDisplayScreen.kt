package org.worldofhacks.sweep.bridge.video

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay
import org.worldofhacks.sweep.bridge.BridgeNode
import org.worldofhacks.sweep.bridge.core.video.FlightOverlay
import org.worldofhacks.sweep.bridge.core.video.OverlayInputs
import org.worldofhacks.sweep.bridge.node.RelayConnection
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * The flight display: full-bleed FPV from the flavor's camera stream under the
 * `visual_advisory` overlay. The overlay is derived every frame from what the session
 * already knows (aircraft and RC connection, relay link, watchdog, authority, yaw) plus the
 * capture progress hook, so it needs no state of its own. The relay link keeps running in
 * the foreground service regardless of this screen.
 */
@Composable
fun FlightDisplayScreen(node: BridgeNode, session: AircraftSession, fpv: FpvSession, variant: String, onBack: () -> Unit) {
    val link by node.link.collectAsStateWithLifecycle()
    val aircraft by session.aircraft.snapshot.collectAsStateWithLifecycle()
    val attitude by fpv.attitude.collectAsStateWithLifecycle()
    val capture by fpv.captureProgress.progress.collectAsStateWithLifecycle()
    val evidence by fpv.cameraStream.evidence.collectAsStateWithLifecycle()
    val lastFrameAt by fpv.cameraStream.lastFrameAtMs.collectAsStateWithLifecycle()
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(250)
            now = System.currentTimeMillis()
        }
    }
    BackHandler(onBack = onBack)
    val inputs = OverlayInputs(
        aircraftConnected = aircraft.aircraftConnected,
        rcConnected = aircraft.rcConnected,
        relayConnected = link.connection == RelayConnection.CONNECTED || link.connection == RelayConnection.DEGRADED,
        watchdog = link.watchdog.toNodeStatus().wire,
        estop = link.estop,
        controlAuthority = link.controlAuthority,
        authorityChangeReason = link.authorityChangeReason,
        yawDeg = attitude.yawDeg,
        measuredHfovDeg = aircraft.hardware.measuredHfovDeg,
        lastFrameAgeMs = lastFrameAt?.let { (now - it).coerceAtLeast(0) },
        capture = capture,
    )
    val state = FlightOverlay.derive(inputs)
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
    ) {
        FpvSurface(camera = fpv.cameraStream, modifier = Modifier.fillMaxSize())
        FpvOverlay(state = state, evidence = evidence, fakeBanner = variant == "fake")
        OutlinedButton(
            onClick = onBack,
            modifier = Modifier
                .align(Alignment.CenterStart)
                .padding(12.dp),
        ) {
            Text("Session")
        }
    }
}
