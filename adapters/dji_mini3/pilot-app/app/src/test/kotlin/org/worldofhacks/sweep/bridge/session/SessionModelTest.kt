package org.worldofhacks.sweep.bridge.session

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class SessionModelTest {
    @Test
    fun `registration walks initializing registering registered`() {
        val model = SessionModel()
        assertEquals(Registration.INITIALIZING, model.current.registration)
        model.initProgress("INITIALIZE_COMPLETE (100)")
        model.registering()
        assertEquals(Registration.REGISTERING, model.current.registration)
        model.registerSucceeded()
        assertEquals(Registration.REGISTERED, model.current.registration)
        model.registerFailed("no network")
        assertEquals(Registration.FAILED, model.current.registration)
        assertEquals("no network", model.current.registrationDetail)
    }

    @Test
    fun `every connect disconnect and change bumps the generation`() {
        val model = SessionModel()
        assertEquals(1L, model.productConnected(7))
        assertEquals(ProductConnection.CONNECTED, model.current.product)
        assertEquals(7, model.current.productId)
        assertEquals(2L, model.productChanged(7))
        assertEquals(3L, model.productDisconnected(7))
        assertEquals(ProductConnection.DISCONNECTED, model.current.product)
        assertNull(model.current.productId)
        assertEquals(3L, model.current.generation)
    }

    @Test
    fun `identity from the current generation is applied`() {
        val model = SessionModel()
        val generation = model.productConnected(1)
        val applied = model.identity(generation, "Product identity", "DJI_MINI_3") {
            it.copy(productType = "DJI_MINI_3", isMini3 = true)
        }
        assertTrue(applied)
        assertEquals("DJI_MINI_3", model.current.identity.productType)
        assertEquals(true, model.current.identity.isMini3)
        assertEquals(0, model.current.droppedCallbacks)
    }

    @Test
    fun `late identity from an old generation is dropped not applied`() {
        val model = SessionModel()
        val first = model.productConnected(1)
        model.productDisconnected(1)
        val second = model.productConnected(2)
        val applied = model.identity(first, "Product identity", "stale") {
            it.copy(productType = "STALE_PRODUCT", isMini3 = false)
        }
        assertFalse(applied)
        assertNull(model.current.identity.productType)
        assertEquals(1, model.current.droppedCallbacks)
        assertTrue(model.identity(second, "Product identity", "fresh") { it.copy(productType = "DJI_MINI_3") })
        assertEquals("DJI_MINI_3", model.current.identity.productType)
        assertTrue(model.current.events.any { it.name == "Dropped callback" })
    }

    @Test
    fun `disconnect clears identity so a reconnect starts blank`() {
        val model = SessionModel()
        val generation = model.productConnected(1)
        model.identity(generation, "Aircraft firmware", "x") { it.copy(aircraftFirmware = "01.00") }
        model.productDisconnected(1)
        assertEquals(AircraftIdentity(), model.current.identity)
    }

    @Test
    fun `events are capped and ordered`() {
        val model = SessionModel()
        repeat(300) { model.event("tick", "$it") }
        assertEquals(250, model.current.events.size)
        assertEquals("299", model.current.events.last().detail)
        assertTrue(model.current.events.zipWithNext().all { (a, b) -> a.seq < b.seq })
    }

    @Test
    fun `probe report renders state and environment`() {
        val model = SessionModel()
        val generation = model.productConnected(1)
        model.identity(generation, "Product identity", "DJI_MINI_3") { it.copy(productType = "DJI_MINI_3", isMini3 = true) }
        val text = ProbeReport.render(
            model.current,
            ProbeReport.Environment(
                aircraftVariant = "fake",
                applicationId = "org.worldofhacks.sweep.bridge",
                appVersion = "0.1.0 (1)",
                msdkVersion = "5.18.0",
                phone = "Solana Mobile Inc. Seeker",
                android = "16 / API 36",
            ),
            exportedAtMs = 123,
        )
        assertTrue(text.contains("exported_at_ms: 123"))
        assertTrue(text.contains("product_type: DJI_MINI_3"))
        assertTrue(text.contains("is_dji_mini_3: true"))
        assertTrue(text.contains("connection_generation: 1"))
        assertTrue(text.contains("msdk_version: 5.18.0"))
    }
}
