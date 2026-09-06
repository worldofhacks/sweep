package org.worldofhacks.sweep.bridge

import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.localization.LocalizationPins

class BridgeSetupTest {
    private val setup = BridgeSetup("ws://127.0.0.1:8000", "session-a", 2, "dummy-test-token")

    @Test
    fun `readiness cannot transfer across a changed relay identity or credential`() {
        val replacements = listOf(
            setup.copy(relayUrl = "ws://127.0.0.1:8010"),
            setup.copy(session = "session-b"),
            setup.copy(droneId = 1),
            setup.copy(token = "dummy-replacement-token"),
        )
        for (replacement in replacements) assertFalse(setup.hasSameRelayIdentity(replacement))
        assertFalse(setup.hasSameRelayIdentity(null))
    }

    @Test
    fun `saving unchanged relay identity retains declarations and diagnostic pins do not change it`() {
        assertTrue(setup.hasSameRelayIdentity(setup.copy()))
        assertTrue(setup.hasSameRelayIdentity(setup.copy(
            localizationPins = LocalizationPins("map-a", "geometry-a", "camera-a", "body-a"),
        )))
    }
}
