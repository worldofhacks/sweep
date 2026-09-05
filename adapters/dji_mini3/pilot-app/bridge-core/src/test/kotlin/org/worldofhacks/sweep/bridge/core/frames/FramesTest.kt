package org.worldofhacks.sweep.bridge.core.frames

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.Fixtures
import org.worldofhacks.sweep.bridge.core.Fixtures.obj
import org.worldofhacks.sweep.bridge.core.Fixtures.string
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonValue
import org.worldofhacks.sweep.bridge.core.signing.Signing
import org.worldofhacks.sweep.bridge.core.watchdog.NodeWatchdogState

/**
 * Every frame is checked three ways against the Python-generated wire vector: the typed
 * model built from fields encodes to the same canonical bytes, the wire parses back to
 * the same model, and (for signed frames) the signature verifies with the vector key.
 */
class FramesTest {
    private val frames = Fixtures.load("frames.json")

    private fun wire(name: String): JsonObject = frames.obj(name).obj("wire")

    private fun key(name: String): ByteArray = frames.obj(name).string("key").toByteArray(Charsets.UTF_8)

    private fun assertSameWire(name: String, actual: JsonValue) {
        assertEquals(Json.canonical(wire(name)), Json.canonical(actual), name)
    }

    @Test
    fun `auth frame`() {
        val frame = AuthFrame(droneId = 1, token = "adapter-token-1")
        assertSameWire("auth", frame.toJson())
        assertEquals(frame, AuthFrame.parse(wire("auth")))
        assertFalse(frame.toString().contains("adapter-token-1"), "toString must not leak the token")
        assertThrows(ContractError::class.java) { AuthFrame.parse(Json.json("v" to 1, "type" to "auth")) }
    }

    @Test
    fun `auth accepted carries the relay-distributed node thresholds`() {
        val accepted = AuthAccepted.parse(wire("auth_accepted"))
        assertEquals("adapter", accepted.source)
        assertEquals(1, accepted.droneId)
        assertEquals(
            NodeSettings(commandTtlMs = 2000, virtualStickHz = 10, watchdogHoldMs = 2000, watchdogFailsafeMs = 10000),
            accepted.node,
        )
        assertSameWire("auth_accepted", accepted.toEvent())
        val console = AuthAccepted.parse(
            JsonObject(wire("auth_accepted").fields + ("source" to Json.value("console")) + ("drone_id" to Json.value(null)) + ("node" to Json.value(null))),
        )
        assertNull(console.node)
        assertThrows(ContractError::class.java) {
            NodeSettings.parse(
                Json.json("command_ttl_ms" to 2000, "virtual_stick_hz" to 30, "watchdog_hold_ms" to 2000, "watchdog_failsafe_ms" to 10000),
                "x",
            )
        }
        assertThrows(ContractError::class.java) {
            NodeSettings.parse(
                Json.json("command_ttl_ms" to 2000, "virtual_stick_hz" to 10, "watchdog_hold_ms" to 10000, "watchdog_failsafe_ms" to 10000),
                "x",
            )
        }
    }

    @Test
    fun `auth refused parses the machine-readable reason`() {
        val refused = AuthRefused.parse(wire("auth_refused"))
        assertEquals("session_closed", refused.reason)
        assertTrue(refused.detail.contains("new session ID"))
        assertSameWire("auth_refused", refused.toEvent())
    }

    @Test
    fun `membership join encodes signs and parses`() {
        val join = MembershipFrame.Join(
            t = 1000,
            eventId = "evt-join-1",
            session = "session-a",
            droneId = 1,
            adapterId = "dji_mini3",
            capabilities = listOf("flight", "camera"),
        )
        assertSameWire("membership_join", join.signed(key("membership_join")))
        assertEquals(join, MembershipFrame.parse(wire("membership_join")))
        assertEquals(wire("membership_join").string("signature"), join.sign(key("membership_join")))
    }

    @Test
    fun `membership readiness encodes signs and parses`() {
        val readiness = MembershipFrame.Readiness(
            t = 1001,
            eventId = "evt-ready-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 1,
            homePoseConfirmed = true,
            controlAuthority = true,
            rcSafetyOperatorPresent = true,
        )
        assertSameWire("membership_readiness", readiness.signed(key("membership_readiness")))
        assertEquals(readiness, MembershipFrame.parse(wire("membership_readiness")))
    }

    @Test
    fun `membership graceful leave encodes signs and parses`() {
        val leave = MembershipFrame.GracefulLeave(
            t = 1002,
            eventId = "evt-leave-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 1,
        )
        assertSameWire("membership_graceful_leave", leave.signed(key("membership_graceful_leave")))
        assertEquals(leave, MembershipFrame.parse(wire("membership_graceful_leave")))
    }

