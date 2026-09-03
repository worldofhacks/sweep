package org.worldofhacks.sweep.dji

enum class FeedCoverage {
    NONE,
    PRIMARY_CAMERA,
}

data class FeedQuality(val width: Int, val height: Int, val framesPerSecond: Int) {
    init {
        require((width == 0 && height == 0) || (width > 0 && height > 0))
        require(framesPerSecond >= 0)
    }

    companion object {
        val UNKNOWN = FeedQuality(0, 0, 0)
    }
}

enum class FeedReadiness {
    NO_SURFACE,
    NO_CAMERA,
    WAITING_FOR_FRAME,
    LIVE,
    STALE,
}

data class FeedAdvisory(
    val coverage: FeedCoverage,
    val quality: FeedQuality,
    val readiness: FeedReadiness,
)

class FeedMonitor(private val staleAfterMs: Long) {
    private var surfaceAvailable = false
    private var cameraAvailable = false
    private var lastFrameAtMs: Long? = null
    private var quality = FeedQuality.UNKNOWN

    init {
        require(staleAfterMs > 0)
    }

    @Synchronized
    fun onSurfaceChanged(available: Boolean) {
        surfaceAvailable = available
        if (!available) clearFrame()
    }

    @Synchronized
    fun onCameraChanged(available: Boolean) {
        cameraAvailable = available
        if (!available) clearFrame()
    }

    @Synchronized
    fun onFrame(quality: FeedQuality, observedAtMs: Long) {
        require(observedAtMs >= 0)
        if (!surfaceAvailable || !cameraAvailable) return
        this.quality = quality
        lastFrameAtMs = observedAtMs
    }

    @Synchronized
    fun advisory(nowMs: Long): FeedAdvisory {
        require(nowMs >= 0)
        val readiness = when {
            !surfaceAvailable -> FeedReadiness.NO_SURFACE
            !cameraAvailable -> FeedReadiness.NO_CAMERA
            lastFrameAtMs == null -> FeedReadiness.WAITING_FOR_FRAME
            nowMs - lastFrameAtMs!! > staleAfterMs -> FeedReadiness.STALE
            else -> FeedReadiness.LIVE
        }
        return FeedAdvisory(
            coverage = if (cameraAvailable) FeedCoverage.PRIMARY_CAMERA else FeedCoverage.NONE,
            quality = quality,
            readiness = readiness,
        )
    }

    private fun clearFrame() {
        lastFrameAtMs = null
        quality = FeedQuality.UNKNOWN
    }
}

enum class ConnectionSource {
    PRODUCT,
    RELAY,
    LAN,
}

sealed interface WatchdogAction {
    data object None : WatchdogAction
    data class HoldAndStop(val source: ConnectionSource) : WatchdogAction
}

class BridgeWatchdog {
    private val connected = ConnectionSource.entries.associateWith { false }.toMutableMap()
    private var active = false

    @Synchronized
    fun onConnectionChanged(source: ConnectionSource, isConnected: Boolean): WatchdogAction {
        connected[source] = isConnected
        if (isConnected || !active) return WatchdogAction.None
        active = false
        return WatchdogAction.HoldAndStop(source)
    }

    @Synchronized
    fun canDispatch(): Boolean = connected.values.all { it }

    @Synchronized
    fun startDispatch(): Boolean {
        active = canDispatch()
        return active
    }

    @Synchronized
    fun stopDispatch() {
        active = false
    }

    @Synchronized
    fun dispatchActive(): Boolean = active
}
