package org.worldofhacks.sweep.bridge.core.localization

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class LocalizationPinsTest {
    private val pins = LocalizationPins(
        mapId = "map-2026-09-05",
        geometryId = "geometry-a",
        cameraCalibrationId = "camera-b",
        bodyExtrinsicsId = "body-c",
    )

    @Test
    fun `versioned diagnostic config preserves every identity pin`() {
        assertEquals(pins, LocalizationPinsJson.parse(LocalizationPinsJson.encode(pins)))
    }

    @Test
    fun `localization import rejects incomplete unknown and noncanonical fields`() {
        val encoded = LocalizationPinsJson.encode(pins)
        for (invalid in listOf(
            encoded.replace("\"v\":1", "\"v\":2"),
            encoded.replace("\"map_id\"", "\"extra\""),
            encoded.replace("\"map-2026-09-05\"", "\" map-2026-09-05\""),
            encoded.dropLast(1) + ",\"navigation_enabled\":true}",
        )) {
            assertThrows(IllegalArgumentException::class.java) { LocalizationPinsJson.parse(invalid) }
        }
    }
}
