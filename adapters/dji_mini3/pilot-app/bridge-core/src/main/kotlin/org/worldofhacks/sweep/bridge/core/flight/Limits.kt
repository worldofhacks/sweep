package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs
import kotlin.math.hypot

/**
 * DJI's advanced-mode ranges (`VirtualStickRange` in MSDK 5.18.0): velocity ±23 m/s
 * horizontally, ±6 m/s vertically, yaw rate ±100 deg/s, yaw angle ±180 deg. The node never
 * approaches them; they bound the sanity checks in [FlightLimits].
 */
object VirtualStickRange {
    const val HORIZONTAL_MAX_MS = 23.0
    const val VERTICAL_MAX_MS = 6.0
    const val YAW_RATE_MAX_DEG_S = 100.0
    const val MIN_HZ = 5
    const val MAX_HZ = 25
}

/**
 * The node's own hard clamps, applied after the planner's and arbiter's: PRD 5.4 fixes the
 * indoor constrained mode at 0.5 m/s until measured evidence supports more. A command that
 * asks for more is slowed, not refused, and the slowdown is reported in the acknowledgement.
 */
data class FlightLimits(
    val maxHorizontalMS: Double = 0.5,
    val maxVerticalMS: Double = 0.3,
    val maxYawRateDegS: Double = 30.0,
) {
    init {
        require(maxHorizontalMS > 0.0 && maxHorizontalMS <= VirtualStickRange.HORIZONTAL_MAX_MS) { "horizontal limit out of range" }
        require(maxVerticalMS > 0.0 && maxVerticalMS <= VirtualStickRange.VERTICAL_MAX_MS) { "vertical limit out of range" }
        require(maxYawRateDegS > 0.0 && maxYawRateDegS <= VirtualStickRange.YAW_RATE_MAX_DEG_S) { "yaw rate limit out of range" }
    }

    /** Scales the horizontal vector down to the limit (direction preserved) and clamps the rest. */
    fun clamp(velocity: BodyVelocity): BodyVelocity {
        val horizontal = hypot(velocity.forwardMS, velocity.rightMS)
        val scale = if (horizontal > maxHorizontalMS) maxHorizontalMS / horizontal else 1.0
        return velocity.copy(
            forwardMS = velocity.forwardMS * scale,
            rightMS = velocity.rightMS * scale,
            upMS = velocity.upMS.coerceIn(-maxVerticalMS, maxVerticalMS),
            yawRateDegS = velocity.yawRateDegS.coerceIn(-maxYawRateDegS, maxYawRateDegS),
        )
    }

    fun within(velocity: BodyVelocity): Boolean =
        hypot(velocity.forwardMS, velocity.rightMS) <= maxHorizontalMS + EPSILON &&
            abs(velocity.upMS) <= maxVerticalMS + EPSILON &&
            abs(velocity.yawRateDegS) <= maxYawRateDegS + EPSILON

    private companion object {
        const val EPSILON = 1e-9
    }
}

/** The stick ticker's cadence: `virtual_stick_hz` from the relay, clamped to 5 to 25 Hz. */
data class StickCadence(val requestedHz: Int) {
    val hz: Int = clamp(requestedHz)

    val periodMs: Long = (1000.0 / hz).toLong()

    /** Drift-free deadline for the tick after [previousDeadlineMs]; resets when a tick ran late by a whole period. */
    fun nextDeadline(previousDeadlineMs: Long, nowMs: Long): Long {
        val next = previousDeadlineMs + periodMs
        return if (next < nowMs - periodMs) nowMs + periodMs else next
    }

    companion object {
        fun clamp(hz: Int): Int = hz.coerceIn(VirtualStickRange.MIN_HZ, VirtualStickRange.MAX_HZ)
    }
}

/** Measured send rate over a sliding window, the number the #85 resend drill records. */
class RateMeter(private val windowMs: Long = 2_000) {
    private val times = ArrayDeque<Long>()

    var count: Long = 0
        private set

    fun record(nowMs: Long) {
        count += 1
        times.addLast(nowMs)
        trim(nowMs)
    }

    fun rateHz(nowMs: Long): Double {
        trim(nowMs)
        if (times.size < 2) return 0.0
        val span = times.last() - times.first()
        return if (span > 0) (times.size - 1) * 1000.0 / span else 0.0
    }

    private fun trim(nowMs: Long) {
        while (times.isNotEmpty() && times.first() < nowMs - windowMs) times.removeFirst()
    }
}

/**
 * Loop tunables that are not relay-distributed. The settle, tolerance, and timeout values are
 * provisional until the guarded-hover run measures them; each is named where it is used.
 */
data class FlightConfig(
    val limits: FlightLimits = FlightLimits(),
    val mapping: AxisMapping = AxisMapping(),
    /** Zero-velocity time after a step before `completed`, and the hover settle window. */
    val settleMs: Long = 500,
    /** Below this measured speed the aircraft counts as hovering. */
    val hoverSpeedMS: Double = 0.15,
    /** `executing` progress acknowledgements for long steps, kept under the relay's command TTL. */
    val progressIntervalMs: Long = 1_000,
    val enableTimeoutMs: Long = 4_000,
    val takeoffMinMs: Long = 3_000,
    val takeoffTimeoutMs: Long = 25_000,
    val landingTimeoutMs: Long = 60_000,
    val landingRetryMs: Long = 2_000,
    val landingRetries: Int = 5,
    val altitudeToleranceM: Double = 0.2,
    val minDisplacementM: Double = 0.05,
    val yawToleranceDeg: Double = 5.0,
    val yawSettleMs: Long = 500,
    val yawMarginMs: Long = 2_000,
    /** PRD 5.5: network stop holds, then lands if the stop stays asserted this long. */
    val estopLandAfterMs: Long = 5_000,
    val defaultStickHz: Int = FlightSettings.DEFAULT_STICK_HZ,
    val navigation: NavigationConfig? = null,
)

data class NavigationConfig(
    val enabled: Boolean,
    val navigationConfigId: String,
    val mapId: String,
    val geometryId: String,
    val cameraCalibrationId: String,
    val bodyExtrinsicsId: String,
    val poseFreshnessMs: Long = 500,
    val lossLandAfterMs: Long = 3_000,
    val maxAuthorizationLifetimeMs: Long = 5_000,
) {
    init {
        require(listOf(navigationConfigId, mapId, geometryId, cameraCalibrationId, bodyExtrinsicsId).all { it.isNotBlank() }) { "navigation identities must be pinned" }
        require(poseFreshnessMs > 0 && lossLandAfterMs > 0 && maxAuthorizationLifetimeMs > 0) { "navigation timing bounds are invalid" }
    }
}
