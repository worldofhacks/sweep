package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
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
    fun `navigation local height policy defaults to seven and eight foot ceilings`() {
        val policy = config().localHeightPolicy
        assertEquals(2.1336, policy.softCeilingM)
        assertEquals(2.4384, policy.hardCeilingM)
        assertThrows(IllegalArgumentException::class.java) {
            NavigationLocalHeightPolicy(hardCeilingM = 2.1336)
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
        navigationConfigSha256 = "a".repeat(64),
        mapVersion = "map-v1",
        mapSha256 = "a".repeat(64),
        geometrySha256 = "a".repeat(64),
        cameraCalibrationSha256 = "a".repeat(64),
        bodyExtrinsicsSha256 = "a".repeat(64),
        worldTransformSha256 = "a".repeat(64),
        controlSourceIds = listOf("tag-source"),
        clockLeaseId = "lease-1",
        clockLeaseExpiresAtMs = Long.MAX_VALUE,
        poseFreshnessMs = poseFreshnessMs,
        authorizationLifetimeMs = 5_000,
        lossLandAfterMs = 3_000,
        arrivalHorizontalToleranceM = 0.2,
        arrivalVerticalToleranceM = 0.2,
        maxPositionUncertaintyM = maxPositionUncertaintyM,
    )
}
