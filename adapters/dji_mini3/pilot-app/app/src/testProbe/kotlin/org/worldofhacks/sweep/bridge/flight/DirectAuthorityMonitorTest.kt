package org.worldofhacks.sweep.bridge.flight

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class DirectAuthorityMonitorTest {
    private class Harness {
        var nowMs = 0L
        val reads = mutableListOf<AuthorityReadRequest>()
        val records = mutableListOf<Triple<String, String, String>>()
        val states = mutableListOf<Pair<Boolean, String>>()
        val progress = mutableListOf<ContractProgress>()
        val monitor = DirectAuthorityMonitor(
            nowMs = { nowMs },
            record = { key, event, status -> records += Triple(key, event, status) },
            readMode = { reads += it },
            publish = { enabled, owner -> states += enabled to owner },
            progress = { progress += it },
        )

        fun connected(): Long {
            val generation = monitor.productConnected()
            monitor.productType("DJI_MINI_3", true)
            return generation
        }

        fun resetReady(operation: AuthorityOperation) {
            monitor.resetCompleted(operation, "ok")
            val resetRead = reads.removeAt(reads.lastIndex)
            assertFalse(resetRead.expectedEnabled)
            monitor.readResult(resetRead, "ok", "false")
        }

        fun enabled(operation: AuthorityOperation, generation: Long) {
            monitor.enableCompleted(operation, "ok")
            monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "true")
        }
    }

    @Test
    fun `current Mini 3 operation needs reset ack direct mode and fresh mode read`() {
        val h = Harness()
        val generation = h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())

        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "false")
        h.monitor.resetCompleted(operation, "ok")
        val resetRead = h.reads.removeAt(h.reads.lastIndex)
        assertFalse(resetRead.expectedEnabled)
        assertTrue(h.progress.isEmpty())
        h.monitor.readResult(resetRead, "ok", "false")
        assertEquals(listOf(ContractProgress.IssueEnable(operation)), h.progress)

        h.monitor.enableCompleted(operation, "ok")
        assertTrue(h.reads.isEmpty())
        h.nowMs = 491
        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "true")
        val read = h.reads.single()
        h.monitor.readResult(read, "ok", "true")

        assertEquals(ContractProgress.Verified(operation), h.progress.last())
        assertTrue(h.records.any { it.first == "virtual_stick_mode_contract" && it.second == "verified" && it.third.contains("raw_owner=UNKNOWN") })
    }


    @Test
    fun `reset needs a post acknowledgement false read even if the listener was already false`() {
        val h = Harness()
        val generation = h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())

        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "false")
        h.monitor.resetCompleted(operation, "ok")
        val resetRead = h.reads.single()
        h.monitor.readResult(resetRead, "ok", "true")

        assertTrue(h.progress.last() is ContractProgress.Failed)
        assertFalse(h.progress.any { it is ContractProgress.IssueEnable })
    }

    @Test
    fun `false listener during enable invalidates the pending true read`() {
        val h = Harness()
        val generation = h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())
        h.resetReady(operation)
        h.monitor.enableCompleted(operation, "ok")
        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "true")
        val trueRead = h.reads.single()

        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "false")
        h.monitor.readResult(trueRead, "ok", "true")

        assertTrue(h.progress.last() is ContractProgress.Failed)
        assertFalse(h.progress.any { it is ContractProgress.Verified })
    }

    @Test
    fun `fresh RC owner during direct true read invalidates the contract`() {
        val h = Harness()
        val generation = h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())
        h.resetReady(operation)
        h.monitor.enableCompleted(operation, "ok")
        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "true")
        val trueRead = h.reads.single()

        h.monitor.listener(generation, AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, "RC")
        h.monitor.readResult(trueRead, "ok", "true")

        assertTrue(h.progress.last() is ContractProgress.Failed)
        assertEquals(listOf(true to "RC"), h.states)
    }

    @Test
    fun `unsupported product type revokes a pending operation`() {
        val h = Harness()
        h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())

        h.monitor.productType("DJI_AIR_2S", false)

        assertTrue(h.progress.last() is ContractProgress.Failed)
        assertFalse(h.progress.any { it is ContractProgress.IssueEnable })
    }

    @Test
    fun `manager owner diagnostic cannot create contract proof`() {
        val h = Harness()
        h.connected()
        val diagnostic = ManagerAuthorityDiagnostic(
            record = { key, event, status -> h.records += Triple(key, event, status) },
            nowMs = { 42L },
        )

        diagnostic.record(enabled = true, advanced = true, owner = "MSDK", directContext = h.monitor.diagnosticContext())

        assertTrue(h.progress.isEmpty())
        assertTrue(h.records.any { it.first == "VirtualStickManager.state" && it.third.contains("diagnostic_only owner_freshness=unproven") })
    }

    @Test
    fun `late enable acknowledgement cannot promote a reset operation after cleanup`() {
        val h = Harness()
        val generation = h.connected()
        val old = requireNotNull(h.monitor.contractEnableIssued())
        h.resetReady(old)
        assertEquals(ContractProgress.IssueEnable(old), h.progress.single())

        h.monitor.disableIssued()
        h.monitor.enableCompleted(old, "ok")

        assertEquals(listOf(ContractProgress.IssueEnable(old)), h.progress)
        assertTrue(h.records.any { it.first == "VirtualStickManager.enableVirtualStick" && it.second == "completion_dropped" })
    }

    @Test
    fun `product change drops a pending fresh read`() {
        val h = Harness()
        val generation = h.connected()
        val operation = requireNotNull(h.monitor.contractEnableIssued())
        h.resetReady(operation)
        h.enabled(operation, generation)
        val request = h.reads.single()

        h.monitor.productConnected()
        h.monitor.readResult(request, "ok", "true")

        assertFalse(h.progress.any { it is ContractProgress.Verified })
        assertTrue(h.records.any { it.first == "KeyVirtualStickEnabled" && it.second == "read_dropped" })
    }

    @Test
    fun `fresh RC owner and virtual stick false revoke immediately`() {
        val h = Harness()
        val generation = h.connected()
        h.monitor.listener(generation, AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, "RC")
        h.monitor.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, "false")

        assertEquals(listOf(true to "RC", false to "UNKNOWN"), h.states)
    }

    @Test
    fun `unsupported or unverified product cannot start the contract`() {
        val h = Harness()
        h.monitor.productConnected()

        assertNull(h.monitor.contractEnableIssued())
        assertTrue(h.records.any { it.first == "virtual_stick_mode_contract" && it.second == "rejected" })
    }
}