    @Test
    fun `membership rejects relay-internal actions unknown fields and empty capabilities`() {
        val base = wire("membership_join")
        assertThrows(ContractError::class.java) {
            MembershipFrame.parse(JsonObject(base.fields + ("action" to Json.value("unexpected_loss"))))
        }
        assertThrows(ContractError::class.java) {
            MembershipFrame.parse(JsonObject(base.fields + ("extra" to Json.value(1))))
        }
        val error = assertThrows(ContractError::class.java) {
            MembershipFrame.parse(JsonObject(base.fields + ("capabilities" to Json.value(emptyList<String>()))))
        }
        assertEquals("invalid_membership", error.code)
    }

    @Test
    fun `relay membership event parses the join answer with epoch and roster`() {
        val event = MembershipEvent.parse(wire("membership_event"))
        assertEquals("join", event.action)
        assertEquals(1, event.droneId)
        assertEquals(2, event.connectionEpoch)
        assertEquals("registered", event.membership)
        assertEquals(4, event.rosterVersion)
        assertEquals("authenticated_rejoin", event.reason)
        assertEquals(listOf("telemetry_missing", "home_pose_missing"), event.readinessReasons)
        assertEquals("adapter_signature", event.provenance)
        // Relay-authored events may grow fields without a node release.
        val grown = MembershipEvent.parse(JsonObject(wire("membership_event").fields + ("future_field" to Json.value(1))))
        assertEquals(event, grown)
        assertThrows(ContractError::class.java) {
            MembershipEvent.parse(JsonObject(wire("membership_event").fields - "connection_epoch"))
        }
    }

    @Test
    fun `state projection parses the roster stop flag and this drone's row`() {
        val state = StateEvent.parse(wire("state"))
        assertEquals(3, state.rosterVersion)
        assertFalse(state.estop)
        assertFalse(state.armed)
        val mine = checkNotNull(state.drone(1))
        assertEquals("ready", mine.membership)
        assertEquals(1, mine.connectionEpoch)
        assertEquals(emptyList<String>(), mine.readinessReasons)
        assertEquals("landed", mine.flightState)
        assertEquals(0.87, mine.battery)
        assertEquals(true, mine.controlAuthority)
        assertNull(state.drone(2))
    }

    @Test
    fun `refusal parses with nullable context fields`() {
        val refusal = RefusalEvent.parse(wire("refusal"))
        assertEquals("stale_timestamp", refusal.reason)
        assertEquals(1, refusal.droneId)
        assertNull(refusal.intentId)
        assertNull(refusal.commandId)
        assertEquals(3, refusal.rosterVersion)
    }

    @Test
    fun `telemetry encodes and parses`() {
        val telemetry = TelemetryFrame(
            t = 2000,
            eventId = "evt-telemetry-1",
            session = "session-a",
            drone = 1,
            connectionEpoch = 1,
            x = 1.5,
            y = -0.25,
            z = 1.0,
            vx = 0.1,
            vy = 0.0,
            vz = -0.05,
            battery = 0.87,
            state = "hovering",
            link = 0.95,
            posQuality = 0.6,
        )
        assertSameWire("telemetry", telemetry.toEvent())
        assertEquals(telemetry, TelemetryFrame.parse(wire("telemetry")))
    }

    @Test
    fun `telemetry rejects out-of-range unit fields and non-finite numbers`() {
        val base = wire("telemetry")
        val error = assertThrows(ContractError::class.java) {
            TelemetryFrame.parse(JsonObject(base.fields + ("battery" to Json.value(1.5))))
        }
        assertEquals("invalid_telemetry", error.code)
        assertThrows(ContractError::class.java) {
            TelemetryFrame.parse(JsonObject(base.fields + ("x" to Json.value(true))))
        }
        assertThrows(ContractError::class.java) {
            TelemetryFrame.parse(JsonObject(base.fields - "link"))
        }
    }

    @Test
    fun `acknowledgement encodes and parses`() {
        val ack = AcknowledgementFrame(
            t = 3000,
            eventId = "evt-ack-1",
            session = "session-a",
            intentId = "intent-1",
            commandId = "cmd-1",
            status = LifecycleStatus.FAILED,
            droneId = 1,
            connectionEpoch = 1,
            rosterVersion = 3,
            reason = "stale_command",
            detail = "issued_at plus ttl_ms elapsed",
        )
        assertSameWire("acknowledgement", ack.toEvent())
        assertEquals(ack, AcknowledgementFrame.parse(wire("acknowledgement")))
    }

