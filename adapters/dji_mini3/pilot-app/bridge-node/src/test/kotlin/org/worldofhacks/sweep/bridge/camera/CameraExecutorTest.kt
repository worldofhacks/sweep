package org.worldofhacks.sweep.bridge.camera

import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CopyOnWriteArrayList
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.MediaFileRecord
import org.worldofhacks.sweep.bridge.core.frames.RetrievalStatus
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.video.CapturePhase
import org.worldofhacks.sweep.bridge.node.FakeAircraft
import org.worldofhacks.sweep.bridge.node.FlightStates
import org.worldofhacks.sweep.bridge.node.LinkTiming
import org.worldofhacks.sweep.bridge.node.NodeConfig
import org.worldofhacks.sweep.bridge.node.PhoneStatus
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource
import org.worldofhacks.sweep.bridge.node.ReadinessInput
import org.worldofhacks.sweep.bridge.node.RelayLink
import org.worldofhacks.sweep.bridge.node.StubRelay
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState

/**
 * The Phase G camera path behind the relay link, on the fake camera port: the frame order
 * on the wire for every camera operation, the pending-then-completed media records with a
 * SHA-256 that matches the synthetic file on disk, the readiness gates, and the refusals
 * (native panorama, unknown file, gimbal not reached, storage low, photo mode left, a short
 * download).
 */
class CameraExecutorTest {
    private val key = "adapter-key-0123456789abcdef0123456789abcdef".toByteArray(Charsets.UTF_8)
    private val timing = LinkTiming(telemetryHz = 10.0, watchdogPollMs = 20, initialBackoffMs = 50, maxBackoffMs = 200, authTimeoutMs = 2_000, joinFallbackMs = 500)
    private val phone = PhoneStatusSource { PhoneStatus(batteryPercent = 81, thermalState = PhoneThermalState.NONE) }
    private val logs = CopyOnWriteArrayList<String>()
    private val roots = CopyOnWriteArrayList<File>()

    private class Node(
        val aircraft: FakeAircraft,
        val port: FakeCameraPort,
        val executor: CameraExecutor,
        val link: RelayLink,
        val root: File,
    ) : AutoCloseable {
        override fun close() {
            link.close()
            executor.close()
            port.close()
        }
    }

    private fun node(stub: StubRelay, config: CameraConfig = CameraConfig(gimbalTimeoutMs = 1_000, gimbalPollMs = 20)): Node {
        val aircraft = FakeAircraft(connected = true)
        aircraft.update { it.copy(state = FlightStates.HOVERING, x = 1.5, y = -0.25, z = 1.2, yawDeg = 45.0) }
        val port = FakeCameraPort(connected = { aircraft.snapshot.value.aircraftConnected })
        val root = Files.createTempDirectory("captures").toFile().also { roots += it }
        val executor = CameraExecutor(
            port,
            aircraft,
            root,
            config = config,
            log = { logs += it },
            onFacts = { probe -> aircraft.update { it.copy(camera = probe) } },
        )
        val nodeConfig = NodeConfig(stub.url, stub.session, 1, String(key, Charsets.UTF_8), "test-node-1", listOf("flight", "reconstruct_8"))
        val link = RelayLink(nodeConfig, aircraft, executor, phone, timing = timing, log = { logs += it }, captureReadiness = executor)
        executor.frames = link.frames
        link.setReadiness(ReadinessInput(homePoseConfirmed = true, controlAuthority = true, rcSafetyOperatorPresent = true))
        link.start()
        return Node(aircraft, port, executor, link, root)
    }

    @AfterEach
    fun cleanup() {
        roots.forEach { it.deleteRecursively() }
    }