class VirtualStickEnableFenceTest {
    private class Harness {
        val calls = mutableListOf<String>()
        val records = mutableListOf<Triple<String, String, String>>()
        private var compensation: ((String) -> Unit)? = null
        private var enable: ((String) -> Unit)? = null
        val fence = VirtualStickEnableFence(
            issueCompensatingDisable = { _, complete ->
                calls += "compensating_disable"
                compensation = complete
            },
            record = { key, event, status -> records += Triple(key, event, status) },
        )

        fun start(operation: AuthorityOperation, results: MutableList<String>): Boolean = fence.start(
            operation,
            issueEnable = { complete ->
                calls += "enable"
                enable = complete
            },
            onResult = results::add,
        )

        fun completeEnable(status: String) = requireNotNull(enable)(status)

        fun completeCompensation(status: String) = requireNotNull(compensation)(status)
    }

    @Test
    fun `late SDK enable success after cancel is compensated before another enable`() {
        val h = Harness()
        val old = AuthorityOperation(1, 1)
        val next = AuthorityOperation(1, 2)
        val oldResults = mutableListOf<String>()

        assertTrue(h.start(old, oldResults))
        h.fence.abandon(old)
        h.fence.cleanupIssued(old)
        h.calls += "cancel_disable"
        h.fence.cleanupCompleted(old)
        h.calls += "cancel_disable_complete"
        h.completeEnable("ok")

        assertEquals(listOf("enable", "cancel_disable", "cancel_disable_complete", "compensating_disable"), h.calls)
        assertFalse(h.start(next, mutableListOf()))
        h.completeCompensation("ok")
        assertTrue(h.fence.isAvailable())
        assertTrue(h.start(next, mutableListOf()))
        assertTrue(oldResults.isEmpty())
        assertTrue(h.records.any { it.second == "late_enable_compensation_issued" })
    }

