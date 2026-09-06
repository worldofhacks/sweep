package org.worldofhacks.sweep.bridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.core.content.ContextCompat
import org.worldofhacks.sweep.bridge.publish.ui.LocalPublisher
import org.worldofhacks.sweep.bridge.session.SimulationControls
import org.worldofhacks.sweep.bridge.ui.SessionScreen

class MainActivity : ComponentActivity() {
    private val requestNotifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        enableEdgeToEdge()
        val app = application as BridgeApplication
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        handleIntent(intent, coldStart = savedInstanceState == null)
        setContent {
            // Phase F hook: the publish cards read the publisher from this CompositionLocal.
            CompositionLocalProvider(LocalPublisher provides app.publisher) {
                MaterialTheme {
                    SessionScreen(
                        node = app.node,
                        session = app.session,
                        variant = BuildConfig.AIRCRAFT,
                        simulation = app.session as? SimulationControls,
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent, coldStart = false)
    }

    /** Fake-flavor debug extras save the setup and connect; otherwise a stored setup reconnects on launch. */
    private fun handleIntent(intent: Intent?, coldStart: Boolean) {
        val app = application as BridgeApplication
        val node = app.node
        val debug = intent?.let { AircraftVariant.debugSetup(it) }
        when {
            debug != null -> node.saveSetup(debug.relayUrl, debug.session, debug.droneId, debug.token, connect = true)
            coldStart -> node.connectIfConfigured()
        }
        // Phase F hook: fake-flavor publish extras (ground-station host and port, start/stop/auto).
        intent?.let { AircraftVariant.debugPublish(it) }?.let { app.publisher.applyLaunchRequest(it) }
    }
}
