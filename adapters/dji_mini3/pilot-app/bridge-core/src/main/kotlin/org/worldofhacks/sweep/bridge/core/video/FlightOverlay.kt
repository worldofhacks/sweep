package org.worldofhacks.sweep.bridge.core.video

import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.roundToInt
import org.worldofhacks.sweep.bridge.core.frames.DeltaKind
import org.worldofhacks.sweep.bridge.core.frames.GuidanceMode
import org.worldofhacks.sweep.bridge.core.frames.SuggestedDelta

/** The capture pill on the flight display; the words are the design brief's, verbatim. */
enum class CaptureState(val label: String) {
    READY("Ready"),
    CAPTURING("Capturing"),
    DOWNLOADING("Downloading"),
    NEEDS_RETAKE("Needs retake"),
    DISCONNECTED("Disconnected"),
}

enum class SectorMark(val wire: String) {
    UNSEEN("unseen"),
    WEAK("weak"),
    ACCEPTED("accepted"),
}

/** Where a capture is, as Phase G will report it; Phase D only ever sees [Idle]. */
sealed interface CapturePhase {
    data object Idle : CapturePhase

    /** `done of total` steps, or a percentage when [percent] is set (panorama progress). */
    data class Capturing(val done: Int, val total: Int, val percent: Boolean = false) : CapturePhase

    data class Downloading(val file: Int, val of: Int) : CapturePhase

    data class NeedsRetake(val missingHeadingsDeg: List<Double>) : CapturePhase
}

/**
 * The capture path's contribution to the overlay. Headings are azimuths in degrees;
 * [nextHeadingDeg] and [gimbalDeltaDeg] come from `capture_readiness` once Phase G computes
 * them, and until then the overlay derives the next heading from the coverage marks itself.
 */
data class CaptureProgress(
    val phase: CapturePhase = CapturePhase.Idle,
    val acceptedHeadingsDeg: List<Double> = emptyList(),
    val weakHeadingsDeg: List<Double> = emptyList(),
    val nextHeadingDeg: Double? = null,
    val gimbalDeltaDeg: Double? = null,
)

/** Everything the session already knows that the overlay is drawn from. */
data class OverlayInputs(
    val aircraftConnected: Boolean,
    val rcConnected: Boolean,
    val relayConnected: Boolean,
    /** Watchdog wire word: `nominal`, `hold`, or `failsafe`. */
    val watchdog: String,
    val estop: Boolean,
    val controlAuthority: Boolean,
    val authorityChangeReason: String?,
    /** Aircraft yaw as the SDK reports it (degrees, north 0, clockwise positive), null when unknown. */
    val yawDeg: Double?,
    /** The measured horizontal field of view; the published lens value is never used here. */
    val measuredHfovDeg: Double?,
    /** Age of the last received video frame, null before the first one. */
    val lastFrameAgeMs: Long?,
    val capture: CaptureProgress = CaptureProgress(),
)

data class CompassSector(val index: Int, val startDeg: Double, val endDeg: Double, val mark: SectorMark) {
    val centerDeg: Double
        get() = (startDeg + endDeg) / 2.0

    fun contains(headingDeg: Double): Boolean {
        val h = FlightOverlay.heading(headingDeg)
        return h >= startDeg && h < endDeg
    }
}

data class OverlayState(
    val captureState: CaptureState,
    val guidanceMode: GuidanceMode,
    val poseSource: String,
    /** Current heading in [0, 360), null when yaw is unknown. */
    val headingDeg: Double?,
    val sectors: List<CompassSector>,
    val sectorWidthDeg: Double,
    /** True while the sectors come from the reconstruct_8 headings instead of a measured field of view. */
    val sectorsProvisional: Boolean,
    val nextHeadingDeg: Double?,
    val suggestedDelta: SuggestedDelta?,
    /** "yaw +12°" or "gimbal −15°"; arrows mean yaw or gimbal only in visual_advisory. */
    val deltaLabel: String?,
    val progressLabel: String?,
    /** "Sweep" or "RC", with the change reason when the SDK gave one. */
    val authorityLabel: String,
    val videoLabel: String,
    /** Degraded states, most urgent first, one sentence each. */
    val degraded: List<String>,
) {
    val clearanceLabel: String
        get() = CLEARANCE_LABEL

    val rcPrimaryNote: String
        get() = RC_PRIMARY_NOTE

    companion object {
        const val RC_PRIMARY_NOTE = "Physical RC remains primary"
        const val CLEARANCE_LABEL = "clearance: unverified"
        const val POSE_SOURCE = "aircraft_telemetry"
    }
}

/**
 * Derives the flight display overlay from session facts. Pure: the same inputs always give
 * the same state, so the five capture states, the compass, and the degraded sentences are
 * unit-tested without Android.
 */
object FlightOverlay {
    /** reconstruct_8 headings until the horizontal field of view has been measured on this aircraft. */
    const val PROVISIONAL_SECTOR_WIDTH_DEG = 45.0
    const val STALE_VIDEO_MS = 1_000L
    private const val MINUS = "−"

