package org.worldofhacks.sweep.bridge.session

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * SDK-agnostic session state machine shared by the probe and fake flavors.
 *
 * Every product connect, disconnect, or change bumps the connection generation. Identity
 * reads issued for one generation carry that number back through [identity]; if the product
 * changed in between, the result is dropped instead of overwriting the newer product's
 * identity. This is the fence that closes the callback race the earlier scaffold had.
 */
class SessionModel(initial: SessionState = SessionState()) {
    private val lock = Any()
    private val _state = MutableStateFlow(initial)
    val state: StateFlow<SessionState> = _state.asStateFlow()

    val current: SessionState
        get() = _state.value

    private var eventSeq = 0L

    fun initProgress(stage: String) = update("SDK initialization", stage) { it.copy(initStage = stage) }

    fun registering() = update("SDK registration", "registerApp called") {
        it.copy(registration = Registration.REGISTERING)
    }

    fun registerSucceeded() = update("SDK registration", "succeeded") {
        it.copy(registration = Registration.REGISTERED, registrationDetail = null)
    }

    fun registerFailed(detail: String) = update("SDK registration", "failed: $detail") {
        it.copy(registration = Registration.FAILED, registrationDetail = detail)
    }

    /** Returns the new generation; pass it to the identity reads issued for this connection. */
    fun productConnected(productId: Int): Long = bump("Product connection", "connected; product id $productId") {
        it.copy(product = ProductConnection.CONNECTED, productId = productId, identity = AircraftIdentity())
    }

    fun productDisconnected(productId: Int): Long = bump("Product connection", "disconnected; product id $productId") {
        it.copy(product = ProductConnection.DISCONNECTED, productId = null, identity = AircraftIdentity())
    }

    fun productChanged(productId: Int): Long = bump("Product identity", "changed; product id $productId") {
        it.copy(product = ProductConnection.CONNECTED, productId = productId, identity = AircraftIdentity())
    }

    /** Applies an identity read only if [generation] is still current; returns whether it was applied. */
    fun identity(generation: Long, name: String, detail: String, transform: (AircraftIdentity) -> AircraftIdentity): Boolean {
        synchronized(lock) {
            val now = current
            if (generation != now.generation) {
                set(
                    now.copy(droppedCallbacks = now.droppedCallbacks + 1),
                    "Dropped callback",
                    "$name from generation $generation arrived during generation ${now.generation}",
                )
                return false
            }
            set(now.copy(identity = transform(now.identity)), name, detail)
            return true
        }
    }

    fun event(name: String, detail: String) = update(name, detail) { it }

    private fun update(name: String, detail: String, transform: (SessionState) -> SessionState) {
        synchronized(lock) { set(transform(current), name, detail) }
    }

    private fun bump(name: String, detail: String, transform: (SessionState) -> SessionState): Long {
        synchronized(lock) {
            val next = current.generation + 1
            set(transform(current).copy(generation = next), name, "$detail; generation $next")
            return next
        }
    }

    private fun set(state: SessionState, name: String, detail: String) {
        eventSeq += 1
        val events = (state.events + SessionEvent(eventSeq, name, detail)).takeLast(MAX_EVENTS)
        _state.value = state.copy(events = events)
    }

    private companion object {
        const val MAX_EVENTS = 250
    }
}
