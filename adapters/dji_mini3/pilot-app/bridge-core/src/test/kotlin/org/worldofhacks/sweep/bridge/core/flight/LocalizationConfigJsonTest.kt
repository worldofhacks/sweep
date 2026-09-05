package org.worldofhacks.sweep.bridge.core.flight

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class LocalizationConfigJsonTest {
    private val config = LocalizationConfig(
        mapId = "map-2026-09-05",
        geometryId = "geometry-a",
        cameraCalibrationId = "camera-b",
        bodyExtrinsicsId = "body-c",
        fixFreshnessMs = 350,
        poseFreshnessMs = 750,
        trackingTubeMm = 900,
        targetToleranceMm = 125,
        settledHoldMs = 700,
        tagLossLandAfterMs = 4_000,
    )

    @Test
    fun `versioned localization config preserves every pin and bound`() {
        assertEquals(config, LocalizationConfigJson.parse(LocalizationConfigJson.encode(config)))
    }

    @Test
    fun `localization import rejects incomplete unknown and unsafe fields`() {
        val encoded = LocalizationConfigJson.encode(config)
        for (invalid in listOf(
            encoded.replace("\"v\":1", "\"v\":2"),
            encoded.replace("\"map_id\"", "\"extra\""),
            encoded.replace("\"tracking_tube_mm\":900", "\"tracking_tube_mm\":10001"),
            encoded.replace("\"settled_hold_ms\":700", "\"settled_hold_ms\":-1"),
            encoded.replace("\"target_tolerance_mm\":125", "\"target_tolerance_mm\":125.0"),
        )) {
            assertThrows(IllegalArgumentException::class.java) { LocalizationConfigJson.parse(invalid) }
        }
    }
}