    fun derive(inputs: OverlayInputs): OverlayState {
        val heading = inputs.yawDeg?.let(::heading)
        val (sectors, width, provisional) = buildSectors(inputs.measuredHfovDeg, inputs.capture)
        val next = nextHeading(inputs.capture.nextHeadingDeg, sectors, heading)
        val delta = suggestedDelta(inputs.capture.gimbalDeltaDeg, heading, next)
        return OverlayState(
            captureState = captureState(inputs),
            guidanceMode = GuidanceMode.VISUAL_ADVISORY,
            poseSource = OverlayState.POSE_SOURCE,
            headingDeg = heading,
            sectors = sectors,
            sectorWidthDeg = width,
            sectorsProvisional = provisional,
            nextHeadingDeg = next,
            suggestedDelta = delta,
            deltaLabel = delta?.let(::deltaLabel),
            progressLabel = progressLabel(inputs.capture.phase),
            authorityLabel = authorityLabel(inputs),
            videoLabel = videoLabel(inputs.lastFrameAgeMs),
            degraded = degraded(inputs),
        )
    }

    /** Normalizes any angle into [0, 360). */
    fun heading(deg: Double): Double {
        val wrapped = deg % 360.0
        return if (wrapped < 0) wrapped + 360.0 else wrapped
    }

    /** Signed shortest rotation from [fromDeg] to [toDeg], in (-180, 180]. */
    fun wrapDelta(fromDeg: Double, toDeg: Double): Double {
        var delta = heading(toDeg) - heading(fromDeg)
        if (delta > 180.0) delta -= 360.0
        if (delta <= -180.0) delta += 360.0
        return delta
    }

    fun captureState(inputs: OverlayInputs): CaptureState = when {
        !inputs.aircraftConnected || !inputs.rcConnected -> CaptureState.DISCONNECTED
        inputs.capture.phase is CapturePhase.Capturing -> CaptureState.CAPTURING
        inputs.capture.phase is CapturePhase.Downloading -> CaptureState.DOWNLOADING
        inputs.capture.phase is CapturePhase.NeedsRetake -> CaptureState.NEEDS_RETAKE
        else -> CaptureState.READY
    }

    private fun buildSectors(measuredHfovDeg: Double?, capture: CaptureProgress): Triple<List<CompassSector>, Double, Boolean> {
        val measured = measuredHfovDeg?.takeIf { it.isFinite() && it > 0.0 && it < 180.0 }
        val count = if (measured == null) (360.0 / PROVISIONAL_SECTOR_WIDTH_DEG).toInt() else ceil(360.0 / measured).toInt()
        val width = 360.0 / count
        val sectors = (0 until count).map { index ->
            val start = index * width
            val end = start + width
            val bare = CompassSector(index, start, end, SectorMark.UNSEEN)
            val mark = when {
                capture.acceptedHeadingsDeg.any(bare::contains) -> SectorMark.ACCEPTED
                capture.weakHeadingsDeg.any(bare::contains) -> SectorMark.WEAK
                else -> SectorMark.UNSEEN
            }
            bare.copy(mark = mark)
        }
        return Triple(sectors, width, measured == null)
    }

    private fun nextHeading(explicit: Double?, sectors: List<CompassSector>, heading: Double?): Double? {
        if (explicit != null) return heading(explicit)
        val open = sectors.filter { it.mark != SectorMark.ACCEPTED }
        if (open.isEmpty()) return null
        if (heading == null) return open.first().centerDeg
        return open.minByOrNull { abs(wrapDelta(heading, it.centerDeg)) }?.centerDeg
    }

    private fun suggestedDelta(gimbalDeltaDeg: Double?, heading: Double?, next: Double?): SuggestedDelta? = when {
        gimbalDeltaDeg != null -> SuggestedDelta(DeltaKind.GIMBAL, gimbalDeltaDeg)
        heading != null && next != null -> SuggestedDelta(DeltaKind.YAW, wrapDelta(heading, next))
        else -> null
    }

    fun deltaLabel(delta: SuggestedDelta): String {
        val whole = delta.degrees.roundToInt()
        val sign = if (whole < 0) MINUS else "+"
        return "${delta.kind.wire} $sign${abs(whole)}°"
    }

    private fun progressLabel(phase: CapturePhase): String? = when (phase) {
        CapturePhase.Idle -> null
        is CapturePhase.Capturing -> if (phase.percent) "${phase.done}%" else "${phase.done} of ${phase.total}"
        is CapturePhase.Downloading -> "file ${phase.file} of ${phase.of}"
        is CapturePhase.NeedsRetake -> when {
            phase.missingHeadingsDeg.isEmpty() -> "missing coverage"
            else -> "missing " + phase.missingHeadingsDeg.joinToString { "${heading(it).roundToInt()}°" }
        }
    }

    private fun authorityLabel(inputs: OverlayInputs): String {
        val word = if (inputs.controlAuthority) "Sweep" else "RC"
        val reason = inputs.authorityChangeReason
        return if (reason == null) word else "$word ($reason)"
    }

    private fun videoLabel(lastFrameAgeMs: Long?): String = when {
        lastFrameAgeMs == null -> "No video yet"
        lastFrameAgeMs > STALE_VIDEO_MS -> "No video for ${lastFrameAgeMs / 1000} s"
        else -> "Video live"
    }

    private fun degraded(inputs: OverlayInputs): List<String> = buildList {
        if (inputs.estop) add("Network stop active: neutral sticks and hover, then land if the stop is held")
        when (inputs.watchdog) {
            "failsafe" -> add("Watchdog failsafe: land indoors, never return home")
            "hold" -> add("Watchdog hold: neutral sticks and hover")
        }
        if (!inputs.aircraftConnected) add("Aircraft disconnected")
        if (!inputs.rcConnected) add("RC disconnected")
        if (!inputs.relayConnected) add("Relay disconnected")
        val age = inputs.lastFrameAgeMs
        if (age == null) add("No video yet") else if (age > STALE_VIDEO_MS) add("No video for ${age / 1000} s")
    }
}
