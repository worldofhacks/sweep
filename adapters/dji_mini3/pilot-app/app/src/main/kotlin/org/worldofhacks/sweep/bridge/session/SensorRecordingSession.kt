package org.worldofhacks.sweep.bridge.session

/** Authenticated relay identity required before the probe records DJI sensor callbacks. */
internal data class SensorRelayContext(
    val session: String,
    val droneId: Int,
    val connectionEpoch: Int,
) {
    init {
        require(session.isNotEmpty() && session == session.trim()) { "sensor relay session is invalid" }
        require(session.codePointCount(0, session.length) <= 512) { "sensor relay session is too long" }
        require(session.codePoints().allMatch { !Character.isISOControl(it) }) { "sensor relay session is invalid" }
        require(droneId > 0 && connectionEpoch > 0) { "sensor relay identity is invalid" }
    }
}

/** Optional probe-only hook; fake aircraft sessions intentionally do not record DJI samples. */
internal interface SensorRecordingSession {
    fun updateSensorRelayContext(context: SensorRelayContext?)
}
