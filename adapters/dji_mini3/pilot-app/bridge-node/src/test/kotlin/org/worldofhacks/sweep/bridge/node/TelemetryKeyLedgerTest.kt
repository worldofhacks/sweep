package org.worldofhacks.sweep.bridge.node

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/** The listener decisions of the probe flavor's `ProbeAircraft`, without a DJI class in sight. */
class TelemetryKeyLedgerTest {
    private val keys = listOf("KeyConnection", "KeyAltitude", "KeySignalQuality")

    @Test
    fun `every key is listened at registration whatever isKeySupported answers`() {
        val ledger = TelemetryKeyLedger(keys)
        val asked = ArrayList<String>()
        val registered = ledger.attach(1_000) { name ->
            asked += name
            name == "KeyConnection"
        }
        assertEquals(keys, registered)
        assertEquals(keys, asked)
        assertEquals(1_000L, ledger.attachedAtMs)
        val status = ledger.snapshot()
        assertEquals(keys, status.keys.toList())
        assertTrue(status.values.all { it.listening })
        assertEquals(true, status.getValue("KeyConnection").supportedAtAttach)
        assertEquals(false, status.getValue("KeyAltitude").supportedAtAttach)
        assertNull(status.getValue("KeyAltitude").supportedAtConnect)
        assertNull(status.getValue("KeyAltitude").firstValueAtMs)
    }

    @Test
    fun `a product connect asks support again and registers nothing twice`() {
        val ledger = TelemetryKeyLedger(keys)
        ledger.attach(1_000) { false }
        // SdkSession calls attach() again on every product connect, then productConnected(true).
        assertEquals(emptyList<String>(), ledger.attach(5_000) { true })
        assertEquals(emptyList<String>(), ledger.productConnected { true })
        val connected = ledger.snapshot().getValue("KeyAltitude")
        assertTrue(connected.listening)
        assertEquals(false, connected.supportedAtAttach, "the registration-time answer is kept for the record")
        assertEquals(true, connected.supportedAtConnect)
        // An aircraft power cycle: asked again, still one listener per key.
        assertEquals(emptyList<String>(), ledger.productConnected { false })
        assertEquals(false, ledger.snapshot().getValue("KeyAltitude").supportedAtConnect)
        assertEquals(1_000L, ledger.attachedAtMs)
    }

    @Test
    fun `a connect before registration registers the missing listeners once`() {
        val ledger = TelemetryKeyLedger(keys)
        assertEquals(keys, ledger.productConnected { true })
        assertEquals(emptyList<String>(), ledger.attach(2_000) { true })
        val status = ledger.snapshot().getValue("KeyAltitude")
        assertTrue(status.listening)
        assertNull(status.supportedAtAttach)
        assertEquals(true, status.supportedAtConnect)
    }

    @Test
    fun `detach forgets the listeners and their evidence so the next attach registers them all again`() {
        val ledger = TelemetryKeyLedger(keys)
        ledger.attach(1_000) { false }
        assertTrue(ledger.value("KeyAltitude", 1_200))
        ledger.detach()
        assertNull(ledger.attachedAtMs)
        assertTrue(ledger.snapshot().values.none { it.listening })
        assertNull(ledger.status("KeyAltitude")?.firstValueAtMs)
        assertEquals(keys, ledger.attach(3_000) { true })
        assertEquals(3_000L, ledger.attachedAtMs)
        assertEquals(true, ledger.status("KeyAltitude")?.supportedAtAttach)
        assertEquals(emptyList<String>(), ledger.attach(3_100) { true })
    }

    @Test
    fun `the first value is recorded once and an unknown key is ignored`() {
        val ledger = TelemetryKeyLedger(keys)
        ledger.attach(1_000) { false }
        assertTrue(ledger.value("KeyAltitude", 1_250))
        assertFalse(ledger.value("KeyAltitude", 1_300))
        assertEquals(1_250L, ledger.status("KeyAltitude")?.firstValueAtMs)
        assertFalse(ledger.value("KeyNotListened", 1_400))
        assertNull(ledger.status("KeyNotListened"))
        val copy = ledger.snapshot()
        ledger.value("KeyConnection", 1_500)
        assertNull(copy.getValue("KeyConnection").firstValueAtMs, "snapshot is a copy")
    }
}
