package org.worldofhacks.sweep.bridge.publish.webrtc

import android.content.Context
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import org.webrtc.CapturerObserver
import org.webrtc.EncodedImage
import org.webrtc.JavaI420Buffer
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoCodecInfo
import org.webrtc.VideoCodecStatus
import org.webrtc.VideoEncoder
import org.webrtc.VideoEncoderFactory
import org.webrtc.VideoFrame

/**
 * One encoded access unit from the aircraft: Annex B bytes with the parameter sets present
 * on keyframes, the SDK's frame flags, and the arrival time on the phone's monotonic clock.
 */
class EncodedUnit(
    val data: ByteArray,
    val keyframe: Boolean,
    val width: Int,
    val height: Int,
    val timestampNs: Long,
)

/**
 * The encoded-frame passthrough: libwebrtc's Android API has no injection point for
 * pre-encoded frames, so the capturer pushes one placeholder I420 frame per access unit whose
 * [VideoFrame.Buffer] carries the unit ([UnitBuffer]), and the "encoder" hands that unit to the
 * H.264 packetizer as an [EncodedImage] instead of encoding the placeholder. libwebrtc matches
 * the emitted image to the input frame by capture timestamp, which is why the image takes the
 * frame's own timestamp rather than the unit's arrival time (the source aligns timestamps).
 *
 * Keyframes: the packetizer's first frame and every keyframe request (PLI from MediaMTX) must
 * be a keyframe; the SDK cannot be asked for one, so delta units are skipped until the next
 * keyframe arrives, and the skip count is reported as dropped frames.
 */
class PassthroughStats {
    val emitted = AtomicLong()
    val encoded = AtomicLong()
    val skippedForKeyframe = AtomicLong()
    val unmatched = AtomicLong()

    /** Units that left the capturer but never reached the packetizer. */
    fun dropped(): Long = (emitted.get() - encoded.get()).coerceAtLeast(0)
}

/** An I420 placeholder whose identity survives libwebrtc's identity crop, carrying the encoded unit. */
class UnitBuffer(val unit: EncodedUnit, private val inner: VideoFrame.Buffer) : VideoFrame.Buffer {
    override fun getWidth(): Int = inner.width

    override fun getHeight(): Int = inner.height

    override fun toI420(): VideoFrame.I420Buffer? = inner.toI420()

    override fun retain() = inner.retain()

    override fun release() = inner.release()

    override fun cropAndScale(cropX: Int, cropY: Int, cropWidth: Int, cropHeight: Int, scaleWidth: Int, scaleHeight: Int): VideoFrame.Buffer =
        UnitBuffer(unit, inner.cropAndScale(cropX, cropY, cropWidth, cropHeight, scaleWidth, scaleHeight))
}

class PassthroughCapturer(private val stats: PassthroughStats) : VideoCapturer {
    private var observer: CapturerObserver? = null
    private val started = AtomicBoolean(false)
    private var placeholder: JavaI420Buffer? = null
    private var placeholderWidth = 0
    private var placeholderHeight = 0
    private var lastTimestampNs = 0L
    private val lock = Any()

    /** Called by the frame source on its own thread for every access unit that passed the codec gate. */
    fun onAccessUnit(unit: EncodedUnit) {
        if (!started.get()) return
        val target = observer ?: return
        synchronized(lock) {
            val buffer = placeholderFor(unit.width, unit.height) ?: return
            val timestampNs = if (unit.timestampNs <= lastTimestampNs) lastTimestampNs + 1_000 else unit.timestampNs
            lastTimestampNs = timestampNs
            buffer.retain()
            val frame = VideoFrame(UnitBuffer(unit, buffer), 0, timestampNs)
            stats.emitted.incrementAndGet()
            target.onFrameCaptured(frame)
            frame.release()
        }
    }

    override fun initialize(surfaceTextureHelper: SurfaceTextureHelper?, applicationContext: Context, capturerObserver: CapturerObserver) {
        observer = capturerObserver
    }

    override fun startCapture(width: Int, height: Int, framerate: Int) {
        if (started.compareAndSet(false, true)) observer?.onCapturerStarted(true)
    }

    override fun stopCapture() {
        if (started.compareAndSet(true, false)) observer?.onCapturerStopped()
    }

    override fun changeCaptureFormat(width: Int, height: Int, framerate: Int) = Unit

