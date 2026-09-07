package org.worldofhacks.sweep.bridge.node

import java.security.MessageDigest
import kotlin.math.sqrt
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.frames.ObservationSubmission

class CaptureAlignmentTest {
    private val identity = RigidTransform(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    private val digest = "a".repeat(64)
    private val alignment = CaptureAlignmentConfig(
        id = "ohmni-capture-alignment-1",
        sha256 = digest,
        scope = CaptureAlignmentScope("session", 1, 2, "map-1", "dji-body-camera"),
        clockMappingId = "dji-pts-to-phone-1",
        ptsToCapture = AffineClockMapping(1_000, 1, 1, 5),
        gimbalCallback = CallbackBound(25, 0.1, 10.0),
        bodyAttitudeCallback = CallbackBound(25, 0.1, 10.0),
        maxExtrinsicsAngleErrorDeg = 1.0,
        kinematicCalibration = KinematicCalibration("mount-1", digest, identity, identity),
    )
    private val config = NodeConfig(
        "ws://localhost", "session", 1, "key", "test", listOf("flight"),
        localizationPins = org.worldofhacks.sweep.bridge.core.localization.LocalizationPins("map-1", "geometry", "camera", "body"),
        captureAlignment = alignment,
    )

    @Test
    fun `body camera pose carries raw PTS real callback receipts and a derived gimbal transform`() {
        val event = canonicalBodyCameraPose(
            config, 2, "capture-1",
            CaptureAlignmentSample(100, 1_110, AttitudeSample(90.0, 0.0, 0.0, 1_110), AttitudeSample(1.0, 0.0, 0.0, 1_108)),
        )!!.toEvent()
        assertEquals(1_100L, ((event["t_capture"] as JsonObject)["value"] as JsonInt).value)
        val pose = ((event["payload"] as JsonObject)["pose"] as JsonObject)
        assertEquals(sqrt(0.5), (pose["qz"] as org.worldofhacks.sweep.bridge.core.json.JsonFloat).value, 1e-12)
        assertEquals(sqrt(0.5), (pose["qw"] as org.worldofhacks.sweep.bridge.core.json.JsonFloat).value, 1e-12)
        val proof = ((event["payload"] as JsonObject)["capture_alignment"] as JsonObject)
        assertEquals(100L, ((proof["frame_pts"] as JsonObject)["value"] as JsonInt).value)
        assertEquals(1_110L, ((proof["gimbal_receipt"] as JsonObject)["value"] as JsonInt).value)
    }

    @Test
    fun `canonical fixture pins raw config bytes and parses the bounded proof`() {
        val configBytes = requireNotNull(javaClass.classLoader.getResource("capture_alignment/capture-alignment.json")).readBytes()
        val digest = MessageDigest.getInstance("SHA-256").digest(configBytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }
        val decoded = CaptureAlignmentConfig.decode(configBytes.toString(Charsets.UTF_8), digest)
        val event = Json.parse(requireNotNull(javaClass.classLoader.getResource("capture_alignment/capture-alignment-observation.json")).readText()) as JsonObject
        val proof = ((event["payload"] as JsonObject)["capture_alignment"] as JsonObject)

        assertEquals(digest, decoded.sha256)
        assertEquals(digest, (proof["alignment_config_sha256"] as org.worldofhacks.sweep.bridge.core.json.JsonString).value)
        ObservationSubmission.parse(event)
    }

    @Test
    fun `producer refuses samples whose bounded callbacks cannot describe mapped frame capture`() {
        assertNull(canonicalBodyCameraPose(
            config, 2, "capture-2",
            CaptureAlignmentSample(100, 1_200, AttitudeSample(0.0, 0.0, 0.0, 900), AttitudeSample(0.0, 0.0, 0.0, 900)),
        ))
    }
}