    private fun await(what: String, timeoutMs: Long = 10_000, predicate: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return
            Thread.sleep(10)
        }
        throw AssertionError("timed out waiting for $what; log:\n" + logs.joinToString("\n"))
    }

    private fun JsonObject.str(key: String): String = (this[key] as JsonString).value

    private fun JsonObject.bool(key: String): Boolean = (this[key] as JsonBool).value

    private fun StubRelay.awaitAck(commandId: String, status: String): JsonObject =
        awaitFrame("acknowledgement") { it.str("command_id") == commandId && it.str("status") == status }

    private fun StubRelay.acks(commandId: String): List<JsonObject> = frames("acknowledgement") { it.str("command_id") == commandId }

    private fun sha256(file: File): String = MessageDigest.getInstance("SHA-256").digest(file.readBytes()).joinToString("") { "%02x".format(it) }

    @Test
    fun `join sends capture_readiness and camera_capabilities refreshes the capabilities frame before completed`() {
        StubRelay(key).use { stub ->
            node(stub).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                val readiness = stub.awaitFrame("capture_readiness")
                assertEquals("visual_advisory", readiness.str("guidance_mode"))
                assertEquals("operator_approved", readiness.str("pose_source"))
                // The fake camera boots in photo mode as a Mini 3 does, so the arbiter's camera gate is open from join.
                assertTrue(readiness.bool("camera_ok"))
                assertTrue(readiness.bool("storage_ok"))
                val joinCapabilities = stub.awaitFrame("capabilities")
                assertEquals(emptyList<Any>(), (joinCapabilities["native_panorama_modes"] as org.worldofhacks.sweep.bridge.core.json.JsonArray).items)

                val before = stub.frames("capabilities").size
                val capabilities = stub.issueCommand(CommandArgs.CameraCapabilities)
                stub.awaitAck(capabilities.commandId, "completed")
                val frames = stub.frames.map { it.str("type") to ((it["command_id"] as? JsonString)?.value ?: "") }
                val capabilitiesIndex = frames.indexOfLast { it.first == "capabilities" }
                val completedIndex = frames.indexOfLast { it == ("acknowledgement" to capabilities.commandId) }
                assertTrue(stub.frames("capabilities").size == before + 1, "one refreshed capabilities frame")
                assertTrue(capabilitiesIndex in 0 until completedIndex, "capabilities precede the terminal acknowledgement: $frames")
                assertEquals(listOf("accepted", "executing", "completed"), stub.acks(capabilities.commandId).map { it.str("status") })
                assertTrue(stub.acks(capabilities.commandId).last().str("detail").contains("50 MB free"))
            }
        }
    }

    @Test
    fun `gimbal ready photo and retrieve run the reconstruct_8 pattern with pending then completed media records`() {
        StubRelay(key).use { stub ->
            node(stub).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }

                val gimbal = stub.issueCommand(CommandArgs.SetGimbalPitch(pitchMdeg = -15_000))
                val gimbalDone = stub.awaitAck(gimbal.commandId, "completed")
                assertTrue(gimbalDone.str("detail").contains("-15.0"), gimbalDone.str("detail"))
                assertEquals(-15.0, node.port.gimbalPitchDeg())

                val ready = stub.issueCommand(CommandArgs.CameraReady)
                stub.awaitAck(ready.commandId, "completed")
                val readiness = stub.awaitFrame("capture_readiness") { it.bool("camera_ok") && it.bool("storage_ok") }
                assertEquals(8, (readiness["coverage_missing"] as org.worldofhacks.sweep.bridge.core.json.JsonArray).items.size)

                val photo = stub.issueCommand(CommandArgs.CapturePhoto("cap-1"))
                stub.awaitAck(photo.commandId, "completed")
                val pending = stub.awaitFrame("media_file") { it.str("file_id") == "cap-1-frame-01" && it.str("retrieval_status") == "pending" }
                assertEquals(MediaFileRecord.PENDING_CHECKSUM, pending.str("checksum_sha256"))
                assertTrue(pending.str("storage_ref").startsWith("aircraft://"))
                val pose = pending["pose"] as JsonObject
                assertEquals(1.5, (pose["x"] as org.worldofhacks.sweep.bridge.core.json.JsonFloat).value)
                assertEquals(45.0, (pending["actual_yaw_deg"] as org.worldofhacks.sweep.bridge.core.json.JsonFloat).value)
                assertEquals(-15.0, (pending["gimbal_pitch_deg"] as org.worldofhacks.sweep.bridge.core.json.JsonFloat).value)
                val order = stub.frames.map { it.str("type") + ((it["command_id"] as? JsonString)?.value?.let { id -> "/$id" } ?: "") + ((it["status"] as? JsonString)?.value?.let { s -> "/$s" } ?: "") }
                val mediaIndex = order.indexOfFirst { it.startsWith("media_file") }
                val photoCompleted = order.indexOf("acknowledgement/${photo.commandId}/completed")
                assertTrue(mediaIndex in 0 until photoCompleted, "media_file precedes the shutter's completed ack: $order")
                assertTrue(node.executor.progress.value.phase is CapturePhase.Capturing)
                assertEquals(listOf(45.0), node.executor.progress.value.acceptedHeadingsDeg)
                // The readiness frame follows the coverage change without another command.
                stub.awaitFrame("capture_readiness") { (it["coverage_missing"] as org.worldofhacks.sweep.bridge.core.json.JsonArray).items.size == 7 }

                val retrieve = stub.issueCommand(CommandArgs.RetrieveMedia("cap-1-frame-01"))
                val retrieved = stub.awaitAck(retrieve.commandId, "completed")
                val completed = stub.awaitFrame("media_file") { it.str("file_id") == "cap-1-frame-01" && it.str("retrieval_status") == "completed" }
                val onDisk = File(node.root, "cap-1/FAKE_0001.JPG")
                assertTrue(onDisk.isFile, "downloaded file exists at ${onDisk.absolutePath}")
                assertEquals(sha256(onDisk), completed.str("checksum_sha256"))
                assertTrue(node.port.bytesOf(1)!!.contentEquals(onDisk.readBytes()), "bytes on disk are the camera's bytes")
                assertEquals(onDisk.toURI().toString(), completed.str("storage_ref"))
                assertTrue(retrieved.str("detail").contains(completed.str("checksum_sha256")))
                val retrieveOrder = stub.frames.map { it.str("type") + ((it["status"] as? JsonString)?.value?.let { s -> "/$s" } ?: "") + ((it["retrieval_status"] as? JsonString)?.value?.let { s -> "/$s" } ?: "") }
                assertTrue(retrieveOrder.indexOf("media_file/completed") < retrieveOrder.lastIndexOf("acknowledgement/completed"), retrieveOrder.toString())
                assertEquals("cap-1-frame-01", node.executor.status.value.files.single().fileId)
                assertEquals(onDisk.absolutePath, node.executor.status.value.files.single().path)

                // Frame numbering continues per capture id; a second capture starts at 01.
                val second = stub.issueCommand(CommandArgs.CapturePhoto("cap-1"))
                stub.awaitAck(second.commandId, "completed")
                stub.awaitFrame("media_file") { it.str("file_id") == "cap-1-frame-02" }
                val other = stub.issueCommand(CommandArgs.CapturePhoto("cap-2"))
                stub.awaitAck(other.commandId, "completed")
                stub.awaitFrame("media_file") { it.str("file_id") == "cap-2-frame-01" }
                assertEquals(RetrievalStatus.PENDING, node.executor.status.value.files.last().record.retrievalStatus)
            }
        }
    }

    @Test
    fun `panorama is unsupported and unknown files gimbal misses and low storage fail with contract reasons`() {
        StubRelay(key).use { stub ->
            node(stub).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }

                val panorama = stub.issueCommand(CommandArgs.CapturePanorama("cap-pano"))
                val unsupported = stub.awaitAck(panorama.commandId, "failed")
                assertEquals("camera_unsupported", unsupported.str("reason"))
                assertTrue(unsupported.str("detail").contains("Virtual Stick"))
                assertNull(stub.frames("media_file").firstOrNull())

                val missing = stub.issueCommand(CommandArgs.RetrieveMedia("never-captured"))
                assertEquals("download_failure", stub.awaitAck(missing.commandId, "failed").str("reason"))

                node.port.reportedPitchOverride = 12.0
                val gimbal = stub.issueCommand(CommandArgs.SetGimbalPitch(pitchMdeg = -30_000))
                val notReached = stub.awaitAck(gimbal.commandId, "failed")
                assertEquals("camera_failure", notReached.str("reason"))
                assertTrue(notReached.str("detail").contains("not reached"))
                node.port.reportedPitchOverride = null

                val outOfRange = stub.issueCommand(CommandArgs.SetGimbalPitch(pitchMdeg = 80_000))
                assertTrue(stub.awaitAck(outOfRange.commandId, "failed").str("detail").contains("outside"))

                // The RC's photo/video switch leaves photo mode: the gate closes, the link reports it, the shutter is refused.
                node.port.setPhotoMode(false)
                stub.awaitFrame("capture_readiness") { !it.bool("camera_ok") && it.bool("storage_ok") }
                val photoBeforeReady = stub.issueCommand(CommandArgs.CapturePhoto("cap-early"))
                assertEquals("camera_not_ready", stub.awaitAck(photoBeforeReady.commandId, "failed").str("reason"))

                node.port.setStorageRemainingBytes(1_000)
                val ready = stub.issueCommand(CommandArgs.CameraReady)
                val notReady = stub.awaitAck(ready.commandId, "failed")
                assertEquals("camera_not_ready", notReady.str("reason"))
                assertTrue(notReady.str("detail").contains("storage"))
                val gate = stub.awaitFrame("capture_readiness") { it.bool("camera_ok") && !it.bool("storage_ok") }
                assertNotNull(gate)

                node.port.setStorageRemainingBytes(50_000_000)
                node.port.downloadFailure = "link dropped"
                val readyAgain = stub.issueCommand(CommandArgs.CameraReady)
                stub.awaitAck(readyAgain.commandId, "completed")
                val photo = stub.issueCommand(CommandArgs.CapturePhoto("cap-3"))
                stub.awaitAck(photo.commandId, "completed")
                val retrieve = stub.issueCommand(CommandArgs.RetrieveMedia("cap-3-frame-01"))
                val failed = stub.awaitAck(retrieve.commandId, "failed")
                assertEquals("download_failure", failed.str("reason"))
                assertTrue(failed.str("detail").contains("link dropped"))
                assertEquals(1, stub.frames("media_file") { it.str("file_id") == "cap-3-frame-01" }.size, "no completed record after a failed download")
                assertTrue(node.executor.progress.value.phase == CapturePhase.Idle)
            }
        }
    }

    @Test
    fun `a short download is removed and answered download_failure with only the pending record`() {
        StubRelay(key).use { stub ->
            node(stub).use { node ->
                await("ready") { node.link.state.value.membership == "ready" }
                stub.awaitAck(stub.issueCommand(CommandArgs.CameraReady).commandId, "completed")
                val photo = stub.issueCommand(CommandArgs.CapturePhoto("cap-4"))
                stub.awaitAck(photo.commandId, "completed")
                stub.awaitFrame("media_file") { it.str("file_id") == "cap-4-frame-01" }

                // The port stops after 100 bytes yet reports success, as a swallowed write error would.
                node.port.truncateDownloadAt = 100
                val short = stub.issueCommand(CommandArgs.RetrieveMedia("cap-4-frame-01"))
                val failed = stub.awaitAck(short.commandId, "failed")
                assertEquals("download_failure", failed.str("reason"))
                assertTrue(failed.str("detail").contains("truncated: 100 of"), failed.str("detail"))
                assertTrue(failed.str("detail").contains("[retryable]"), failed.str("detail"))
                val onDisk = File(node.root, "cap-4/FAKE_0001.JPG")
                assertFalse(onDisk.exists(), "the partial file is removed")
                val records = stub.frames("media_file") { it.str("file_id") == "cap-4-frame-01" }
                assertEquals(listOf("pending"), records.map { it.str("retrieval_status") }, "no completed record for a short file")
                assertEquals(RetrievalStatus.PENDING, node.executor.status.value.files.single().record.retrievalStatus)
                assertTrue(node.executor.progress.value.phase == CapturePhase.Idle)

                // Retryable: the ledger still holds the file, and a whole download completes with the right checksum.
                node.port.truncateDownloadAt = null
                val retry = stub.issueCommand(CommandArgs.RetrieveMedia("cap-4-frame-01"))
                stub.awaitAck(retry.commandId, "completed")
                val completed = stub.awaitFrame("media_file") { it.str("file_id") == "cap-4-frame-01" && it.str("retrieval_status") == "completed" }
                assertTrue(onDisk.isFile)
                assertEquals(sha256(onDisk), completed.str("checksum_sha256"))
                assertTrue(node.port.bytesOf(1)!!.contentEquals(onDisk.readBytes()), "the retried file is whole")
            }
        }
    }
}
