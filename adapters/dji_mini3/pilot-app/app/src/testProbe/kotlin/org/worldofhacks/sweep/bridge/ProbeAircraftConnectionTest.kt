package org.worldofhacks.sweep.bridge

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class ProbeAircraftConnectionTest {
    @Test
    fun `KeyConnection lifecycle emits only physical transitions`() {
        val transitions = AircraftConnectionTransitions()

        assertEquals(true, transitions.changed(true))
        assertEquals(null, transitions.changed(true))
        assertEquals(false, transitions.changed(false))
        assertEquals(null, transitions.changed(false))
    }

    @Test
    fun `product disconnect resets KeyConnection transition state`() {
        val transitions = AircraftConnectionTransitions()

        assertEquals(true, transitions.changed(true))
        transitions.reset()
        assertEquals(true, transitions.changed(true))
    }
}