    @Test
    fun `acknowledgement rejects refused terminal-without-reason and non-machine reasons`() {
        val base = wire("acknowledgement")
        assertThrows(ContractError::class.java) {
            AcknowledgementFrame.parse(JsonObject(base.fields + ("status" to Json.value("refused"))))
        }
        assertThrows(ContractError::class.java) {
            AcknowledgementFrame.parse(JsonObject(base.fields + ("reason" to Json.value(null))))
        }
        assertThrows(ContractError::class.java) {
            AcknowledgementFrame.parse(JsonObject(base.fields + ("reason" to Json.value("Not Snake"))))
        }
        val ok = AcknowledgementFrame.parse(
            JsonObject(base.fields + ("status" to Json.value("completed")) + ("reason" to Json.value(null))),
        )
        assertEquals(LifecycleStatus.COMPLETED, ok.status)
    }

    @Test
    fun `command parses verifies and re-encodes byte for byte`() {
        val command = CommandFrame.parse(wire("command"))
        assertEquals("cmd-1", command.commandId)
        assertEquals("intent-1", command.intentId)
        assertEquals(3, command.rosterVersion)
        assertEquals(1, command.droneId)
        assertEquals(1, command.connectionEpoch)
        assertEquals(7L, command.seq)
        assertEquals(4000L, command.issuedAt)
        assertEquals(1500L, command.ttlMs)
        assertEquals(CommandOperation.GOTO, command.operation)
        assertEquals(CommandArgs.Goto(xMm = 1000, yMm = 2500, zMm = 1200, speedMmS = 500), command.args)
        assertTrue(command.verify(key("command")))
        assertFalse(command.verify("wrong".toByteArray()))
        assertSameWire("command", command.toJson())
    }

    @Test
    fun `command built from fields signs to the vector signature`() {
        val built = CommandFrame(
            t = 4000,
            eventId = "evt-cmd-1",
            session = "session-a",
            commandId = "cmd-1",
            intentId = "intent-1",
            rosterVersion = 3,
            droneId = 1,
            connectionEpoch = 1,
            seq = 7,
            issuedAt = 4000,
            ttlMs = 1500,
            operation = CommandOperation.GOTO,
            args = CommandArgs.Goto(xMm = 1000, yMm = 2500, zMm = 1200, speedMmS = 500),
        ).signed(key("command"))
        assertEquals(wire("command").string("signature"), built.signature)
        assertSameWire("command", built.toJson())
    }

    @Test
    fun `every operation has integer-only typed args`() {
        val samples = frames.obj("command_args")
        for (operation in CommandOperation.entries) {
            val raw = samples.obj(operation.wire)
            val args = CommandArgs.parse(operation, raw)
            assertEquals(Json.canonical(raw), Json.canonical(args.toJson()), operation.wire)
        }
        // Floats, non-positive speeds, and stray fields all fail exactly as the relay refuses them.
        assertThrows(ContractError::class.java) {
            CommandArgs.parse(CommandOperation.TAKEOFF, Json.json("z_mm" to 1.2))
        }
        assertThrows(ContractError::class.java) {
            CommandArgs.parse(CommandOperation.GOTO, Json.json("x_mm" to 1, "y_mm" to 2, "z_mm" to 3, "speed_mm_s" to 0))
        }
        assertThrows(ContractError::class.java) {
            CommandArgs.parse(CommandOperation.ROTATE_TO, Json.json("yaw" to 90, "speed" to 30))
        }
        assertThrows(ContractError::class.java) {
            CommandArgs.parse(CommandOperation.HOVER, Json.json("z_mm" to 1))
        }
        assertEquals(CommandArgs.Takeoff(zMm = -5), CommandArgs.parse(CommandOperation.TAKEOFF, Json.json("z_mm" to -5)))
    }

