package org.worldofhacks.sweep.bridge.flight

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class DirectAuthorityMonitorTest {
    private data class Read(val key: AuthorityKey, val request: AuthorityReadRequest)

    private class Harness {
        var nowMs = 0L
        val reads = mutableListOf<Read>()
        val records = mutableListOf<Triple<String, String, String>>()
        val states = mutableListOf<Pair<Boolean, String>>()
        val monitor = DirectAuthorityMonitor(
            nowMs = { nowMs },
            record = { key, event, status -> synchronized(records) { records += Triple(key, event, status) } },
            read = { key, request -> synchronized(reads) { reads += Read(key, request) } },
            publish = { enabled, owner -> synchronized(states) { states += enabled to owner } },
        )

        fun latestSnapshot(): AuthorityReadRequest = synchronized(reads) { reads.last().request }

        fun answer(request: AuthorityReadRequest, enabled: String, owner: String) {
            monitor.readResult(AuthorityKey.VIRTUAL_STICK_ENABLED, request, "ok", enabled)
            monitor.readResult(AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, request, "ok", owner)
        }
    }

    @Test
    fun `only a matched bounded post enable snapshot confirms MSDK authority`() {
        val h = Harness()
        h.monitor.productConnected()
        val operation = h.monitor.enableIssued()
        assertTrue(h.monitor.enableCompleted(operation, "ok"))
        val request = h.latestSnapshot()

        h.answer(request, "true", "MSDK")

        assertEquals(listOf(true to "MSDK"), h.states)
    }

    @Test
    fun `connection reads cannot confirm after an enable attempt starts`() {
        val h = Harness()
        h.monitor.productConnected()
        val connectionRead = h.latestSnapshot()
        val operation = h.monitor.enableIssued()

        h.answer(connectionRead, "true", "MSDK")
        assertEquals(emptyList<Pair<Boolean, String>>(), h.states)
        assertTrue(h.monitor.enableCompleted(operation, "ok"))
        h.answer(h.latestSnapshot(), "true", "MSDK")

        assertEquals(listOf(true to "MSDK"), h.states)
        assertTrue(h.records.any { it.second == "read_dropped" })
    }

    @Test
    fun `fresh idle connection snapshot exposes an enabled MSDK virtual stick for cleanup`() {
        val h = Harness()
        h.monitor.productConnected()

        h.answer(h.latestSnapshot(), "true", "MSDK")

        assertEquals(listOf(true to "MSDK"), h.states)
    }

    @Test
    fun `old enable completion cannot survive disable and retry`() {
        val h = Harness()
        h.monitor.productConnected()
        val oldEnable = h.monitor.enableIssued()
        h.monitor.disableIssued()
        val retry = h.monitor.enableIssued()

        assertFalse(h.monitor.enableCompleted(oldEnable, "ok"))
        assertTrue(h.monitor.enableCompleted(retry, "ok"))
        h.answer(h.latestSnapshot(), "true", "MSDK")

        assertEquals(listOf(true to "MSDK"), h.states)
        assertTrue(h.records.any { it.first == "VirtualStickManager.enableVirtualStick" && it.second == "completion_dropped" })
    }

    @Test
    fun `skewed post enable reads cannot pair into authority confirmation`() {
        val h = Harness()
        h.monitor.productConnected()
        val operation = h.monitor.enableIssued()
        h.monitor.enableCompleted(operation, "ok")
        val request = h.latestSnapshot()

        h.monitor.readResult(AuthorityKey.VIRTUAL_STICK_ENABLED, request, "ok", "true")
        h.nowMs = 251
        h.monitor.readResult(AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, request, "ok", "MSDK")

        assertEquals(emptyList<Pair<Boolean, String>>(), h.states)
        assertTrue(h.records.any { it.second == "pair_dropped" })
    }

    @Test
    fun `expired post enable snapshot cannot pair into authority confirmation`() {
        val h = Harness()
        h.monitor.productConnected()
        val operation = h.monitor.enableIssued()
        h.monitor.enableCompleted(operation, "ok")
        val request = h.latestSnapshot()
        h.nowMs = 900
        h.monitor.readResult(AuthorityKey.VIRTUAL_STICK_ENABLED, request, "ok", "true")
        h.nowMs = 1_001
        h.monitor.readResult(AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, request, "ok", "MSDK")

        assertEquals(emptyList<Pair<Boolean, String>>(), h.states)
        assertTrue(h.records.any { it.second == "pair_dropped" })
    }

    @Test
    fun `negative direct listener state fails closed without a refresh loop`() {
        val h = Harness()
        val generation = h.monitor.productConnected()
        val readsBeforeListener = h.reads.size

        assertTrue(h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "false"))
        assertTrue(h.monitor.listener(generation, AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, "RC"))

        assertEquals(readsBeforeListener, h.reads.size)
        assertEquals(listOf(false to "UNKNOWN", true to "RC"), h.states)
        assertEquals("rc_lost", takeoverReason("RC_LOST"))
        assertEquals("battery_low_landing", takeoverReason("BATTERY_SUPER_LOW_LANDING"))
        assertEquals(null, takeoverReason("MSDK_REQUEST"))
    }

    @Test
    fun `late listener from an earlier product is ignored`() {
        val h = Harness()
        h.monitor.productConnected()
        val readsForFirstProduct = h.reads.size
        h.monitor.productConnected()

        assertFalse(h.monitor.listener(1, AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON, "RC_LOST"))
        assertEquals(readsForFirstProduct * 2, h.reads.size)
        assertTrue(h.records.any { it.second == "listener_dropped" })
    }

    @Test
    fun `disconnect invalidates pending product reads`() {
        val h = Harness()
        h.monitor.productConnected()
        val beforeDisconnect = h.latestSnapshot()

        h.monitor.disconnected()
        h.answer(beforeDisconnect, "true", "MSDK")

        assertEquals(emptyList<Pair<Boolean, String>>(), h.states)
        assertTrue(h.records.any { it.first == "direct_authority" && it.second == "product_disconnected" })
    }

    @Test
    fun `concurrent direct read callbacks publish one synchronized snapshot`() {
        val h = Harness()
        h.monitor.productConnected()
        val operation = h.monitor.enableIssued()
        h.monitor.enableCompleted(operation, "ok")
        val request = h.latestSnapshot()
        val start = CountDownLatch(1)
        val done = CountDownLatch(2)
        val enabled = thread {
            start.await(1, TimeUnit.SECONDS)
            h.monitor.readResult(AuthorityKey.VIRTUAL_STICK_ENABLED, request, "ok", "true")
            done.countDown()
        }
        val owner = thread {
            start.await(1, TimeUnit.SECONDS)
            h.monitor.readResult(AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, request, "ok", "MSDK")
            done.countDown()
        }

        start.countDown()
        assertTrue(done.await(1, TimeUnit.SECONDS))
        enabled.join()
        owner.join()

        assertEquals(listOf(true to "MSDK"), h.states)
    }
}
