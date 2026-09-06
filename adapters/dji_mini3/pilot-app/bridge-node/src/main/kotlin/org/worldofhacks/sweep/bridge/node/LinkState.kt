package org.worldofhacks.sweep.bridge.node

import org.worldofhacks.sweep.bridge.core.frames.AuthRefused
import org.worldofhacks.sweep.bridge.core.frames.ControlPose
import org.worldofhacks.sweep.bridge.core.frames.NodeSettings
import org.worldofhacks.sweep.bridge.core.frames.NodeStatusBody
import org.worldofhacks.sweep.bridge.core.frames.RefusalEvent
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPins
import org.worldofhacks.sweep.bridge.core.watchdog.WatchdogState

/** What the pilot enters once on the Setup screen. The token is also the HMAC signing key. */
data class NodeConfig(
    val relayUrl: String,
    val session: String,
    val droneId: Int,
    val token: String,
    val adapterId: String,
    val capabilities: List<String>,
    val localizationPins: LocalizationPins? = null,
) {
    init {
        require(relayUrl.startsWith("ws://") || relayUrl.startsWith("wss://")) { "relay URL must start with ws:// or wss://" }
        require(session.isNotEmpty() && session.length <= 512 && session.none { it.code < 32 }) { "session id is invalid" }
        require(droneId > 0) { "drone id must be a positive integer" }
        require(token.isNotEmpty()) { "token must not be empty" }
        require(adapterId.isNotEmpty()) { "adapter id must not be empty" }
        require(capabilities.isNotEmpty() && capabilities.toSet().size == capabilities.size) {
            "capabilities must be a non-empty list without duplicates"
        }
        require("localized_navigation" !in capabilities) {
            "localized_navigation is not implemented; localization input is diagnostic-only"
        }
    }

    val key: ByteArray
        get() = token.toByteArray(Charsets.UTF_8)

    /** `<relay>/ws/{session}`; the token never appears in the URL. */
    val socketUrl: String
        get() = relayUrl.trimEnd('/') + "/ws/" + pathSegment(session)

    /** Never includes the token. */
    override fun toString(): String =
        "NodeConfig(relayUrl=$relayUrl, session=$session, droneId=$droneId, adapterId=$adapterId, token=<redacted>)"

    private companion object {
        fun pathSegment(value: String): String = buildString {
            for (byte in value.toByteArray(Charsets.UTF_8)) {
                val char = byte.toInt().toChar()
                val unreserved = char in 'A'..'Z' || char in 'a'..'z' || char in '0'..'9' || char in "-._~"
                if (unreserved) append(char) else append('%').append("%02X".format(byte.toInt() and 0xFF))
            }
        }
    }
}

/**
 * Node-side timing. The telemetry rate is 10 Hz to match the relay's 10 Hz state fan-out and
 * stay well inside its `SWEEP_TELEMETRY_FRESHNESS_MS` (1000 ms by default); the relay does not
 * distribute a telemetry rate in `auth.accepted`. Reconnect backoff doubles from
 * [initialBackoffMs] to [maxBackoffMs] with jitter.
 */
data class LinkTiming(
    val telemetryHz: Double = 10.0,
    val watchdogPollMs: Long = 100,
    val initialBackoffMs: Long = 500,
    val maxBackoffMs: Long = 5_000,
    val connectTimeoutMs: Long = 5_000,
    val authTimeoutMs: Long = 5_000,
    val joinFallbackMs: Long = 1_000,
    // 3 s so a dead Wi-Fi link is detected within a few seconds (issue #43 deployment note).
    val pingIntervalMs: Long = 3_000,
) {
    init {
        require(telemetryHz > 0.0 && telemetryHz <= 50.0) { "telemetry rate must be between 0 and 50 Hz" }
        require(watchdogPollMs > 0) { "watchdog poll period must be positive" }
        require(initialBackoffMs > 0 && maxBackoffMs >= initialBackoffMs) { "backoff bounds are invalid" }
    }
}

/** Relay connection words from the design brief's vocabulary. */
enum class RelayConnection(val wire: String) {
    CONNECTING("connecting"),
    CONNECTED("connected"),
    DEGRADED("degraded"),
    DISCONNECTED("disconnected"),
}

/** The three pilot toggles behind the signed `readiness` frame. */
data class ReadinessInput(
    val homePoseConfirmed: Boolean = false,
    val controlAuthority: Boolean = false,
    val rcSafetyOperatorPresent: Boolean = false,
)

/** One command this epoch and its latest outcome; the Commands surface lists these newest first. */
data class CommandRecord(
    val commandId: String,
    val intentId: String,
    val operation: String,
    val seq: Long,
    val rosterVersion: Int,
    val connectionEpoch: Int,
    val receivedAtMs: Long,
    val updatedAtMs: Long,
    val outcome: String,
    val reason: String?,
    val detail: String?,
)

/** Everything the UI and the foreground notification show about the relay link. */
data class LinkState(
    val connection: RelayConnection = RelayConnection.DISCONNECTED,
    val authenticated: Boolean = false,
    val joined: Boolean = false,
    val connectionEpoch: Int? = null,
    val rejoins: Int = 0,
    val rosterVersion: Int? = null,
    val nodeSettings: NodeSettings? = null,
    val relayOffsetMs: Long? = null,
    val authRoundTripMs: Long? = null,
    val relayNetwork: String? = null,
    val membership: String? = null,
    val membershipReason: String? = null,
    val readinessReasons: List<String> = emptyList(),
    val readiness: ReadinessInput = ReadinessInput(),
    val controlAuthority: Boolean = false,
    val authorityChangeReason: String? = null,
    val watchdog: WatchdogState = WatchdogState.DISARMED,
    val nodeStatus: NodeStatusBody? = null,
    /** When any frame last arrived on the socket, the relay's echo of this node's own frames included: link health, not liveness. */
    val lastRelayFrameAtMs: Long? = null,
    /** When an authorized control heartbeat last arrived: the deadman's clock for the flight loop. */
    val lastRelayActivityMs: Long? = null,
    /** Signed current-epoch diagnostic only; never copied into the physical flight loop. */
    val controlPose: ControlPose? = null,
    /** Local-clock deadline at which [controlPose] is cleared. */
    val controlPoseExpiresAtMs: Long? = null,
    val estop: Boolean = false,
    val lastRefusal: RefusalEvent? = null,
    val lastAuthRefusal: AuthRefused? = null,
    val halted: Boolean = false,
    val attempts: Int = 0,
    val backoffMs: Long? = null,
    val nextAttemptAtMs: Long? = null,
    val framesIn: Long = 0,
    val framesOut: Long = 0,
    val telemetrySent: Long = 0,
    val telemetryRateHz: Double = 0.0,
    val commands: List<CommandRecord> = emptyList(),
    val lastError: String? = null,
)

/** Plain-text log sink. The link never puts the token or a signature into a message. */
fun interface NodeLog {
    fun log(message: String)
}
