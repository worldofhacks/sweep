package org.worldofhacks.sweep.dji

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class CommandAdmissionTest {
    @Test
    fun `admits a fresh new command`() {
        val gate = CommandAdmissionGate(commandTtlMs = 250)

        assertEquals(CommandAdmission.Accepted, gate.admit(CommandEnvelope(7, 1_000), 1_120))
    }

    @Test
    fun `consumes an expired sequence before it rejects the command`() {
        val gate = CommandAdmissionGate(commandTtlMs = 250)

        assertEquals(CommandAdmission.Expired, gate.admit(CommandEnvelope(7, 1_000), 1_251))
        assertEquals(CommandAdmission.OutOfOrder, gate.admit(CommandEnvelope(7, 1_200), 1_220))
        assertEquals(CommandAdmission.Accepted, gate.admit(CommandEnvelope(8, 1_200), 1_220))
    }

    @Test
    fun `rejects old sequences before they can be sent`() {
        val gate = CommandAdmissionGate(commandTtlMs = 250)

        gate.admit(CommandEnvelope(8, 1_000), 1_020)

        assertEquals(CommandAdmission.OutOfOrder, gate.admit(CommandEnvelope(7, 1_000), 1_020))
    }

    @Test
    fun `requires nondecreasing send timestamps for measured cadence`() {
        val cadence = SendCadence()

        cadence.recordSend(1_000)
        cadence.recordSend(1_050)
        cadence.recordSend(1_100)

        assertEquals(20.0, cadence.observedHz())
        assertFailsWith<IllegalArgumentException> { cadence.recordSend(1_099) }
    }
}
