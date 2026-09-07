package org.worldofhacks.sweep.bridge.node

import kotlin.math.abs

/** Tracks one gimbal request until a post-acceptance attitude sample confirms it. */
class GimbalPitchCompletion(
    private val toleranceDegrees: Double = DEFAULT_TOLERANCE_DEGREES,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
) {
    init {
        require(toleranceDegrees > 0.0 && toleranceDegrees.isFinite())
        require(timeoutMs > 0)
    }

    data class Request(
        val id: Long,
        val targetDegrees: Double,
        val startedAtMonotonicMs: Long,
        val deadlineMonotonicMs: Long,
    )

    sealed interface Start {
        data class Begun(val request: Request) : Start
        data object Busy : Start
    }

    sealed interface Outcome {
        data object Ignored : Outcome
        data class Accepted(val request: Request) : Outcome
        data class Completed(val request: Request, val observedDegrees: Double) : Outcome
        data class Failed(val request: Request, val reason: String) : Outcome
    }

    private var nextId = 0L
    private var active: Request? = null
    private var acceptedAtMonotonicMs: Long? = null

    fun begin(targetDegrees: Double, nowMonotonicMs: Long): Start {
        require(targetDegrees.isFinite())
        require(nowMonotonicMs >= 0)
        if (active != null) return Start.Busy
        val request = Request(++nextId, targetDegrees, nowMonotonicMs, nowMonotonicMs + timeoutMs)
        active = request
        return Start.Begun(request)
    }

    fun accepted(id: Long, nowMonotonicMs: Long): Outcome {
        val request = active?.takeIf { it.id == id } ?: return Outcome.Ignored
        if (acceptedAtMonotonicMs != null) return Outcome.Ignored
        if (nowMonotonicMs >= request.deadlineMonotonicMs) return fail(request, "gimbal action accepted after deadline")
        acceptedAtMonotonicMs = nowMonotonicMs
        return Outcome.Accepted(request)
    }

    fun rejected(id: Long, reason: String): Outcome {
        val request = active?.takeIf { it.id == id } ?: return Outcome.Ignored
        return fail(request, reason)
    }

    fun observe(pitchDegrees: Double, receivedAtMonotonicMs: Long): Outcome {
        val request = active ?: return Outcome.Ignored
        val acceptedAt = acceptedAtMonotonicMs ?: return Outcome.Ignored
        if (!pitchDegrees.isFinite() || receivedAtMonotonicMs < acceptedAt) return Outcome.Ignored
        if (receivedAtMonotonicMs >= request.deadlineMonotonicMs) return fail(request, "gimbal attitude confirmation timed out")
        if (abs(pitchDegrees - request.targetDegrees) > toleranceDegrees) return Outcome.Ignored
        active = null
        acceptedAtMonotonicMs = null
        return Outcome.Completed(request, pitchDegrees)
    }

    fun expire(id: Long, nowMonotonicMs: Long): Outcome {
        val request = active?.takeIf { it.id == id } ?: return Outcome.Ignored
        if (nowMonotonicMs < request.deadlineMonotonicMs) return Outcome.Ignored
        return fail(request, "gimbal attitude confirmation timed out")
    }

    private fun fail(request: Request, reason: String): Outcome.Failed {
        active = null
        acceptedAtMonotonicMs = null
        return Outcome.Failed(request, reason)
    }

    companion object {
        const val DEFAULT_TOLERANCE_DEGREES = 2.0
        const val DEFAULT_TIMEOUT_MS = 4_000L
    }
}

/** Couples an operation token to its confirmation state so terminal reports cannot be replaced. */
class GimbalPitchOperationCoordinator<T>(
    private val completion: GimbalPitchCompletion = GimbalPitchCompletion(),
) {
    data class Active<T>(
        val request: GimbalPitchCompletion.Request,
        val operation: T,
    )

    sealed interface Start<out T> {
        data class Begun<T>(val active: Active<T>) : Start<T>
        data object Busy : Start<Nothing>
    }

    sealed interface Update<out T> {
        data object Ignored : Update<Nothing>
        data class Accepted<T>(val active: Active<T>) : Update<T>
        data class Completed<T>(val active: Active<T>, val observedDegrees: Double) : Update<T>
        data class Failed<T>(val active: Active<T>, val reason: String) : Update<T>
    }

    private var active: Active<T>? = null

    fun begin(targetDegrees: Double, nowMonotonicMs: Long, operation: T): Start<T> {
        if (active != null) return Start.Busy
        return when (val started = completion.begin(targetDegrees, nowMonotonicMs)) {
            GimbalPitchCompletion.Start.Busy -> Start.Busy
            is GimbalPitchCompletion.Start.Begun -> {
                val next = Active(started.request, operation)
                active = next
                Start.Begun(next)
            }
        }
    }

    fun accepted(id: Long, nowMonotonicMs: Long): Update<T> = update(completion.accepted(id, nowMonotonicMs))

    fun rejected(id: Long, reason: String): Update<T> = update(completion.rejected(id, reason))

    fun observed(pitchDegrees: Double, receivedAtMonotonicMs: Long): Update<T> =
        update(completion.observe(pitchDegrees, receivedAtMonotonicMs))

    fun expired(id: Long, nowMonotonicMs: Long): Update<T> = update(completion.expire(id, nowMonotonicMs))

    fun isActive(id: Long): Boolean = active?.request?.id == id

    fun activeId(): Long? = active?.request?.id

    private fun update(outcome: GimbalPitchCompletion.Outcome): Update<T> = when (outcome) {
        GimbalPitchCompletion.Outcome.Ignored -> Update.Ignored
        is GimbalPitchCompletion.Outcome.Accepted -> active
            ?.takeIf { it.request.id == outcome.request.id }
            ?.let { Update.Accepted(it) }
            ?: Update.Ignored
        is GimbalPitchCompletion.Outcome.Completed -> terminal(outcome.request.id) { Update.Completed(it, outcome.observedDegrees) }
        is GimbalPitchCompletion.Outcome.Failed -> terminal(outcome.request.id) { Update.Failed(it, outcome.reason) }
    }

    private fun terminal(id: Long, result: (Active<T>) -> Update<T>): Update<T> {
        val operation = active?.takeIf { it.request.id == id } ?: return Update.Ignored
        active = null
        return result(operation)
    }
}
