package org.worldofhacks.sweep.bridge.core.admission

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertInstanceOf
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.CommandArgs
import org.worldofhacks.sweep.bridge.core.frames.CommandFrame
import org.worldofhacks.sweep.bridge.core.frames.CommandOperation

class CommandAdmissionTest {
    private val key = "node-key-1".toByteArray()
    private val clock = FakeClock(nowMs = 10_000)

    private fun admission(offsetMs: Long = 0): CommandAdmission =
        CommandAdmission(key = key, droneId = 1, clock = clock).also {
            it.relayOffsetMs = offsetMs
            it.bind(connectionEpoch = 1, rosterVersion = 3)
        }

    private fun command(
        seq: Long,
        issuedAt: Long = clock.nowMs(),
        ttlMs: Long = 1500,
        rosterVersion: Int = 3,
        connectionEpoch: Int = 1,
        droneId: Int = 1,
        signingKey: ByteArray = key,
    ): CommandFrame = CommandFrame(
        t = issuedAt,
        eventId = "evt-$seq",
        session = "session-a",
        commandId = "cmd-$seq",
        intentId = "intent-1",
        rosterVersion = rosterVersion,
        droneId = droneId,
        connectionEpoch = connectionEpoch,
        seq = seq,
        issuedAt = issuedAt,
        ttlMs = ttlMs,
        operation = CommandOperation.HOVER,
        args = CommandArgs.Hover,
    ).signed(signingKey)

    private fun rejected(result: AdmissionResult): AdmissionResult.Rejected =
        assertInstanceOf(AdmissionResult.Rejected::class.java, result)

    @Test
    fun `fresh signed in-order command is admitted and advances the sequence`() {
        val gate = admission()
        assertInstanceOf(AdmissionResult.Admitted::class.java, gate.admit(command(seq = 1)))
        assertInstanceOf(AdmissionResult.Admitted::class.java, gate.admit(command(seq = 2)))
        assertEquals(2L, gate.lastSeq)
    }

    @Test
    fun `sequence must be strictly monotonic within an epoch`() {
        val gate = admission()
        gate.admit(command(seq = 5))
        assertEquals(AdmissionReason.OUT_OF_ORDER_COMMAND, rejected(gate.admit(command(seq = 5))).reason)
        assertEquals(AdmissionReason.OUT_OF_ORDER_COMMAND, rejected(gate.admit(command(seq = 4))).reason)
        assertEquals("out_of_order_command", AdmissionReason.OUT_OF_ORDER_COMMAND.wire)
        assertInstanceOf(AdmissionResult.Admitted::class.java, gate.admit(command(seq = 6)))
    }

    @Test
    fun `sequence restarts when the connection epoch changes`() {
        val gate = admission()
        gate.admit(command(seq = 9))
        gate.bind(connectionEpoch = 2, rosterVersion = 3)
        assertInstanceOf(AdmissionResult.Admitted::class.java, gate.admit(command(seq = 1, connectionEpoch = 2)))
    }

    @Test
    fun `stale command is rejected once issued_at plus ttl_ms has elapsed on the relay clock`() {
        val gate = admission()
        val issued = clock.nowMs() - 2000
        val result = rejected(gate.admit(command(seq = 1, issuedAt = issued, ttlMs = 1500)))
        assertEquals(AdmissionReason.STALE_COMMAND, result.reason)
        assertEquals("stale_command", result.reason.wire)
        assertTrue(result.detail.contains("ttl"), result.detail)
        // The boundary itself is still fresh: issued_at + ttl_ms == now.
        assertInstanceOf(
            AdmissionResult.Admitted::class.java,
            gate.admit(command(seq = 1, issuedAt = clock.nowMs() - 1500, ttlMs = 1500)),
        )
    }

    @Test
    fun `measured clock offset shifts the freshness window`() {
        // Relay clock runs 5 s ahead of the phone: a command stamped 5 s "in the future"
        // on local terms is fresh, and one stamped at local now is already 5 s old.
        val ahead = admission(offsetMs = 5000)
        assertInstanceOf(
            AdmissionResult.Admitted::class.java,
            ahead.admit(command(seq = 1, issuedAt = clock.nowMs() + 5000, ttlMs = 1000)),
        )
        assertEquals(
            AdmissionReason.STALE_COMMAND,
            rejected(ahead.admit(command(seq = 2, issuedAt = clock.nowMs(), ttlMs = 1000))).reason,
        )
    }

    @Test
    fun `commands from the far future are stale not admitted`() {
        val gate = admission()
        val result = rejected(gate.admit(command(seq = 1, issuedAt = clock.nowMs() + 60_000)))
        assertEquals(AdmissionReason.STALE_COMMAND, result.reason)
        assertTrue(result.detail.contains("future"), result.detail)
    }

    @Test
    fun `roster and epoch must match the bound values`() {
        val gate = admission()
        assertEquals(
            AdmissionReason.STALE_ROSTER,
            rejected(gate.admit(command(seq = 1, rosterVersion = 2))).reason,
        )
        assertEquals(
            AdmissionReason.STALE_CONNECTION_EPOCH,
            rejected(gate.admit(command(seq = 1, connectionEpoch = 2))).reason,
        )
        // The relay contract names exactly stale_command and out_of_order_command on the wire.
        assertEquals("stale_command", AdmissionReason.STALE_ROSTER.wire)
        assertEquals("stale_command", AdmissionReason.STALE_CONNECTION_EPOCH.wire)
    }

    @Test
    fun `only contract reasons are acknowledged and forged or misaddressed frames are dropped`() {
        assertEquals(
            setOf("stale_command", "out_of_order_command"),
            AdmissionReason.entries.filter { it.acknowledged }.map { it.wire }.toSet(),
        )
        assertEquals(
            setOf(AdmissionReason.INVALID_SIGNATURE, AdmissionReason.INVALID_SELECTION),
            AdmissionReason.entries.filterNot { it.acknowledged }.toSet(),
        )
    }

    @Test
    fun `wrong drone and bad signature are rejected before any state changes`() {
        val gate = admission()
        assertEquals(AdmissionReason.INVALID_SELECTION, rejected(gate.admit(command(seq = 1, droneId = 2))).reason)
        assertEquals(
            AdmissionReason.INVALID_SIGNATURE,
            rejected(gate.admit(command(seq = 1, signingKey = "impostor".toByteArray()))).reason,
        )
        assertEquals(null, gate.lastSeq)
    }

    @Test
    fun `rejections do not consume sequence numbers`() {
        val gate = admission()
        gate.admit(command(seq = 1, issuedAt = clock.nowMs() - 9000))
        assertEquals(null, gate.lastSeq)
        assertInstanceOf(AdmissionResult.Admitted::class.java, gate.admit(command(seq = 1)))
    }

    @Test
    fun `clock advances between admissions without sleeping`() {
        val gate = admission()
        val frame = command(seq = 1, ttlMs = 100)
        clock.advance(101)
        assertEquals(AdmissionReason.STALE_COMMAND, rejected(gate.admit(frame)).reason)
    }
}

class FakeClock(private var nowMs: Long) : Clock {
    override fun nowMs(): Long = nowMs

    fun advance(deltaMs: Long) {
        nowMs += deltaMs
    }
}
