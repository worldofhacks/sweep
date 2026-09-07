package org.worldofhacks.sweep.bridge.node

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.ContractError
import org.worldofhacks.sweep.bridge.core.frames.ObservationSubmission
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject

class CanonicalObservationsTest {
    private val source = ObservationSourceConfig("dji-telemetry", "dji_enu", "phone_snapshot_wall_ms")
    private val config = NodeConfig("ws://localhost", "session", 1, "key", "test", listOf("flight"), observationSource = source)
    private val snapshot = FakeAircraft(connected = true).snapshot.value

    @Test
    fun `producer preserves ENU measurements and refuses invalid numbers instead of replacing them`() {
        val measured = snapshot.copy(x = 1.5, y = -2.0, z = 0.7, vx = 0.3, vy = -0.4, vz = 0.1, posQuality = 0.4)
        val event = canonicalTelemetry(config, measured, 2, "sample", 123)!!.toEvent()
        assertEquals(JsonFloat(0.4), event["confidence"])
        val payload = event["payload"] as JsonObject
        assertEquals(JsonFloat(-2.0), (payload["position"] as JsonObject)["y_m"])
        assertEquals(JsonFloat(0.1), (payload["velocity"] as JsonObject)["z_m_s"])
        assertEquals(JsonInt(123), (event["t_source_receipt"] as JsonObject)["value"])
        assertNull(canonicalTelemetry(config, measured.copy(x = Double.NaN), 2, "sample", 123))
        assertNull(canonicalTelemetry(config, measured.copy(posQuality = -1.0), 2, "sample", 123))
        assertNull(canonicalTelemetry(config.copy(observationSource = null), measured, 2, "sample", 123))
        assertThrows(ContractError::class.java) { ObservationSubmission.parse(event.with("t_ingest", JsonInt(123))) }
    }

    @Test
    fun `configuration rejects world aliases duplicate keys and oversized data`() {
        assertThrows(IllegalArgumentException::class.java) { source.copy(frameId = "world") }
        assertThrows(IllegalArgumentException::class.java) { source.copy(frameId = "another_enu") }
        assertThrows(IllegalArgumentException::class.java) { source.copy(telemetrySourceId = "dji-camera") }
        assertThrows(IllegalArgumentException::class.java) { source.copy(clockId = "sdk_capture_ms") }
        assertThrows(IllegalArgumentException::class.java) { ObservationSourceConfig.decode(" ".repeat(4097)) }
        assertThrows(RuntimeException::class.java) {
            ObservationSourceConfig.decode("""{"v":1,"v":1,"telemetry_source_id":"dji-telemetry","frame_id":"dji_enu","clock_id":"phone_snapshot_wall_ms"}""")
        }
        assertEquals(source, ObservationSourceConfig.decode("""{"v":1,"telemetry_source_id":"dji-telemetry","frame_id":"dji_enu","clock_id":"phone_snapshot_wall_ms"}"""))
    }
}
