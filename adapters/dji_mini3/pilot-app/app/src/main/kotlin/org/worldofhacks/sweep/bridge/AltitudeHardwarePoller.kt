package org.worldofhacks.sweep.bridge

internal fun interface AltitudePollScheduler {
    fun after(delayMs: Long, action: () -> Unit)
}

internal fun interface AltitudeHardwareQuery {
    fun request(callback: AltitudeHardwareQueryCallback)
}

internal interface AltitudeHardwareQueryCallback {
    fun onSuccess(value: Double?)

    fun onFailure()
}

/** Polls a hardware-backed altitude key without treating the SDK cache as a fresh receipt. */
internal class AltitudeHardwarePoller(
    private val scheduler: AltitudePollScheduler,
    private val query: AltitudeHardwareQuery,
    private val monotonicNowMs: () -> Long,
    private val onSample: (value: Double, receivedAtMonotonicMs: Long) -> Unit,
    private val periodMs: Long = DEFAULT_PERIOD_MS,
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
    private val guard: Any = Any(),
) {
    init {
        require(periodMs > 0)
        require(timeoutMs > 0)
    }

    private data class Request(
        val id: Long,
        val lifecycleGeneration: Long,
        val listenerGeneration: Long,
        val deadlineMonotonicMs: Long,
        var timedOut: Boolean = false,
    )

    private var attached = false
    private var connected = false
    private var lifecycleGeneration = 0L
    private var listenerGeneration = 0L
    private var nextScheduleId = 0L
    private var nextRequestId = 0L
    private var scheduledId: Long? = null
    private var inFlight: Request? = null

    fun attach() {
        synchronized(guard) {
            if (attached) return
            attached = true
            lifecycleGeneration += 1
        }
        scheduleIfReady(0)
    }

    fun detach() {
        synchronized(guard) {
            if (!attached) return
            attached = false
            lifecycleGeneration += 1
            scheduledId = null
        }
    }

    /** Binds every query response to the current product connection, including a product change. */
    fun connectionChanged(isConnected: Boolean) {
        synchronized(guard) {
            connected = isConnected
            lifecycleGeneration += 1
            scheduledId = null
        }
        scheduleIfReady(0)
    }

    /** A listener receipt after a query starts makes that query's value older than the listener value. */
    fun listenerSampleReceived() {
        synchronized(guard) { listenerGeneration += 1 }
    }

    private fun scheduleIfReady(delayMs: Long) {
        val scheduleId = synchronized(guard) {
            if (!ready() || inFlight != null || scheduledId != null) null else (++nextScheduleId).also { scheduledId = it }
        } ?: return
        scheduler.after(delayMs) { begin(scheduleId) }
    }

    private fun begin(scheduleId: Long) {
        val request = synchronized(guard) {
            if (scheduledId != scheduleId) return
            scheduledId = null
            if (!ready() || inFlight != null) return
            Request(
                id = ++nextRequestId,
                lifecycleGeneration = lifecycleGeneration,
                listenerGeneration = listenerGeneration,
                deadlineMonotonicMs = monotonicNowMs() + timeoutMs,
            ).also { inFlight = it }
        }
        scheduler.after(timeoutMs) { timeout(request.id) }
        try {
            query.request(object : AltitudeHardwareQueryCallback {
                override fun onSuccess(value: Double?) = complete(request.id, value)

                override fun onFailure() = complete(request.id, null)
            })
        } catch (_: RuntimeException) {
            complete(request.id, null)
        }
    }

    private fun timeout(requestId: Long) {
        synchronized(guard) {
            if (inFlight?.id == requestId) inFlight?.timedOut = true
        }
    }

    private fun complete(requestId: Long, value: Double?) {
        synchronized(guard) {
            val request = inFlight?.takeIf { it.id == requestId } ?: return
            inFlight = null
            value?.takeIf { it.isFinite() }?.takeIf {
                !request.timedOut &&
                    monotonicNowMs() < request.deadlineMonotonicMs &&
                    request.lifecycleGeneration == lifecycleGeneration &&
                    request.listenerGeneration == listenerGeneration &&
                    ready()
            }?.let { onSample(it, monotonicNowMs()) }
        }
        scheduleIfReady(periodMs)
    }

    private fun ready(): Boolean = attached && connected

    private companion object {
        const val DEFAULT_PERIOD_MS = 100L
        const val DEFAULT_TIMEOUT_MS = 500L
    }
}
