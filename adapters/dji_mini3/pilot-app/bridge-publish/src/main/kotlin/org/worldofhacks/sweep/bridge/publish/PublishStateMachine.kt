package org.worldofhacks.sweep.bridge.publish

import kotlin.math.min
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState

/** Exponential backoff between publish attempts: [initialMs] doubling per failure, capped at [maxMs]. */
data class PublishBackoff(val initialMs: Long = 1_000, val maxMs: Long = 30_000) {
    init {
        require(initialMs > 0 && maxMs >= initialMs) { "backoff bounds are invalid" }
    }

    fun delayMs(consecutiveFailures: Int): Long {
        if (consecutiveFailures <= 1) return initialMs
        val exponent = min(consecutiveFailures - 1, MAX_EXPONENT)
        return min(initialMs shl exponent, maxMs)
    }

    private companion object {
        const val MAX_EXPONENT = 20
    }
}

/**
 * The `video_publish_state` machine: `stopped` → `connecting` → `publishing`, `failed` with a
 * reason on any loss, and a bounded backoff before the next `connecting`. Terminal reasons
 * ([PublishReasons.TERMINAL]) schedule no retry. Invalid transitions are ignored and return
 * the unchanged status, so a late callback from a torn-down session cannot revive it.
 */
class PublishStateMachine(
    private val backoff: PublishBackoff = PublishBackoff(),
    private val clock: Clock = SystemClock,
) {
    private val lock = Any()
    private val _status = MutableStateFlow(PublishStatus())
    val status: StateFlow<PublishStatus> = _status.asStateFlow()

    val current: PublishStatus
        get() = _status.value

    /** A fresh session: `stopped` or `failed` → `connecting` with the attempt counters reset. */
    fun start(source: PublishSource, whipUrl: String): PublishStatus = synchronized(lock) {
        val now = current
        if (now.state == VideoPublishState.CONNECTING || now.state == VideoPublishState.PUBLISHING) return now
        set(
            PublishStatus(
                state = VideoPublishState.CONNECTING,
                source = source,
                whipUrl = whipUrl,
                attempts = 1,
                lastChangeAtMs = clock.nowMs(),
            ),
        )
    }

    /** The scheduled retry fires: `failed` → `connecting`; failures are kept for the next delay. */
    fun attempting(): PublishStatus = synchronized(lock) {
        val now = current
        if (now.state != VideoPublishState.FAILED || now.nextAttemptAtMs == null) return now
        set(
            now.copy(
                state = VideoPublishState.CONNECTING,
                reason = null,
                detail = null,
                resourceUrl = null,
                attempts = now.attempts + 1,
                nextAttemptAtMs = null,
                lastChangeAtMs = clock.nowMs(),
            ),
        )
    }

    /** The WHIP POST answered 201: remember the resource so stop can DELETE it. */
    fun offerAccepted(resourceUrl: String?): PublishStatus = synchronized(lock) {
        val now = current
        if (now.state != VideoPublishState.CONNECTING) return now
        set(now.copy(resourceUrl = resourceUrl))
    }

    /** ICE connected: `connecting` → `publishing`; the failure streak ends. */
    fun publishing(codec: String? = null): PublishStatus = synchronized(lock) {
        val now = current
        if (now.state != VideoPublishState.CONNECTING) return now
        val at = clock.nowMs()
        set(
            now.copy(
                state = VideoPublishState.PUBLISHING,
                reason = null,
                detail = null,
                consecutiveFailures = 0,
                nextAttemptAtMs = null,
                publishingSinceMs = at,
                codec = codec ?: now.codec,
                lastChangeAtMs = at,
            ),
        )
    }

    /** Records codec evidence without changing the state. */
    fun codec(label: String?): PublishStatus = synchronized(lock) { set(current.copy(codec = label)) }

    /**
     * Any loss while `connecting` or `publishing` → `failed` with [reason]. Returns the delay
     * before the next attempt, or null when [reason] is terminal (no retry is scheduled).
     */
    fun failed(reason: String, detail: String? = null): Long? = synchronized(lock) {
        val now = current
        if (now.state == VideoPublishState.STOPPED) return null
        val failures = now.consecutiveFailures + 1
        val at = clock.nowMs()
        val delay = if (PublishReasons.isTerminal(reason)) null else backoff.delayMs(failures)
        set(
            now.copy(
                state = VideoPublishState.FAILED,
                reason = reason,
                detail = detail,
                resourceUrl = null,
                consecutiveFailures = failures,
                nextAttemptAtMs = delay?.let { at + it },
                publishingSinceMs = null,
                lastChangeAtMs = at,
            ),
        )
        delay
    }

    /** Pilot stop, aircraft disconnect, or service stop: any state → `stopped`. */
    fun stop(): PublishStatus = synchronized(lock) {
        val now = current
        if (now.state == VideoPublishState.STOPPED && now.reason == null) return now
        set(PublishStatus(codec = now.codec, lastChangeAtMs = clock.nowMs()))
    }

    private fun set(status: PublishStatus): PublishStatus {
        _status.value = status
        return status
    }
}