    override fun dispose() {
        stopCapture()
        synchronized(lock) {
            placeholder?.release()
            placeholder = null
        }
        observer = null
    }

    override fun isScreencast(): Boolean = false

    private fun placeholderFor(width: Int, height: Int): JavaI420Buffer? {
        if (width <= 0 || height <= 0) return null
        val current = placeholder
        if (current != null && placeholderWidth == width && placeholderHeight == height) return current
        current?.release()
        val allocated = JavaI420Buffer.allocate(width, height)
        placeholder = allocated
        placeholderWidth = width
        placeholderHeight = height
        return allocated
    }
}

class PassthroughVideoEncoderFactory(private val stats: PassthroughStats, private val log: (String) -> Unit) : VideoEncoderFactory {
    override fun getSupportedCodecs(): Array<VideoCodecInfo> = arrayOf(
        VideoCodecInfo(
            "H264",
            mapOf(
                VideoCodecInfo.H264_FMTP_PROFILE_LEVEL_ID to VideoCodecInfo.H264_CONSTRAINED_BASELINE_3_1,
                VideoCodecInfo.H264_FMTP_LEVEL_ASYMMETRY_ALLOWED to "1",
                VideoCodecInfo.H264_FMTP_PACKETIZATION_MODE to "1",
            ),
            emptyList(),
        ),
    )

    override fun createEncoder(info: VideoCodecInfo): VideoEncoder? =
        if (info.name.equals("H264", ignoreCase = true)) PassthroughVideoEncoder(stats, log) else null
}

class PassthroughVideoEncoder(private val stats: PassthroughStats, private val log: (String) -> Unit) : VideoEncoder {
    private var callback: VideoEncoder.Callback? = null

    @Volatile
    private var awaitingKeyframe = true

    @Volatile
    private var warnedUnmatched = false

    override fun initEncode(settings: VideoEncoder.Settings, encodeCallback: VideoEncoder.Callback): VideoCodecStatus {
        callback = encodeCallback
        awaitingKeyframe = true
        log("passthrough encoder initialised for ${settings.width}x${settings.height}; waiting for the next SDK keyframe")
        return VideoCodecStatus.OK
    }

    override fun release(): VideoCodecStatus {
        callback = null
        return VideoCodecStatus.OK
    }

    override fun encode(frame: VideoFrame, info: VideoEncoder.EncodeInfo): VideoCodecStatus {
        val unit = (frame.buffer as? UnitBuffer)?.unit
        if (unit == null) {
            stats.unmatched.incrementAndGet()
            if (!warnedUnmatched) {
                warnedUnmatched = true
                log("passthrough encoder received a frame without its access unit (${frame.buffer.javaClass.simpleName}); frames are being converted before encode")
            }
            return VideoCodecStatus.OK
        }
        if (info.frameTypes.any { it == EncodedImage.FrameType.VideoFrameKey }) awaitingKeyframe = true
        if (awaitingKeyframe && !unit.keyframe) {
            stats.skippedForKeyframe.incrementAndGet()
            return VideoCodecStatus.OK
        }
        awaitingKeyframe = false
        val target = callback ?: return VideoCodecStatus.UNINITIALIZED
        val buffer = ByteBuffer.allocateDirect(unit.data.size)
        buffer.put(unit.data)
        buffer.rewind()
        val image = EncodedImage.builder()
            .setBuffer(buffer, null)
            .setEncodedWidth(unit.width)
            .setEncodedHeight(unit.height)
            .setCaptureTimeNs(frame.timestampNs)
            .setFrameType(if (unit.keyframe) EncodedImage.FrameType.VideoFrameKey else EncodedImage.FrameType.VideoFrameDelta)
            .setRotation(0)
            .setQp(null)
            .createEncodedImage()
        target.onEncodedFrame(image, VideoEncoder.CodecSpecificInfo())
        stats.encoded.incrementAndGet()
        return VideoCodecStatus.OK
    }

    override fun setRateAllocation(allocation: VideoEncoder.BitrateAllocation?, framerate: Int): VideoCodecStatus = VideoCodecStatus.OK

    override fun getScalingSettings(): VideoEncoder.ScalingSettings = VideoEncoder.ScalingSettings.OFF

    override fun getImplementationName(): String = "SweepPassthroughH264"
}
