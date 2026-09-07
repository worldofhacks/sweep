package org.worldofhacks.sweep.bridge

import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.publish.FakePublishSources
import org.worldofhacks.sweep.bridge.publish.PublishSource
import org.worldofhacks.sweep.bridge.publish.PublishSourceListener
import org.worldofhacks.sweep.bridge.publish.SourceUnavailableException
import org.worldofhacks.sweep.bridge.publish.codec.CodecDecision
import org.worldofhacks.sweep.bridge.publish.codec.CodecEvidence
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics

class SyntheticIsolationTest {
    @Test
    fun `synthetic adapters always declare test provenance`() {
        assertTrue("test:synthetic" in AircraftVariant.capabilities)
    }

    @Test
    fun `generated video cannot open a live publish source`() {
        val listener = object : PublishSourceListener {
            override fun onCodecEvidence(evidence: CodecEvidence, decision: CodecDecision) = Unit
            override fun onSourceMetrics(metrics: WebRTCStreamMetrics) = Unit
            override fun onSourceFailure(reason: String, detail: String) = Unit
        }
        assertThrows(SourceUnavailableException::class.java) {
            FakePublishSources().open(PublishSource.TEST_PATTERN, 1, listener)
        }
    }
}
