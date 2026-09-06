package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class AxisProbeTest {
    private fun samples(forward: Double, right: Double, count: Int = 20): List<AxisProbe.Sample> =
        List(count) { index ->
            // The first samples are still accelerating; the classifier ignores them.
            val ramp = if (index < 6) index / 6.0 else 1.0
            AxisProbe.Sample(index * 100L, forward * ramp, right * ramp)
        }

    @Test
    fun `under the documented mapping a pure roll command is expected to move forward and a pure pitch command right`() {
        assertEquals(AxisProbe.Axis.FORWARD, AxisProbe.expectedAxis(AxisProbe.Field.ROLL, AxisMapping()))
        assertEquals(AxisProbe.Axis.RIGHT, AxisProbe.expectedAxis(AxisProbe.Field.PITCH, AxisMapping()))
        assertEquals(AxisProbe.Axis.RIGHT, AxisProbe.expectedAxis(AxisProbe.Field.ROLL, AxisMapping(transposed = true)))
    }

    @Test
    fun `observed motion on the expected axis agrees`() {
        val result = AxisProbe.classify(AxisProbe.Field.ROLL, 0.3, AxisMapping(), samples(forward = 0.28, right = 0.02))
        assertTrue(result.agrees, result.summary())
        assertEquals(AxisProbe.Axis.FORWARD, result.observedAxis)
        assertEquals(1, result.observedSign)
        assertFalse(result.suggestsTranspose)
        assertTrue(result.summary().contains("agrees"))
    }

    @Test
    fun `motion on the other axis flags a transpose`() {
        val result = AxisProbe.classify(AxisProbe.Field.PITCH, 0.3, AxisMapping(), samples(forward = 0.3, right = 0.01))
        assertFalse(result.agrees)
        assertTrue(result.suggestsTranspose)
        assertTrue(result.summary().contains("TRANSPOSED"))
    }

    @Test
    fun `a reversed sign or no clear motion never agrees`() {
        val reversed = AxisProbe.classify(AxisProbe.Field.ROLL, 0.3, AxisMapping(), samples(forward = -0.3, right = 0.0))
        assertFalse(reversed.agrees)
        assertEquals(-1, reversed.observedSign)
        assertFalse(reversed.suggestsTranspose)
        val still = AxisProbe.classify(AxisProbe.Field.ROLL, 0.3, AxisMapping(), samples(forward = 0.03, right = 0.02))
        assertEquals(AxisProbe.Axis.NONE, still.observedAxis)
        assertFalse(still.agrees)
        assertTrue(still.summary().contains("no clear motion"))
    }
}
