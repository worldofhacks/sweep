package org.worldofhacks.sweep.bridge.session

import kotlinx.coroutines.flow.StateFlow

enum class Registration { INITIALIZING, REGISTERING, REGISTERED, FAILED }

enum class ProductConnection { DISCONNECTED, CONNECTED }

/** What the identity check read from the connected product; null until each key answers. */
data class AircraftIdentity(
    val productType: String? = null,
    val isMini3: Boolean? = null,
    val aircraftFirmware: String? = null,
    val rcFirmwareType: String? = null,
    val rcFirmwareVersions: List<String> = emptyList(),
    val rcFirmware: String? = null,
)

data class SessionEvent(val seq: Long, val name: String, val detail: String)

/**
 * Observable state of the SDK session. [generation] increments on every product connect,
 * disconnect, or change; identity results stamped with an older generation are dropped and
 * counted in [droppedCallbacks].
 */
data class SessionState(
    val registration: Registration = Registration.INITIALIZING,
    val initStage: String = "waiting for SDK",
    val registrationDetail: String? = null,
    val product: ProductConnection = ProductConnection.DISCONNECTED,
    val productId: Int? = null,
    val generation: Long = 0,
    val identity: AircraftIdentity = AircraftIdentity(),
    val droppedCallbacks: Int = 0,
    val events: List<SessionEvent> = emptyList(),
)

sealed interface ExportResult {
    data class Saved(val path: String) : ExportResult

    data class Failed(val reason: String) : ExportResult
}

interface AircraftSession {
    val state: StateFlow<SessionState>

    fun exportProbeReport(): ExportResult
}

/** Only the fake flavor's session implements this; the screen shows its buttons when present. */
interface SimulationControls {
    fun simulateRegister(success: Boolean)

    fun simulateConnect()

    fun simulateDisconnect()

    fun simulateLateCallback()
}
