package org.worldofhacks.sweep.bridge.publish

/** Test flavor only. Synthetic pictures never enter the platform's live media paths. */
class FakePublishSources : PublishSourceFactory {
    override val available: List<PublishSource> = listOf(PublishSource.TEST_PATTERN)

    override fun open(source: PublishSource, droneId: Int, listener: PublishSourceListener): OpenSource {
        throw SourceUnavailableException("Synthetic video publishing is disabled. Connect a real aircraft camera using the probe app.")
    }
}
