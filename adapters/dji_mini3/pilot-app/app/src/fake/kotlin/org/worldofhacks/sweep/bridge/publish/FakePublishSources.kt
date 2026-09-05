package org.worldofhacks.sweep.bridge.publish

/** Fake flavor: the generated test pattern through the phone's own encoder, no aircraft needed. */
class FakePublishSources : PublishSourceFactory {
    override val available: List<PublishSource> = listOf(PublishSource.TEST_PATTERN)

    override fun open(source: PublishSource, droneId: Int, listener: PublishSourceListener): OpenSource {
        if (source != PublishSource.TEST_PATTERN) throw SourceUnavailableException("the fake flavor only offers the test pattern")
        val capturer = TestPatternCapturer(droneId) { listener.onSourceMetrics(it) }
        return OpenSource(
            source = source,
            capturer = capturer,
            encoderFactory = null,
            targetBitrateBps = TARGET_BITRATE_BPS,
            maxBitrateBps = MAX_BITRATE_BPS,
            codecLabel = null,
            extraDropped = null,
            onClose = { capturer.dispose() },
        )
    }

    private companion object {
        const val TARGET_BITRATE_BPS = 2_500_000
        const val MAX_BITRATE_BPS = 4_000_000
    }
}
