package org.worldofhacks.sweep.bridge

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class AltitudeHardwarePollerTest {
    @Test
    fun `successful zero height queries refresh their receipt at the polling cadence`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.query.succeed(0, 0.0)
        fixture.scheduler.advance(100)
        fixture.query.succeed(1, 0.0)

        assertEquals(listOf(0.0 to 0L, 0.0 to 100L), fixture.samples)
    }

    @Test
    fun `a query with no response times out without issuing an overlapping request`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.scheduler.advance(2_000)

        assertEquals(1, fixture.query.callbacks.size)
        assertTrue(fixture.samples.isEmpty())
    }

    @Test
    fun `failed and nonfinite responses do not create fresh height receipts`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.query.fail(0)
        fixture.scheduler.advance(100)
        fixture.query.succeed(1, Double.NaN)

        assertTrue(fixture.samples.isEmpty())
        fixture.scheduler.advance(100)
        assertEquals(3, fixture.query.callbacks.size)
    }

    @Test
    fun `a callback that arrives after its monotonic deadline is rejected even before the timeout task runs`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.scheduler.moveWithoutRunning(501)
        fixture.query.succeed(0, 2.0)

        assertTrue(fixture.samples.isEmpty())
    }

    @Test
    fun `a synchronous query exception is treated as a failed bounded attempt`() {
        val scheduler = Scheduler()
        var attempts = 0
        val poller = AltitudeHardwarePoller(
            scheduler = scheduler,
            query = AltitudeHardwareQuery {
                attempts += 1
                throw IllegalStateException("SDK unavailable")
            },
            monotonicNowMs = scheduler::nowMs,
            onSample = { _, _ -> throw AssertionError("a failed query cannot publish a sample") },
        )
        poller.attach()
        poller.connectionChanged(true)

        scheduler.runDue()
        scheduler.advance(100)

        assertEquals(2, attempts)
    }

    @Test
    fun `disconnect detach and replacement connection reject delayed query responses`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.poller.connectionChanged(false)
        fixture.query.succeed(0, 1.0)
        fixture.poller.connectionChanged(true)
        fixture.scheduler.advance(100)
        fixture.poller.detach()
        fixture.query.succeed(1, 2.0)
        fixture.poller.attach()
        fixture.poller.connectionChanged(true)
        fixture.scheduler.advance(100)
        fixture.poller.connectionChanged(true)
        fixture.query.succeed(2, 3.0)

        assertTrue(fixture.samples.isEmpty())
    }

    @Test
    fun `a listener receipt prevents an older hardware query overwriting it`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.poller.listenerSampleReceived()
        fixture.query.succeed(0, 9.0)
        fixture.scheduler.advance(100)
        fixture.query.succeed(1, 4.0)

        assertEquals(listOf(4.0 to 100L), fixture.samples)
    }

    @Test
    fun `sample delivery holds the shared aircraft state guard`() {
        val fixture = Fixture()
        fixture.attachAndConnect()

        fixture.scheduler.runDue()
        fixture.query.succeed(0, 3.0)

        assertEquals(listOf(true), fixture.sampleGuardHeld)
    }

    @Test
    fun `a listener receipt cannot interleave between query validation and height publication`() {
        val scheduler = Scheduler()
        val query = Query()
        val aircraftStateGuard = Any()
        val queryPublishing = CountDownLatch(1)
        val releaseQuery = CountDownLatch(1)
        val listenerDone = CountDownLatch(1)
        var height = 0.0
        val poller = AltitudeHardwarePoller(
            scheduler = scheduler,
            query = query,
            monotonicNowMs = scheduler::nowMs,
            onSample = { value, _ ->
                queryPublishing.countDown()
                assertTrue(releaseQuery.await(2, TimeUnit.SECONDS))
                height = value
            },
            guard = aircraftStateGuard,
        )
        poller.attach()
        poller.connectionChanged(true)
        scheduler.runDue()

        val response = Thread { query.succeed(0, 1.0) }.apply { start() }
        assertTrue(queryPublishing.await(2, TimeUnit.SECONDS))
        val listener = Thread {
            synchronized(aircraftStateGuard) {
                poller.listenerSampleReceived()
                height = 2.0
            }
            listenerDone.countDown()
        }.apply { start() }

        assertFalse(listenerDone.await(100, TimeUnit.MILLISECONDS))
        releaseQuery.countDown()
        response.join(2_000)
        listener.join(2_000)
        assertEquals(2.0, height)
    }

    private class Fixture {
        val scheduler = Scheduler()
        val query = Query()
        val samples = mutableListOf<Pair<Double, Long>>()
        val sampleGuardHeld = mutableListOf<Boolean>()
        private val aircraftStateGuard = Any()
        val poller = AltitudeHardwarePoller(
            scheduler = scheduler,
            query = query,
            monotonicNowMs = scheduler::nowMs,
            onSample = { value, receivedAtMs ->
                sampleGuardHeld += Thread.holdsLock(aircraftStateGuard)
                samples += value to receivedAtMs
            },
            guard = aircraftStateGuard,
        )

        fun attachAndConnect() {
            poller.attach()
            poller.connectionChanged(true)
        }
    }

    private class Query : AltitudeHardwareQuery {
        val callbacks = mutableListOf<AltitudeHardwareQueryCallback>()

        override fun request(callback: AltitudeHardwareQueryCallback) {
            callbacks += callback
        }

        fun succeed(index: Int, value: Double) = callbacks[index].onSuccess(value)

        fun fail(index: Int) = callbacks[index].onFailure()
    }

    private class Scheduler : AltitudePollScheduler {
        private data class Task(val atMs: Long, val action: () -> Unit)

        private val tasks = mutableListOf<Task>()
        private var now = 0L

        override fun after(delayMs: Long, action: () -> Unit) {
            tasks += Task(now + delayMs, action)
        }

        fun nowMs(): Long = now

        fun runDue() {
            while (true) {
                val next = tasks.withIndex().filter { it.value.atMs <= now }.minByOrNull { it.value.atMs } ?: return
                tasks.removeAt(next.index)
                next.value.action()
            }
        }

        fun advance(milliseconds: Long) {
            now += milliseconds
            runDue()
        }

        fun moveWithoutRunning(milliseconds: Long) {
            now += milliseconds
        }
    }
}