    @Test
    fun `old SDK enable completion after product change cannot compensate the replacement`() {
        val h = Harness()
        val old = AuthorityOperation(1, 1)
        val replacement = AuthorityOperation(2, 1)

        assertTrue(h.start(old, mutableListOf()))
        h.fence.abandon(old)
        h.fence.cleanupIssued(old)
        h.fence.cleanupCompleted(old)
        h.fence.productChanged()
        h.completeEnable("ok")

        assertEquals(listOf("enable"), h.calls)
        assertTrue(h.fence.isAvailable())
        assertTrue(h.start(replacement, mutableListOf()))
    }

    @Test
    fun `failed late-enable compensation blocks further enables until a later disable succeeds`() {
        val h = Harness()
        val old = AuthorityOperation(1, 1)
        val next = AuthorityOperation(1, 2)

        assertTrue(h.start(old, mutableListOf()))
        h.fence.abandon(old)
        h.fence.cleanupIssued(old)
        h.fence.cleanupCompleted(old)
        h.completeEnable("ok")
        h.completeCompensation("SDK_ERROR")

        assertFalse(h.start(next, mutableListOf()))
        assertTrue(h.fence.unavailableDetail().contains("reconnect the aircraft"))
        h.fence.cleanupIssued(next)
        h.fence.cleanupCompleted(next)
        assertFalse(h.start(next, mutableListOf()))
        h.fence.productChanged()
        assertTrue(h.start(next, mutableListOf()))
    }

    @Test
    fun `another cleanup completion cannot clear a pending late-enable compensation`() {
        val h = Harness()
        val oldEnable = AuthorityOperation(1, 1)
        val firstDisable = AuthorityOperation(1, 2)
        val secondDisable = AuthorityOperation(1, 3)
        val nextEnable = AuthorityOperation(1, 4)

        assertTrue(h.start(oldEnable, mutableListOf()))
        h.fence.abandon(oldEnable)
        h.fence.cleanupIssued(firstDisable)
        h.fence.cleanupIssued(secondDisable)
        h.fence.cleanupCompleted(firstDisable)
        h.completeEnable("ok")
        h.fence.cleanupCompleted(secondDisable)

        assertFalse(h.start(nextEnable, mutableListOf()))
        h.completeCompensation("ok")
        assertTrue(h.start(nextEnable, mutableListOf()))
    }
}

class ProductGenerationFenceTest {
    @Test
    fun `product transition waits for a reserved compensation dispatch`() {
        val product = ProductGenerationFence()
        val old = AuthorityOperation(1, 1)
        val entered = CountDownLatch(1)
        val transitioned = CountDownLatch(1)
        product.completeTransition(old.productGeneration)
        assertTrue(product.reserveIssuance(old))

        Thread {
            entered.countDown()
            product.beginTransition()
            transitioned.countDown()
        }.start()

        assertTrue(entered.await(1, TimeUnit.SECONDS))
        assertFalse(transitioned.await(100, TimeUnit.MILLISECONDS))
        product.releaseIssuance()
        assertTrue(transitioned.await(1, TimeUnit.SECONDS))
    }

    @Test
    fun `synchronous old enable completion is quarantined while the product handoff is in progress`() {
        val product = ProductGenerationFence()
        product.completeTransition(1)
        val calls = mutableListOf<String>()
        var lateEnable: ((String) -> Unit)? = null
        val enableFence = VirtualStickEnableFence(
            issueCompensatingDisable = { operation, _ ->
                if (product.reserveIssuance(operation)) {
                    calls += "compensating_disable"
                    product.releaseIssuance()
                } else {
                    calls += "product_generation_changed"
                }
            },
            record = { _, _, _ -> },
        )
        val old = AuthorityOperation(1, 1)

        assertTrue(enableFence.start(old, { lateEnable = it }, {}))
        product.beginTransition()
        enableFence.abandon(old)
        requireNotNull(lateEnable)("ok")

        assertEquals(listOf("product_generation_changed"), calls)
        enableFence.productChanged()
        product.completeTransition(2)
        assertTrue(product.reserveIssuance(AuthorityOperation(2, 1)))
        product.releaseIssuance()
        assertFalse(product.reserveIssuance(old))
    }
}
