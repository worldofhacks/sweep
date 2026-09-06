package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.hypot
import kotlin.math.sqrt
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs

/** One time-boxed piece of a motion command. */
sealed interface MotionStep {
    val durationMs: Long

    /** A constant body-frame velocity held for [durationMs]. */
    data class Velocity(
        val body: BodyVelocity,
        override val durationMs: Long,
        /** Planned ground displacement (east, north, up) in metres, for the acknowledgement detail. */
        val displacementM: Triple<Double, Double, Double>,
        /** True when the node slowed the command below the requested speed. */
        val slowed: Boolean,
    ) : MotionStep

    /** Yaw angle mode to an absolute compass heading; [durationMs] is the deadline, not the hold time. */
    data class Yaw(
        val targetDeg: Double,
        val speedDegS: Double,
        val deltaDeg: Double,
        override val durationMs: Long,
    ) : MotionStep
}

/**
 * Turns integer-millimetre command arguments into time-boxed body-frame velocity steps
 * (Phase E plan on issue #43: fused position is geographic and not trustworthy indoors, so
 * M1 flies open-loop steps and a position controller waits for the M3 localization gate).
 *
 * `goto` is the displacement from the position the node currently reports (which is 0,0
 * indoors until localization lands) to the target, flown at the requested speed clamped by
 * [FlightLimits], rotated into the body frame at the current heading, and held for exactly
 * the time the displacement needs at that speed. Sub-centimetre-per-tick rounding is the
 * only error term; the step never exceeds either limit.
 */
object MotionPlanner {
    fun goto(args: CommandArgs.Goto, facts: AircraftFacts, limits: FlightLimits, minDisplacementM: Double): MotionStep.Velocity? {
        val east = args.xMm / 1000.0 - facts.xEast
        val north = args.yMm / 1000.0 - facts.yNorth
        val up = args.zMm / 1000.0 - facts.zUp
        return step(east, north, up, args.speedMmS / 1000.0, facts.yawDeg, limits, minDisplacementM)
    }

    /** A pure vertical step, used after takeoff to reach the requested `z_mm`. */
    fun climb(targetZM: Double, facts: AircraftFacts, limits: FlightLimits, minDisplacementM: Double): MotionStep.Velocity? =
        step(0.0, 0.0, targetZM - facts.zUp, limits.maxVerticalMS, facts.yawDeg, limits, minDisplacementM)

    fun rotateTo(args: CommandArgs.RotateTo, facts: AircraftFacts, limits: FlightLimits, marginMs: Long): MotionStep.Yaw {
        val target = AxisMapping.normalizeYaw(args.yawMdeg / 1000.0)
        val speed = (args.speedMdegS / 1000.0).coerceAtMost(limits.maxYawRateDegS)
        val delta = AxisMapping.yawDelta(facts.yawDeg, target)
        val travelMs = ceil(abs(delta) / speed * 1000.0).toLong()
        return MotionStep.Yaw(targetDeg = target, speedDegS = speed, deltaDeg = delta, durationMs = travelMs + marginMs)
    }

    private fun step(
        east: Double,
        north: Double,
        up: Double,
        requestedSpeedMS: Double,
        yawDeg: Double,
        limits: FlightLimits,
        minDisplacementM: Double,
    ): MotionStep.Velocity? {
        val distance = sqrt(east * east + north * north + up * up)
        if (distance < minDisplacementM) return null
        val horizontal = hypot(east, north)
        val requested = requestedSpeedMS.coerceAtLeast(MIN_SPEED_MS)
        // Duration is the longest of: the path at the requested speed, the horizontal part at
        // the horizontal limit, the vertical part at the vertical limit. That keeps every
        // component inside its limit while preserving the straight-line direction.
        val byRequest = distance / requested
        val byHorizontal = horizontal / limits.maxHorizontalMS
        val byVertical = abs(up) / limits.maxVerticalMS
        val seconds = maxOf(byRequest, byHorizontal, byVertical)
        val durationMs = ceil(seconds * 1000.0).toLong().coerceAtLeast(1)
        val duration = durationMs / 1000.0
        val (forward, right) = GroundFrame.toBody(east / duration, north / duration, yawDeg)
        val body = BodyVelocity(forwardMS = forward, rightMS = right, upMS = up / duration)
        check(limits.within(body)) { "planned step exceeds the flight limits: $body" }
        return MotionStep.Velocity(
            body = body,
            durationMs = durationMs,
            displacementM = Triple(east, north, up),
            slowed = seconds > byRequest + 1e-9,
        )
    }

    private const val MIN_SPEED_MS = 0.01
}
