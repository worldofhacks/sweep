package org.worldofhacks.sweep.bridge

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class IdentityQueryFenceTest {
    @Test
    fun `physical reconnect invalidates an earlier same session identity query`() {
        val fence = IdentityQueryFence()
        val beforeAircraft = fence.issue()
        fence.invalidate()
        val afterAircraft = fence.issue()

        assertFalse(fence.current(beforeAircraft))
        assertTrue(fence.current(afterAircraft))
    }

    @Test
    fun `stale identity query cannot apply a model update`() {
        val fence = IdentityQueryFence()
        val stale = fence.issue()
        fence.invalidate()
        var value = 0

        assertNull(fence.applyIfCurrent(stale) { value = 1 })
        assertEquals(0, value)
        assertEquals(2, fence.applyIfCurrent(fence.issue()) { value = 2; value })
        assertEquals(2, value)
    }
}
