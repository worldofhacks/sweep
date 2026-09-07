package org.worldofhacks.sweep.bridge

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.AuthFrame
import org.worldofhacks.sweep.bridge.publish.WhipEndpoint
import org.worldofhacks.sweep.bridge.ui.aircraftDeviceIdFromSetup

class SetupDeviceIdTest {
    @Test
    fun `additional aircraft IDs retain their wire and publisher identity`() {
        for (deviceId in listOf(1, 5, Int.MAX_VALUE)) {
            val parsed = aircraftDeviceIdFromSetup(" $deviceId ")
            assertEquals(deviceId, parsed)
            val frame = AuthFrame(requireNotNull(parsed), "fixture-adapter-token")
            assertEquals(deviceId, AuthFrame.parse(frame.toJson()).droneId)
            assertEquals("drone$deviceId", WhipEndpoint.streamName(parsed))
            assertTrue(SetupSummary(
                relayUrl = "ws://127.0.0.1:18000", session = "fixture-session",
                droneId = parsed, tokenStored = true, loaded = true,
            ).complete)
        }
    }

    @Test
    fun `invalid or overflowing device IDs cannot select a default aircraft`() {
        for (raw in listOf("", " ", "0", "-1", "2147483648", "-2147483649", "999999999999999999999", "1.5")) {
            assertNull(aircraftDeviceIdFromSetup(raw), "must reject '$raw'")
        }
    }
}
