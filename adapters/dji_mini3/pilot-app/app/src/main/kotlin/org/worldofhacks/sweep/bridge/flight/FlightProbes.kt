package org.worldofhacks.sweep.bridge.flight

import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.worldofhacks.sweep.bridge.bench.BenchRecorder
import org.worldofhacks.sweep.bridge.core.admission.SystemClock
import org.worldofhacks.sweep.bridge.core.flight.AxisProbe
import org.worldofhacks.sweep.bridge.core.flight.FlightReason
import org.worldofhacks.sweep.bridge.core.flight.FlightStatus
import org.worldofhacks.sweep.bridge.core.flight.GroundFrame
import org.worldofhacks.sweep.bridge.core.flight.ReportSink
import org.worldofhacks.sweep.bridge.core.flight.StickFrame
import org.worldofhacks.sweep.bridge.node.AircraftSource

/** What the probes card shows. */
data class ProbesState(
    val running: String? = null,
    val logPath: String? = null,
    val lastResult: String? = null,
    val transitions: List<String> = emptyList(),
    val pendingSignOff: String? = null,
    val signedOff: Int = 0,
    val error: String? = null,
)

/**
 * The issue #85 first-flight procedures, run from the screen with the RC operator present,
 * each written as signed-off entries through [BenchRecorder] into one JSONL file per session:
 *
 * - Axis-transpose probe: a pure `pitch` frame, then a pure `roll` frame, held in BODY frame
 *   at guarded hover while the body-frame velocity from telemetry is sampled; the result says
 *   which axis moved and whether it agrees with the mapping the loop uses. The firmware and
 *   MSDK versions are recorded with every entry.
 * - Hover drills (deadman, RC takeover, relay-link kill): neutral sticks held under Virtual
 *   Stick while the operator kills the relay link, moves a stick, or pulls the LAN; the drill
 *   records the measured stick rate and every loop transition (hold, failsafe, landing,
 *   authority lost) with timestamps until the loop is idle and the aircraft is on the ground.
 */
