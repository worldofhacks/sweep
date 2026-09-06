package org.worldofhacks.sweep.bridge.core.frames

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.AdmissionResult
import org.worldofhacks.sweep.bridge.core.admission.CommandAdmission
import org.worldofhacks.sweep.bridge.core.admission.FakeClock
import org.worldofhacks.sweep.bridge.core.json.Json

class BodyPulseContractTest {
    private val key = "body-pulse-test-key-0123456789abcdef".toByteArray()

    private fun command(args: CommandArgs.BodyPulse) = CommandFrame(
        t = 1_000, eventId = "pulse-event", session = "pulse-session",
        commandId = "pulse-command", intentId = "pulse-intent", rosterVersion = 1,
        droneId = 2, connectionEpoch = 3, seq = 1, issuedAt = 1_000, ttlMs = 2_000,
        operation = CommandOperation.BODY_PULSE, args = args,
    ).signed(key)

    @Test
    fun `bounded positive and negative pulses round trip through signed admission`() {
        for (speed in listOf(-250L, -1L, 1L, 250L)) {
            for (duration in listOf(100L, 500L)) {
                val original = command(CommandArgs.BodyPulse(speed, duration))
                val parsed = CommandFrame.parse(original.toJson())
                assertEquals(original, parsed)
                assertEquals(CommandArgs.BodyPulse(speed, duration), parsed.args)
                val admission = CommandAdmission(key, 2, FakeClock(1_000))
                admission.bind(connectionEpoch = 3, rosterVersion = 1)
                assertTrue(admission.admit(parsed) is AdmissionResult.Admitted)
            }
        }
    }

    @Test
    fun `pulse direction and duration are covered by the signature`() {
        val signed = command(CommandArgs.BodyPulse(250, 500))
        for (changed in listOf(Json.json("forward_mm_s" to -250, "duration_ms" to 500), Json.json("forward_mm_s" to 250, "duration_ms" to 100))) {
            val parsed = CommandFrame.parse(signed.toJson().with("args", changed))
            assertFalse(parsed.verify(key))
            val admission = CommandAdmission(key, 2, FakeClock(1_000))
            admission.bind(connectionEpoch = 3, rosterVersion = 1)
            assertTrue(admission.admit(parsed) is AdmissionResult.Rejected)
        }
    }

    @Test
    fun `decoder refuses malformed out of range and goto shaped pulses`() {
        val invalid = listOf(
            Json.json("forward_mm_s" to 0, "duration_ms" to 500),
            Json.json("forward_mm_s" to 251, "duration_ms" to 500),
            Json.json("forward_mm_s" to -251, "duration_ms" to 500),
            Json.json("forward_mm_s" to Long.MIN_VALUE, "duration_ms" to 500),
            Json.json("forward_mm_s" to true, "duration_ms" to 500),
            Json.json("forward_mm_s" to "250", "duration_ms" to 500),
            Json.json("forward_mm_s" to 250.5, "duration_ms" to 500),
            Json.json("forward_mm_s" to 250, "duration_ms" to 99),
            Json.json("forward_mm_s" to 250, "duration_ms" to 501),
            Json.json("forward_mm_s" to 250, "duration_ms" to false),
            Json.json("forward_mm_s" to 250, "duration_ms" to 500.5),
            Json.json("forward_mm_s" to 250),
            Json.json("forward_mm_s" to 250, "duration_ms" to 500, "right_mm_s" to 0),
            Json.json("x_mm" to 0, "y_mm" to 125, "z_mm" to 1_200, "speed_mm_s" to 250),
        )
        val signed = command(CommandArgs.BodyPulse(250, 500))
        for (args in invalid) {
            val error = assertThrows(ContractError::class.java) { CommandFrame.parse(signed.toJson().with("args", args)) }
            assertEquals("invalid_command", error.code)
        }
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(signed.toJson().with("operation", Json.value("goto")))
        }
        assertThrows(ContractError::class.java) {
            CommandFrame.parse(signed.toJson().with("operation", Json.value("body_pulse_unbounded")))
        }
    }

    @Test
    fun `programmatic pulse construction enforces the same bounds`() {
        for (speed in listOf(0L, -251L, 251L, Long.MIN_VALUE)) {
            assertThrows(IllegalArgumentException::class.java) { CommandArgs.BodyPulse(speed, 500) }
        }
        for (duration in listOf(0L, 99L, 501L, Long.MAX_VALUE)) {
            assertThrows(IllegalArgumentException::class.java) { CommandArgs.BodyPulse(250, duration) }
        }
    }
}
