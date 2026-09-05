package org.worldofhacks.sweep.bridge.core.signing

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.Fixtures
import org.worldofhacks.sweep.bridge.core.Fixtures.obj
import org.worldofhacks.sweep.bridge.core.Fixtures.string
import org.worldofhacks.sweep.bridge.core.json.Json

class SigningTest {
    @Test
    fun `signatures match relay auth sign_event for every generated vector`() {
        val cases = Fixtures.cases("hmac_sha256.json")
        assertTrue(cases.size >= 3)
        for (case in cases) {
            val key = case.string("key").toByteArray(Charsets.UTF_8)
            val unsigned = case.obj("unsigned_event")
            assertEquals(case.string("canonical"), Json.canonical(unsigned), "canonical ${case.string("name")}")
            assertEquals(case.string("signature"), Signing.sign(unsigned, key), "signature ${case.string("name")}")
            assertTrue(Signing.verify(unsigned, case.string("signature"), key), "verify ${case.string("name")}")
        }
    }

    @Test
    fun `verification rejects wrong key tampered payload and malformed signatures`() {
        val key = "node-key".toByteArray()
        val event = Json.json("v" to 1, "type" to "membership", "drone_id" to 1)
        val signature = Signing.sign(event, key)
        assertFalse(Signing.verify(event, signature, "other-key".toByteArray()))
        assertFalse(Signing.verify(Json.json("v" to 1, "type" to "membership", "drone_id" to 2), signature, key))
        assertFalse(Signing.verify(event, signature.uppercase(), key))
        assertFalse(Signing.verify(event, signature.dropLast(1), key))
        assertFalse(Signing.verify(event, signature.dropLast(1) + "g", key))
        assertFalse(Signing.verify(event, "", key))
    }
}