class FlightProbes(
    private val executor: FlightExecutor,
    private val aircraft: AircraftSource,
    private val directory: File,
    private val log: (String) -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val lock = Any()
    private var recorder: BenchRecorder? = null
    private var writer: FileWriter? = null
    private var job: Job? = null
    private val stickTimes = ArrayList<Long>()

    private val _state = MutableStateFlow(ProbesState())
    val state: StateFlow<ProbesState> = _state.asStateFlow()

    fun axisProbe(field: AxisProbe.Field, speedMS: Double = AXIS_PROBE_SPEED_MS, durationMs: Long = AXIS_PROBE_MS) = start("axis probe ${field.name.lowercase()}") {
        val frame = if (field == AxisProbe.Field.PITCH) StickFrame.NEUTRAL.copy(pitch = speedMS) else StickFrame.NEUTRAL.copy(roll = speedMS)
        val mapping = executor.status.value.mapping
        record("axis_probe_start", "pure ${field.name.lowercase()} ${format(speedMS)} m/s for $durationMs ms in BODY frame", "field" to field.name.lowercase(), "commanded_ms" to speedMS, "mapping_transposed" to mapping.transposed)
        val samples = ArrayList<AxisProbe.Sample>()
        val raw = ArrayList<Triple<Double, Double, Double>>()
        val outcome = holdWhile("axis-${field.name.lowercase()}", frame, durationMs) {
            val snapshot = aircraft.snapshot.value
            val (forward, right) = GroundFrame.toBody(snapshot.vx, snapshot.vy, snapshot.yawDeg)
            samples += AxisProbe.Sample(System.currentTimeMillis(), forward, right)
            raw += Triple(snapshot.vx, snapshot.vy, snapshot.yawDeg)
        }
        val result = AxisProbe.classify(field, speedMS, mapping, samples)
        val summary = result.summary() + " (bench ${outcome.first}${outcome.second?.let { ": $it" } ?: ""})"
        record(
            "axis_probe",
            summary,
            "field" to field.name.lowercase(),
            "commanded_ms" to speedMS,
            "observed_axis" to result.observedAxis.name.lowercase(),
            "observed_sign" to result.observedSign,
            "expected_axis" to result.expectedAxis.name.lowercase(),
            "agrees" to result.agrees,
            "suggests_transpose" to result.suggestsTranspose,
            "mean_forward_ms" to result.meanForwardMS,
            "mean_right_ms" to result.meanRightMS,
            "mean_vx_east_ms" to raw.map { it.first }.average().orZero(),
            "mean_vy_north_ms" to raw.map { it.second }.average().orZero(),
            "mean_yaw_deg" to raw.map { it.third }.average().orZero(),
            "samples" to result.samples,
            "mapping_transposed" to mapping.transposed,
            "outcome" to outcome.first,
            "outcome_reason" to outcome.second,
        )
        summary
    }

    fun hoverDrill(label: String, durationMs: Long = DRILL_MS) = start("hover drill $label") {
        val settings = executor.status.value.settings
        record("drill_start", "$label: neutral sticks under virtual stick for up to $durationMs ms", "label" to label, "stick_hz" to settings?.clampedStickHz, "hold_ms" to settings?.holdMs, "failsafe_ms" to settings?.failsafeMs)
        synchronized(lock) { stickTimes.clear() }
        executor.onStickSent = { seq, _, now ->
            synchronized(lock) {
                stickTimes += now
                recorder?.stickSent(seq)
            }
        }
        var last = executor.status.value
        val outcome = try {
            holdWhile(label, StickFrame.NEUTRAL, durationMs) {
                val current = executor.status.value
                if (current != last) {
                    noteTransitions(last, current)
                    last = current
                }
            }
        } finally {
            executor.onStickSent = null
        }
        // Keep watching until the loop is idle and the aircraft is on the ground or the window ends.
        withTimeoutOrNull(AFTERMATH_MS) {
            while (true) {
                val current = executor.status.value
                if (current != last) {
                    noteTransitions(last, current)
                    last = current
                }
                val snapshot = aircraft.snapshot.value
                if (current.phase == "idle" && snapshot.state !in FlightExecutor.FLYING_STATES) break
                delay(100)
            }
        }
        val rate = synchronized(lock) { measuredRate() }
        val summary = "$label ended ${outcome.first}${outcome.second?.let { ": $it" } ?: ""}; measured stick rate ${format(rate)} Hz over ${stickTimes.size} sends; ${_state.value.transitions.size} transitions"
        record("drill_end", summary, "label" to label, "outcome" to outcome.first, "outcome_reason" to outcome.second, "stick_rate_hz" to rate, "sticks_sent" to stickTimes.size, "transitions" to _state.value.transitions)
        summary
    }

    fun benchTakeoff(zMm: Long = 1_200) = start("bench takeoff") {
        val outcome = command { sink -> executor.benchTakeoff(zMm, sink) }
        val summary = "bench takeoff ${outcome.first}${outcome.second?.let { ": $it" } ?: ""}"
        record("bench_takeoff", summary, "z_mm" to zMm, "outcome" to outcome.first, "outcome_reason" to outcome.second)
        summary
    }

    fun benchLand() = start("bench land") {
        val outcome = command { sink -> executor.benchLand(sink) }
        val summary = "bench land ${outcome.first}${outcome.second?.let { ": $it" } ?: ""}"
        record("bench_land", summary, "outcome" to outcome.first, "outcome_reason" to outcome.second)
        summary
    }

    fun stop() = executor.stopBench()

    /** The operator's sign-off on the last result; the entry carries the versions the probe ran on. */
    fun signOff(operator: String, note: String) {
        val pending = _state.value.pendingSignOff ?: _state.value.lastResult ?: return
        record("sign_off", "$operator: $note", "operator" to operator, "note" to note, "result" to pending)
        _state.update { it.copy(pendingSignOff = null, signedOff = it.signedOff + 1) }
    }

    private fun start(name: String, block: suspend () -> String) {
        if (job?.isActive == true) {
            _state.update { it.copy(error = "${it.running} is still running") }
            return
        }
        _state.update { it.copy(running = name, error = null, transitions = emptyList()) }
        job = scope.launch {
            try {
                open()
                val summary = block()
                log("probe $name: $summary")
                _state.update { it.copy(running = null, lastResult = summary, pendingSignOff = summary) }
            } catch (error: Exception) {
                log("probe $name failed: $error")
                _state.update { it.copy(running = null, error = error.message ?: error.javaClass.simpleName) }
            }
        }
    }

    private suspend fun holdWhile(label: String, frame: StickFrame, durationMs: Long, sample: () -> Unit): Pair<String, String?> {
        val done = CompletableDeferred<Pair<String, String?>>()
        executor.startBench(label, frame, durationMs, sink(done))
        while (!done.isCompleted) {
            sample()
            delay(SAMPLE_MS)
        }
        return done.await()
    }

    private suspend fun command(issue: (ReportSink) -> Unit): Pair<String, String?> {
        val done = CompletableDeferred<Pair<String, String?>>()
        issue(sink(done))
        return withTimeoutOrNull(COMMAND_MS) { done.await() } ?: ("timeout" to "no terminal report within $COMMAND_MS ms")
    }

    private fun sink(done: CompletableDeferred<Pair<String, String?>>) = object : ReportSink {
        override fun executing(detail: String?) {
            detail?.let { record("executing", it) }
        }

        override fun completed(detail: String?) {
            done.complete("completed" to detail)
        }

        override fun failed(reason: FlightReason, detail: String?) {
            done.complete("failed" to "${reason.wire}: $detail")
        }
    }

    private fun noteTransitions(previous: FlightStatus, current: FlightStatus) {
        val changes = ArrayList<String>()
        if (previous.phase != current.phase) changes += "phase ${current.phase}"
        if (previous.watchdog != current.watchdog) changes += "watchdog ${current.watchdog}"
        if (previous.virtualStickEnabled != current.virtualStickEnabled) changes += "virtual_stick ${current.virtualStickEnabled}"
        if (previous.authorityLostReason != current.authorityLostReason) changes += "authority_lost ${current.authorityLostReason}"
        if (previous.landingReason != current.landingReason) changes += "landing ${current.landingReason}"
        if (previous.estopLatched != current.estopLatched) changes += "estop_latched ${current.estopLatched}"
        if (changes.isEmpty()) return
        val rate = synchronized(lock) { measuredRate() }
        val line = "${TIME.format(Date())} ${changes.joinToString(", ")} (stick rate ${format(rate)} Hz)"
        _state.update { it.copy(transitions = (it.transitions + line).takeLast(MAX_TRANSITIONS)) }
        record("transition", changes.joinToString(", "), "stick_rate_hz" to rate, "phase" to current.phase, "watchdog" to current.watchdog, "virtual_stick" to current.virtualStickEnabled, "authority_lost" to current.authorityLostReason, "landing" to current.landingReason)
    }

    private fun measuredRate(): Double {
        val recent = stickTimes.filter { it >= (stickTimes.lastOrNull() ?: 0L) - RATE_WINDOW_MS }
        if (recent.size < 2) return 0.0
        val span = recent.last() - recent.first()
        return if (span > 0) (recent.size - 1) * 1000.0 / span else 0.0
    }

    private fun open() {
        synchronized(lock) {
            if (recorder != null) return
            directory.mkdirs()
            val file = File(directory, "first-flight-${FILE_TIME.format(Date())}.jsonl")
            val out = FileWriter(file, true)
            writer = out
            recorder = BenchRecorder(Flushing(out), SystemClock)
            _state.update { it.copy(logPath = file.absolutePath) }
        }
        val hardware = aircraft.snapshot.value.hardware
        record(
            "session",
            "first-flight probes on ${hardware.aircraftModel} firmware ${hardware.aircraftFirmware}, RC ${hardware.rcFirmware}, MSDK ${hardware.sdkVersion}, ${hardware.phoneModel} Android ${hardware.androidVersion}",
        )
    }

    private fun record(name: String, summary: String, vararg fields: Pair<String, Any?>) {
        val hardware = aircraft.snapshot.value.hardware
        synchronized(lock) {
            recorder?.probe(
                name,
                summary,
                *fields,
                "aircraft_model" to hardware.aircraftModel,
                "aircraft_firmware" to hardware.aircraftFirmware,
                "rc_firmware" to hardware.rcFirmware,
                "msdk_version" to hardware.sdkVersion,
            )
        }
    }

    /** Flushes after every line so a crash or a pulled cable loses nothing. */
    private class Flushing(private val out: FileWriter) : Appendable {
        override fun append(csq: CharSequence?): Appendable {
            out.append(csq)
            return this
        }

        override fun append(csq: CharSequence?, start: Int, end: Int): Appendable {
            out.append(csq, start, end)
            return this
        }

        override fun append(c: Char): Appendable {
            out.append(c)
            if (c == '\n') out.flush()
            return this
        }
    }

    private fun Double.orZero(): Double = if (isNaN()) 0.0 else this

    private fun format(value: Double): String = String.format(Locale.ROOT, "%.2f", value)

    private companion object {
        const val AXIS_PROBE_SPEED_MS = 0.3
        const val AXIS_PROBE_MS = 1_500L
        const val DRILL_MS = 60_000L
        const val AFTERMATH_MS = 60_000L
        const val COMMAND_MS = 90_000L
        const val SAMPLE_MS = 100L
        const val RATE_WINDOW_MS = 2_000L
        const val MAX_TRANSITIONS = 40
        val TIME = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
        val FILE_TIME = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
    }
}