    @Test
    fun `command rejects unknown operation missing field zero seq and bad signature shape`() {
        val base = wire("command")
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(JsonObject(base.fields + ("operation" to Json.value("fly_home"))))
        }
        assertThrows(ContractError::class.java) { CommandFrame.parse(JsonObject(base.fields - "seq")) }
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(JsonObject(base.fields + ("seq" to Json.value(0))))
        }
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(JsonObject(base.fields + ("signature" to Json.value(""))))
        }
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(JsonObject(base.fields + ("ttl_ms" to Json.value(-1))))
        }
    }

    @Test
    fun `capabilities encodes and parses flat`() {
        val capabilities = CapabilitiesFrame(
            t = 5000,
            eventId = "evt-cap-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 1,
            camera = CameraProbe(
                nativePanoramaModes = emptyList(),
                photoCapture = true,
                gimbalPitchMinDeg = -90.0,
                gimbalPitchMaxDeg = 20.0,
                horizontalFovDeg = 82.1,
                storageRemainingBytes = 12_000_000_000L,
                mediaRetrieval = true,
            ),
            hardware = HardwareProfile(
                aircraftModel = "DJI Mini 3",
                aircraftFirmware = "unreported",
                rcFirmware = "unreported",
                phoneModel = "Solana Seeker",
                androidVersion = "16",
                sdkVersion = "5.18.0",
                measuredHfovDeg = null,
            ),
        )
        assertSameWire("capabilities", capabilities.toEvent())
        assertEquals(capabilities, CapabilitiesFrame.parse(wire("capabilities")))
        assertThrows(ContractError::class.java) {
            CapabilitiesFrame.parse(JsonObject(wire("capabilities").fields + ("gimbal_pitch_max_deg" to Json.value(-95.0))))
        }
        assertThrows(ContractError::class.java) {
            CapabilitiesFrame.parse(JsonObject(wire("capabilities").fields + ("measured_hfov_deg" to Json.value(200.0))))
        }
        assertThrows(ContractError::class.java) {
            CapabilitiesFrame.parse(JsonObject(wire("capabilities").fields + ("aircraft_firmware" to Json.value(null))))
        }
    }

    @Test
    fun `capture readiness encodes and parses`() {
        val readiness = CaptureReadinessFrame(
            t = 6000,
            eventId = "evt-readiness-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 1,
            roomId = "office-101",
            captureId = "cap-0042",
            guidanceMode = GuidanceMode.VISUAL_ADVISORY,
            poseSource = "operator_approved",
            poseOk = true,
            clearanceOk = true,
            cameraOk = true,
            storageOk = true,
            motionOk = true,
            imageQualityOk = false,
            coverageMissing = listOf(90.0, 135.0),
            nextHeadingDeg = 90.0,
            suggestedDelta = SuggestedDelta(kind = DeltaKind.YAW, degrees = 12.0),
        )
        assertSameWire("capture_readiness", readiness.toEvent())
        assertEquals(readiness, CaptureReadinessFrame.parse(wire("capture_readiness")))
        val idle = CaptureReadinessFrame.parse(
            JsonObject(
                wire("capture_readiness").fields +
                    ("room_id" to Json.value(null)) + ("capture_id" to Json.value(null)) +
                    ("suggested_delta" to Json.value(null)) + ("next_heading_deg" to Json.value(null)),
            ),
        )
        assertNull(idle.suggestedDelta)
        assertNull(idle.roomId)
        assertThrows(ContractError::class.java) {
            CaptureReadinessFrame.parse(JsonObject(wire("capture_readiness").fields + ("coverage_missing" to Json.value(listOf(360.0)))))
        }
    }

    @Test
    fun `node status encodes and parses`() {
        val status = NodeStatusFrame(
            t = 7000,
            eventId = "evt-status-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 1,
            body = NodeStatusBody(
                virtualStickEnabled = false,
                controlAuthority = true,
                authorityChangeReason = null,
                watchdogState = NodeWatchdogState.NOMINAL,
                videoPublishState = VideoPublishState.STOPPED,
                phoneBatteryPercent = 72,
                phoneThermalState = PhoneThermalState.NONE,
            ),
        )
        assertSameWire("node_status", status.toEvent())
        assertEquals(status, NodeStatusFrame.parse(wire("node_status")))
        assertThrows(ContractError::class.java) {
            NodeStatusFrame.parse(JsonObject(wire("node_status").fields + ("watchdog_state" to Json.value("armed"))))
        }
        assertThrows(ContractError::class.java) {
            NodeStatusFrame.parse(JsonObject(wire("node_status").fields + ("phone_battery_percent" to Json.value(101))))
        }
        assertThrows(ContractError::class.java) {
            NodeStatusFrame.parse(JsonObject(wire("node_status").fields + ("video_publish_state" to Json.value("idle"))))
        }
        assertThrows(IllegalArgumentException::class.java) {
            status.body.copy(authorityChangeReason = "Not Snake")
        }
    }

    @Test
    fun `signed node-authored frames are not required but signing helper works on any event`() {
        val event = TelemetryFrame.parse(wire("telemetry")).toEvent()
        val signature = Signing.sign(event, "k".toByteArray())
        assertTrue(Signing.verify(event, signature, "k".toByteArray()))
    }
}
