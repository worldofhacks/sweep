package org.worldofhacks.sweep.bridge

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.material3.MaterialTheme
import org.worldofhacks.sweep.bridge.session.SimulationControls
import org.worldofhacks.sweep.bridge.ui.SessionScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        enableEdgeToEdge()
        val session = (application as BridgeApplication).session
        setContent {
            MaterialTheme {
                SessionScreen(
                    session = session,
                    variant = BuildConfig.AIRCRAFT,
                    simulation = session as? SimulationControls,
                )
            }
        }
    }
}
