package org.worldofhacks.sweep.bridge.session

import kotlinx.coroutines.flow.StateFlow

data class GimbalPitchState(
    val active: Boolean = false,
    val targetDegrees: Double? = null,
    val detail: String = "Idle",
)

interface GimbalPitchControls {
    val gimbalPitch: StateFlow<GimbalPitchState>

    fun requestGimbalPitch(targetDegrees: Double)
}
