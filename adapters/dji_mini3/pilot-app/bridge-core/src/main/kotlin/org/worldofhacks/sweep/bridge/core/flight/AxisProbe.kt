package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs

/**
 * Issue #85 axis-transpose probe: the node holds a pure single-field stick command (only
 * `pitch`, then only `roll`) at guarded hover and records the body-frame velocity the
 * telemetry reports. This classifier says which body axis actually moved and whether that
 * agrees with the [AxisMapping] the loop is using. A disagreement is fixed in the bridge by
 * flipping the mapping, never downstream.
 */
object AxisProbe {
    enum class Field { PITCH, ROLL }

    enum class Axis { FORWARD, RIGHT, NONE }

    data class Sample(val tMs: Long, val forwardMS: Double, val rightMS: Double)

    data class Result(
        val field: Field,
        val commandedMS: Double,
        val observedAxis: Axis,
        val observedSign: Int,
        val meanForwardMS: Double,
        val meanRightMS: Double,
        val expectedAxis: Axis,
        val samples: Int,
    ) {
        /** True when the aircraft moved the axis the current mapping predicts, in the commanded direction. */
        val agrees: Boolean
            get() = observedAxis == expectedAxis && observedAxis != Axis.NONE && observedSign == commandedSign

        val commandedSign: Int
            get() = if (commandedMS < 0) -1 else 1

        val suggestsTranspose: Boolean
            get() = observedAxis != Axis.NONE && observedAxis != expectedAxis

        fun summary(): String = buildString {
            append(field.name.lowercase()).append(' ').append(format(commandedMS)).append(" m/s -> observed ")
            append(observedAxis.name.lowercase())
            if (observedAxis != Axis.NONE) append(if (observedSign < 0) " negative" else " positive")
            append(" (mean forward ").append(format(meanForwardMS)).append(", right ").append(format(meanRightMS)).append(" over ")
            append(samples).append(" samples); expected ").append(expectedAxis.name.lowercase())
            append(if (agrees) "; agrees with the mapping" else if (suggestsTranspose) "; TRANSPOSED relative to the mapping" else "; no clear motion")
        }

        private fun format(value: Double): String = String.format(java.util.Locale.ROOT, "%.2f", value)
    }

    /** The body axis a pure [field] command should move under [mapping]. */
    fun expectedAxis(field: Field, mapping: AxisMapping): Axis {
        val frame = if (field == Field.PITCH) StickFrame.NEUTRAL.copy(pitch = 1.0) else StickFrame.NEUTRAL.copy(roll = 1.0)
        val body = mapping.toBody(frame)
        return if (abs(body.forwardMS) > abs(body.rightMS)) Axis.FORWARD else Axis.RIGHT
    }

    /**
     * Classifies the samples taken while the command was held, ignoring the first
     * [ignoreFraction] of them (acceleration) and calling the motion clear only when the
     * dominant mean exceeds [minSpeedMS] and the other axis by [dominance].
     */
    fun classify(
        field: Field,
        commandedMS: Double,
        mapping: AxisMapping,
        samples: List<Sample>,
        minSpeedMS: Double = 0.1,
        dominance: Double = 2.0,
        ignoreFraction: Double = 0.3,
    ): Result {
        val settled = samples.drop((samples.size * ignoreFraction).toInt())
        val forward = if (settled.isEmpty()) 0.0 else settled.sumOf { it.forwardMS } / settled.size
        val right = if (settled.isEmpty()) 0.0 else settled.sumOf { it.rightMS } / settled.size
        val axis = when {
            abs(forward) >= minSpeedMS && abs(forward) >= dominance * abs(right) -> Axis.FORWARD
            abs(right) >= minSpeedMS && abs(right) >= dominance * abs(forward) -> Axis.RIGHT
            else -> Axis.NONE
        }
        val sign = when (axis) {
            Axis.FORWARD -> if (forward < 0) -1 else 1
            Axis.RIGHT -> if (right < 0) -1 else 1
            Axis.NONE -> 0
        }
        return Result(field, commandedMS, axis, sign, forward, right, expectedAxis(field, mapping), settled.size)
    }
}
