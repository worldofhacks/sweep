package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.cos
import kotlin.math.sin

/**
 * A velocity command in the aircraft body frame: `forward` along the nose, `right` along the
 * starboard side, `up` against gravity, all in metres per second, plus yaw. Yaw is either a
 * rate (degrees per second, clockwise positive) or an absolute compass target (degrees,
 * 0 = north, clockwise positive, the convention of DJI `KeyAircraftAttitude.yaw` and of the
 * planner's `rotate_to` headings).
 */
data class BodyVelocity(
    val forwardMS: Double = 0.0,
    val rightMS: Double = 0.0,
    val upMS: Double = 0.0,
    val yawRateDegS: Double = 0.0,
    val yawTargetDeg: Double? = null,
) {
    companion object {
        val ZERO = BodyVelocity()
    }
}

enum class YawMode { ANGLE, ANGULAR_VELOCITY }

/**
 * What goes into `VirtualStickFlightControlParam` each tick. The horizontal mode is always
 * velocity in the BODY coordinate system and the vertical mode is always velocity (metres per
 * second, positive up), as the Phase E plan on issue #43 fixes them; only the yaw mode varies.
 */
data class StickFrame(
    val pitch: Double,
    val roll: Double,
    val yaw: Double,
    val verticalThrottle: Double,
    val yawMode: YawMode,
) {
    val isNeutral: Boolean
        get() = pitch == 0.0 && roll == 0.0 && verticalThrottle == 0.0 && yawMode == YawMode.ANGULAR_VELOCITY && yaw == 0.0

    companion object {
        val NEUTRAL = StickFrame(0.0, 0.0, 0.0, 0.0, YawMode.ANGULAR_VELOCITY)
    }
}

/**
 * The one place the body-frame intent is mapped onto DJI's `pitch` and `roll` fields.
 *
 * DJI documents the velocity-mode fields as: "the roll property represents the X direction
 * velocity; the pitch property represents the Y direction velocity" (MSDK v5.18.0 docs,
 * SimulatorDemo; v4 API: `setRoll` is velocity along the x-axis, `setPitch` along the
 * y-axis). In the BODY coordinate system X is the nose axis and Y the starboard axis, so the
 * documented default is `roll = forward`, `pitch = right`. That is exactly the "transpose"
 * lis-epfl measured in GROUND frame (`pitch` drove east, `roll` drove north), recorded in
 * docs/prior-art.md and issue #85. The #85 axis probe must confirm it on the exact Mini 3 and
 * MSDK 5.18.0 pair; if the aircraft moves the other way, [transposed] flips the mapping here,
 * in the bridge, never downstream.
 *
 * Signs: forward positive along the nose, right positive to starboard, up positive, yaw rate
 * positive clockwise seen from above. `verticalThrottle` in velocity mode is positive up.
 */
data class AxisMapping(val transposed: Boolean = false) {
    fun toFrame(velocity: BodyVelocity): StickFrame {
        val target = velocity.yawTargetDeg
        val (pitch, roll) = if (transposed) velocity.forwardMS to velocity.rightMS else velocity.rightMS to velocity.forwardMS
        return if (target != null) {
            StickFrame(pitch, roll, normalizeYaw(target), velocity.upMS, YawMode.ANGLE)
        } else {
            StickFrame(pitch, roll, velocity.yawRateDegS, velocity.upMS, YawMode.ANGULAR_VELOCITY)
        }
    }

    /** The inverse for a frame built from a pure SDK-field command, as the #85 probe sends. */
    fun toBody(frame: StickFrame): BodyVelocity {
        val forward = if (transposed) frame.pitch else frame.roll
        val right = if (transposed) frame.roll else frame.pitch
        return if (frame.yawMode == YawMode.ANGLE) {
            BodyVelocity(forward, right, frame.verticalThrottle, 0.0, frame.yaw)
        } else {
            BodyVelocity(forward, right, frame.verticalThrottle, frame.yaw, null)
        }
    }

    companion object {
        /** Wraps a compass heading into DJI's `[-180, 180]` yaw range. */
        fun normalizeYaw(degrees: Double): Double {
            var value = degrees % 360.0
            if (value > 180.0) value -= 360.0
            if (value <= -180.0) value += 360.0
            return value
        }

        /** Shortest signed rotation from [fromDeg] to [toDeg], clockwise positive, in `(-180, 180]`. */
        fun yawDelta(fromDeg: Double, toDeg: Double): Double {
            val delta = normalizeYaw(toDeg - fromDeg)
            return if (delta == -180.0) 180.0 else delta
        }
    }
}

/** Ground-frame quantities in the planner's telemetry frame: `x` east, `y` north, `z` up. */
object GroundFrame {
    /**
     * Rotates a ground displacement or velocity (east, north) into the body frame of an
     * aircraft heading [yawDeg] (compass, clockwise from north). At yaw 0 north is forward and
     * east is right; at yaw 90 east is forward and south is right.
     */
    fun toBody(east: Double, north: Double, yawDeg: Double): Pair<Double, Double> {
        val yaw = Math.toRadians(yawDeg)
        val forward = north * cos(yaw) + east * sin(yaw)
        val right = -north * sin(yaw) + east * cos(yaw)
        return forward to right
    }

    /** The inverse of [toBody]: body (forward, right) at heading [yawDeg] back to (east, north). */
    fun toGround(forward: Double, right: Double, yawDeg: Double): Pair<Double, Double> {
        val yaw = Math.toRadians(yawDeg)
        val east = forward * sin(yaw) + right * cos(yaw)
        val north = forward * cos(yaw) - right * sin(yaw)
        return east to north
    }
}
