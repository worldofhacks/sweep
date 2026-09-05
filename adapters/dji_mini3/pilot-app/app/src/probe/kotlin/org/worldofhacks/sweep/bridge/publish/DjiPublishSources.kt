package org.worldofhacks.sweep.bridge.publish

import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.SDKManager
import org.worldofhacks.sweep.bridge.publish.webrtc.PassthroughCapturer
import org.worldofhacks.sweep.bridge.publish.webrtc.PassthroughStats
import org.worldofhacks.sweep.bridge.publish.webrtc.PassthroughVideoEncoderFactory
import org.worldofhacks.sweep.bridge.publish.webrtc.SharedDJIFrameSource
import org.worldofhacks.sweep.bridge.publish.webrtc.SharedVideoCapturerHandle

/**
 * Probe flavor: the SDK's encoded frames straight to the packetizer (default), or WildBridge's
 * decoded-frame path re-encoded on the phone when the pilot selects it explicitly.
 */
class DjiPublishSources(private val log: (String) -> Unit) : PublishSourceFactory {
    override val available: List<PublishSource> = listOf(PublishSource.PASSTHROUGH, PublishSource.REENCODE)

    override fun open(source: PublishSource, droneId: Int, listener: PublishSourceListener): OpenSource {
        if (!SDKManager.getInstance().isRegistered) throw SourceUnavailableException("DJI SDK is not registered")
        return when (source) {
            PublishSource.PASSTHROUGH -> openPassthrough(listener)
            PublishSource.REENCODE -> openReencode(listener)
            PublishSource.TEST_PATTERN -> throw SourceUnavailableException("the probe flavor has no test pattern")
        }
    }

    private fun openPassthrough(listener: PublishSourceListener): OpenSource {
        val stats = PassthroughStats()
        val capturer = PassthroughCapturer(stats)
        val frames = DjiEncodedFrameSource(ComponentIndexType.LEFT_OR_MAIN, listener, log)
        frames.sink = DjiEncodedFrameSource.Sink { unit -> capturer.onAccessUnit(unit) }
        frames.start()
        return OpenSource(
            source = PublishSource.PASSTHROUGH,
            capturer = capturer,
            encoderFactory = PassthroughVideoEncoderFactory(stats, log),
            targetBitrateBps = PASSTHROUGH_FLOOR_BPS,
            maxBitrateBps = PASSTHROUGH_MAX_BPS,
            codecLabel = null,
            extraDropped = { stats.dropped() + stats.skippedForKeyframe.get() },
            onClose = {
                frames.stop()
                capturer.dispose()
            },
        )
    }

    private fun openReencode(listener: PublishSourceListener): OpenSource {
        val shared = SharedDJIFrameSource(ComponentIndexType.LEFT_OR_MAIN, log)
        shared.metricsListener = { listener.onSourceMetrics(it) }
        val capturer = SharedVideoCapturerHandle("whip", shared)
        return OpenSource(
            source = PublishSource.REENCODE,
            capturer = capturer,
            encoderFactory = null,
            targetBitrateBps = REENCODE_TARGET_BPS,
            maxBitrateBps = REENCODE_MAX_BPS,
            codecLabel = null,
            extraDropped = null,
            onClose = {
                capturer.dispose()
                shared.dispose()
            },
        )
    }

    private companion object {
        // The Mini 3 live view is 720p/30 at up to 8 Mbps; the floor keeps the pacer above the
        // aircraft's rate so pre-encoded frames are never queued, the ceiling bounds bursts.
        const val PASSTHROUGH_FLOOR_BPS = 6_000_000
        const val PASSTHROUGH_MAX_BPS = 14_000_000
        const val REENCODE_TARGET_BPS = 4_000_000
        const val REENCODE_MAX_BPS = 6_000_000
    }
}
