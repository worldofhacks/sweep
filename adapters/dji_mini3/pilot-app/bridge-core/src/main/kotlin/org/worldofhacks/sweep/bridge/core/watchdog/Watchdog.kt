package org.worldofhacks.sweep.bridge.core.watchdog

import org.worldofhacks.sweep.bridge.core.admission.Clock

/** Wire names are the `node_status.watchdog_state` values. */
enum class WatchdogState(val wire: String) {
    DISARMED("disarmed"),
    ARMED("armed"),
    HOLD("hold"),
    FAILSAFE("failsafe");

    companion object {
        fun fromWire(value: String): WatchdogState? = entries.firstOrNull { it.wire == value }
    }
}

/** Same invariant as `adapters.protocols.WatchdogConfig`: `0 <= hold < failsafe`. */
data class WatchdogConfig(val holdMs: Long, val failsafeMs: Long) {
    init {
        require(holdMs >= 0 && failsafeMs > holdMs) { "watchdog thresholds must satisfy 0 <= hold < failsafe" }
    }
}

/** Acknowledgement reasons a node reports when the watchdog acts. */
enum class WatchdogReason(val wire: String) {
    WATCHDOG_HOLD("watchdog_hold"),
    WATCHDOG_FAILSAFE("watchdog_failsafe"),
}

data class WatchdogTransition(
    val from: WatchdogState,
    val to: WatchdogState,
    val reason: WatchdogReason?,
    val elapsedMs: Long,
)

/**
 * Node-local link watchdog (Phase E3). While armed, any heartbeat or admitted command
 * resets the activity clock. [poll] moves to `hold` once `hold_ms` pass without activity
 * and to `failsafe` once `failsafe_ms` pass; a long silence jumps straight to failsafe.
 * Activity during `hold` recovers to `armed`; `failsafe` is terminal until [arm] is
 * called again, mirroring `NodeWatchdogState.action_at` in `adapters/protocols.py`.
 */
class Watchdog(val config: WatchdogConfig, private val clock: Clock) {
    var state: WatchdogState = WatchdogState.DISARMED
        private set

    var lastActivityMs: Long? = null
        private set

    private var queued: WatchdogTransition? = null

    fun arm() {
        state = WatchdogState.ARMED
        lastActivityMs = clock.nowMs()
        queued = null
    }

    fun disarm() {
        state = WatchdogState.DISARMED
        lastActivityMs = null
        queued = null
    }

    fun heartbeat() = activity()

    fun command() = activity()

    private fun activity() {
        when (state) {
            WatchdogState.ARMED -> lastActivityMs = clock.nowMs()
            WatchdogState.HOLD -> {
                lastActivityMs = clock.nowMs()
                state = WatchdogState.ARMED
                queued = WatchdogTransition(WatchdogState.HOLD, WatchdogState.ARMED, null, 0)
            }
            WatchdogState.FAILSAFE, WatchdogState.DISARMED -> Unit
        }
    }

    /** Returns the transition that just happened, if any; call it from the control tick. */
    fun poll(): WatchdogTransition? {
        queued?.let {
            queued = null
            return it
        }
        val since = lastActivityMs
        if (state == WatchdogState.DISARMED || state == WatchdogState.FAILSAFE || since == null) return null
        val elapsed = clock.nowMs() - since
        val target = when {
            elapsed >= config.failsafeMs -> WatchdogState.FAILSAFE
            elapsed >= config.holdMs -> WatchdogState.HOLD
            else -> WatchdogState.ARMED
        }
        if (target == state) return null
        val from = state
        state = target
        val reason = if (target == WatchdogState.FAILSAFE) WatchdogReason.WATCHDOG_FAILSAFE else WatchdogReason.WATCHDOG_HOLD
        return WatchdogTransition(from, target, reason, elapsed)
    }
}
