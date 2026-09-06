package org.worldofhacks.sweep.bridge.publish

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.admission.Clock
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState

private class StepClock(private var nowMs: Long) : Clock {
    override fun nowMs(): Long = nowMs

    fun advance(deltaMs: Long) {
        nowMs += deltaMs
    }
}

class PublishStateMachineTest {
    private val url = "http://10.10.1.60:8889/drone1/whip"

    @Test
    fun `stopped connecting publishing is the happy path`() {
        val clock = StepClock(1_000)
        val machine = PublishStateMachine(clock = clock)
        assertEquals(VideoPublishState.STOPPED, machine.current.state)
        val connecting = machine.start(PublishSource.PASSTHROUGH, url)
        assertEquals(VideoPublishState.CONNECTING, connecting.state)
        assertEquals(1, connecting.attempts)
        assertEquals(url, connecting.whipUrl)
        assertEquals(PublishSource.PASSTHROUGH, connecting.source)
        machine.offerAccepted("http://10.10.1.60:8889/drone1/whip/abc")
        assertEquals("http://10.10.1.60:8889/drone1/whip/abc", machine.current.resourceUrl)
        clock.advance(250)
        val publishing = machine.publishing("H264 High 4.0 passthrough")
        assertEquals(VideoPublishState.PUBLISHING, publishing.state)
        assertEquals(1_250L, publishing.publishingSinceMs)
        assertEquals("H264 High 4.0 passthrough", publishing.codec)
        assertNull(publishing.reason)
        assertEquals(0, publishing.consecutiveFailures)
    }

    @Test
    fun `an ice loss fails with the reason and schedules a bounded backoff`() {
        val clock = StepClock(0)
        val machine = PublishStateMachine(PublishBackoff(initialMs = 1_000, maxMs = 8_000), clock)
        machine.start(PublishSource.PASSTHROUGH, url)
        machine.publishing()
        val delays = ArrayList<Long>()
        repeat(6) {
            val delay = machine.failed(PublishReasons.ICE_DISCONNECTED, "ICE state DISCONNECTED")
            delays += requireNotNull(delay)
            val failed = machine.current
            assertEquals(VideoPublishState.FAILED, failed.state)
            assertEquals(PublishReasons.ICE_DISCONNECTED, failed.reason)
            assertEquals("ICE state DISCONNECTED", failed.detail)
            assertEquals(clock.nowMs() + delay, failed.nextAttemptAtMs)
            assertTrue(failed.retryPending)
            clock.advance(delay)
            assertEquals(VideoPublishState.CONNECTING, machine.attempting().state)
        }
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L, 8_000L, 8_000L), delays)
        assertEquals(7, machine.current.attempts)
        machine.publishing()
        assertEquals(0, machine.current.consecutiveFailures)
        assertEquals(1_000L, machine.failed(PublishReasons.HTTP_ERROR, "HTTP 500"))
    }

    @Test
    fun `codec_unsupported is terminal - failed with the reason and no retry`() {
        val machine = PublishStateMachine(clock = StepClock(5))
        machine.start(PublishSource.PASSTHROUGH, url)
        val delay = machine.failed(PublishReasons.CODEC_UNSUPPORTED, "aircraft emits H.265 (H265 Main 3.1)")
        assertNull(delay)
        val failed = machine.current
        assertEquals(VideoPublishState.FAILED, failed.state)
        assertEquals(PublishReasons.CODEC_UNSUPPORTED, failed.reason)
        assertTrue(failed.detail!!.contains("H.265"))
        assertNull(failed.nextAttemptAtMs)
        assertFalse(failed.retryPending)
        // attempting without a scheduled retry is ignored; a fresh start (new source) is allowed.
        assertEquals(VideoPublishState.FAILED, machine.attempting().state)
        assertEquals(VideoPublishState.CONNECTING, machine.start(PublishSource.REENCODE, url).state)
        assertEquals(PublishSource.REENCODE, machine.current.source)
        assertEquals(1, machine.current.attempts)
    }

    @Test
    fun `stop from any state returns to stopped and keeps the codec evidence`() {
        val machine = PublishStateMachine(clock = StepClock(0))
        machine.start(PublishSource.TEST_PATTERN, url)
        machine.publishing("VP8")
        assertEquals(VideoPublishState.STOPPED, machine.stop().state)
        assertNull(machine.current.reason)
        assertNull(machine.current.resourceUrl)
        assertEquals("VP8", machine.current.codec)
        machine.start(PublishSource.TEST_PATTERN, url)
        machine.failed(PublishReasons.NETWORK_ERROR, "connection refused")
        assertEquals(VideoPublishState.STOPPED, machine.stop().state)
        assertNull(machine.current.nextAttemptAtMs)
    }

    @Test
    fun `late callbacks after stop or start cannot change the state`() {
        val machine = PublishStateMachine(clock = StepClock(0))
        assertNull(machine.failed(PublishReasons.ICE_FAILED, "late"))
        assertEquals(VideoPublishState.STOPPED, machine.current.state)
        assertEquals(VideoPublishState.STOPPED, machine.publishing().state)
        assertEquals(VideoPublishState.STOPPED, machine.offerAccepted("x").state)
        machine.start(PublishSource.PASSTHROUGH, url)
        assertEquals(1, machine.start(PublishSource.PASSTHROUGH, url).attempts)
        machine.publishing()
        assertEquals(VideoPublishState.PUBLISHING, machine.start(PublishSource.PASSTHROUGH, url).state)
        assertNull(machine.publishing().reason)
    }

    @Test
    fun `backoff doubles from the initial delay and caps at the maximum`() {
        val backoff = PublishBackoff(initialMs = 500, maxMs = 5_000)
        assertEquals(500L, backoff.delayMs(0))
        assertEquals(500L, backoff.delayMs(1))
        assertEquals(1_000L, backoff.delayMs(2))
        assertEquals(4_000L, backoff.delayMs(4))
        assertEquals(5_000L, backoff.delayMs(5))
        assertEquals(5_000L, backoff.delayMs(60))
    }
}
