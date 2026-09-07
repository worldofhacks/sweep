package org.worldofhacks.sweep.bridge.core.frames

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString

class ObservationTest {
    @Test
    fun `parses the aircraft telemetry vector and preserves canonical event bytes`() {
        val wire = observation()
        val parsed = ObservationFrame.parse(wire)

        assertEquals("aircraft", parsed.nodeType)
        assertEquals("world", parsed.frame)
        assertEquals("telemetry", (parsed.payload["kind"] as JsonString).value)
        assertEquals(Json.canonical(wire), Json.canonical(parsed.toEvent()))
    }

    @Test
    fun `parses local range scan and accepted tag evidence`() {
        val scan = observation(
            frame = "lidar",
            nodeType = "ground",
            payload = Json.json(
                "kind" to "range_scan",
                "sensor_pose" to pose("odom", "lidar", 0.0, 0.0, 0.25),
                "angle_min_rad" to -0.1,
                "angle_increment_rad" to 0.1,
                "range_min_m" to 0.1,
                "range_max_m" to 8.0,
                "ranges_m" to listOf(1.0, null, 2.0),
                "mount_id" to "ohmni-lidar-v1",
            ),
        )
        assertEquals("range_scan", (ObservationFrame.parse(scan).payload["kind"] as JsonString).value)

        val tag = observation(
            frame = "camera",
            nodeType = "ground",
            payload = Json.json(
                "kind" to "tag_observation",
                "family" to "tag36h11",
                "tag_id" to 42,
                "image_id" to "frame-001",
                "pose_accepted" to true,
                "tag_pose" to pose("camera", "tag:42", 0.2, 0.0, 1.0),
                "covariance_m2" to listOf(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
                "reason" to "pose",
                "size_m" to 0.16,
                "corners_px" to listOf(listOf(100.0, 200.0), listOf(120.0, 200.0), listOf(120.0, 220.0), listOf(100.0, 220.0)),
                "pixel_frame" to "camera",
                "reprojection_rms_px" to 0.3,
            ),
        )
        assertEquals("tag_observation", (ObservationFrame.parse(tag).payload["kind"] as JsonString).value)
    }

    @Test
    fun `rejects malformed observation fields and tag covariance`() {
        assertThrows(ContractError::class.java) {
            ObservationFrame.parse(observation().with("node_type", Json.json("invalid" to true)))
        }
        assertThrows(ContractError::class.java) {
            ObservationFrame.parse(observation().with("payload", Json.json("kind" to "telemetry")))
        }
        assertThrows(ContractError::class.java) {
            ObservationFrame.parse(
                observation(
                    frame = "camera",
                    payload = Json.json(
                        "kind" to "tag_observation", "family" to "tag36h11", "tag_id" to 42,
                        "image_id" to "frame-001", "pose_accepted" to true,
                        "tag_pose" to pose("camera", "tag:42", 0.2, 0.0, 1.0),
                        "covariance_m2" to listOf(1.0, 2.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                        "reason" to "pose", "size_m" to 0.16,
                        "corners_px" to listOf(listOf(100.0, 200.0), listOf(120.0, 200.0), listOf(120.0, 220.0), listOf(100.0, 220.0)),
                        "pixel_frame" to "camera", "reprojection_rms_px" to 0.3,
                    ),
                ),
            )
        }
    }

    private fun observation(
        frame: String = "world",
        nodeType: String = "aircraft",
        payload: JsonObject = Json.json(
            "kind" to "telemetry",
            "position" to Json.json("frame" to "world", "x_m" to 1.0, "y_m" to 2.0, "z_m" to 1.5),
            "velocity" to Json.json("frame" to "world", "x_m_s" to 0.0, "y_m_s" to 0.0, "z_m_s" to 0.0),
            "battery" to 0.8,
            "link" to 0.9,
            "pos_quality" to 0.7,
            "state" to "hovering",
        ),
    ): JsonObject = Json.json(
        "v" to 1,
        "type" to "observation",
        "event_id" to "aircraft-001",
        "session" to "demo-1",
        "device_id" to 7,
        "connection_epoch" to 3,
        "source_id" to "bridge",
        "node_type" to nodeType,
        "frame" to frame,
        "confidence" to 0.9,
        "t_capture" to Json.json("clock_id" to "bridge-ms", "unit" to "ms", "value" to 100),
        "t_source_receipt" to Json.json("clock_id" to "bridge-ms", "unit" to "ms", "value" to 101),
        "clock_mapping_id" to "bridge-clock",
        "payload" to payload,
        "t_ingest" to 1005,
    )

    private fun pose(parent: String, child: String, x: Double, y: Double, z: Double): JsonObject =
        Json.json("parent_frame" to parent, "child_frame" to child, "x_m" to x, "y_m" to y, "z_m" to z, "qx" to 0.0, "qy" to 0.0, "qz" to 0.0, "qw" to 1.0)
}
