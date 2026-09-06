package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class NavigationConfigTest {
    @Test
    fun `route navigation is disabled until a measured configuration is supplied`() {
        assertNull(FlightConfig().navigation)
    }

    @Test
    fun `route navigation requires pinned identities and bounded timing`() {
        assertThrows(IllegalArgumentException::class.java) {
            config(navigationConfigId = "")
        }
        assertThrows(IllegalArgumentException::class.java) {
            config(poseFreshnessMs = 0)
        }
    }

    @Test
    fun `navigation limits must be finite and positive`() {
        assertThrows(IllegalArgumentException::class.java) {
            config(maxPositionUncertaintyM = Double.POSITIVE_INFINITY)
        }
    }

    @Test
    fun `deployment configuration uses an exact versioned millimetre codec`() {
        val config = config()
        val encoded = NavigationConfigJson.encode(config)
        assertEquals(config, NavigationConfigJson.parse(encoded))
        assertThrows(IllegalArgumentException::class.java) {
            NavigationConfigJson.parse(encoded.replace("\"v\":1", "\"v\":2"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            NavigationConfigJson.parse(encoded.dropLast(1) + ",\"extra\":1}")
        }
    }

    @Test
    fun `arrival requires both measured tolerance and uncertainty boundaries`() {
        val config = config()
        assertTrue(config.isWithinArrival(0.1, -0.1, 0.1))
        assertFalse(config.isWithinArrival(0.1, 0.0, 0.11))
        assertFalse(config.isWithinArrival(0.0, 0.1, 0.11))
        assertFalse(config.isWithinArrival(-0.01, 0.0, 0.0))
        assertFalse(config.isWithinArrival(0.0, 0.0, -0.01))
        assertFalse(config(maxPositionUncertaintyM = 0.05).isWithinArrival(0.0, 0.0, 0.1))
    }

    private fun config(
        navigationConfigId: String = "navigation-measured-v1",
        poseFreshnessMs: Long = 500,
        maxPositionUncertaintyM: Double = 0.2,
    ) = NavigationConfig(
        navigationConfigId = navigationConfigId,
        mapId = "map-v1",
        geometryId = "geometry-v1",
        cameraCalibrationId = "camera-v1",
        bodyExtrinsicsId = "body-v1",
        poseFreshnessMs = poseFreshnessMs,
        authorizationLifetimeMs = 5_000,
        lossLandAfterMs = 3_000,
        arrivalHorizontalToleranceM = 0.2,
        arrivalVerticalToleranceM = 0.2,
        maxPositionUncertaintyM = maxPositionUncertaintyM,
    )
}
