package org.worldofhacks.sweep.bridge.publish

import android.app.Application
import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.worldofhacks.sweep.bridge.BridgeNode
import org.worldofhacks.sweep.bridge.core.frames.VideoPublishState
import org.worldofhacks.sweep.bridge.node.VideoPublishSource
import org.worldofhacks.sweep.bridge.publish.codec.CodecDecision
import org.worldofhacks.sweep.bridge.publish.codec.CodecEvidence
import org.worldofhacks.sweep.bridge.publish.metrics.PublishMetrics
import org.worldofhacks.sweep.bridge.publish.metrics.PublishMetricsAggregator
import org.worldofhacks.sweep.bridge.publish.metrics.WebRTCStreamMetrics
import org.worldofhacks.sweep.bridge.publish.webrtc.WebRTCMediaOptions
import org.worldofhacks.sweep.bridge.publish.webrtc.WhipPublisher
import org.worldofhacks.sweep.bridge.publish.whip.WhipClient
import org.worldofhacks.sweep.bridge.session.AircraftSession

/** The pilot's override of the automatic policy. */
enum class PublishRequest { AUTO, FORCE_ON, FORCE_OFF }

/** The URLs derived from the Setup values, for the screen and the README checklist. */
data class PublishEndpoints(val whipUrl: String?, val whepUrl: String?, val playerUrl: String?, val error: String?)

/**
 * Process-wide owner of the WHIP publish session (Phase F, issue #51). It watches the relay
 * link, the aircraft, the Setup values, and the pilot's Start/Stop, decides whether a session
 * should run, and drives one [WhipPublisher] at a time through the pure
 * [PublishStateMachine]; `node_status.video_publish_state` reads [current]. Metrics are
 * sampled once a second into the bench log while a session is open.
 *
 * Policy: `AUTO` publishes while the relay link is joined, the aircraft is connected (for
 * sources that need one), and auto-start is on; `FORCE_ON` publishes regardless of the relay
 * (the fake flavor proves the WHIP path without a relay); `FORCE_OFF` holds. Aircraft loss
 * stops the session cleanly; a stopped service stops an automatic session.
 */
class Publisher(
    private val application: Application,
    private val node: BridgeNode,
    private val session: AircraftSession,
    private val sources: PublishSourceFactory,
) : VideoPublishSource {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val loop: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "video-publish").apply { isDaemon = true }
    }
    private val store = PublishSettingsStore(application, sources.available.first())
    private val machine = PublishStateMachine()
    private val aggregator = PublishMetricsAggregator()
    private val timeFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    private val _settings = MutableStateFlow(store.load())
    val settings: StateFlow<PublishSettings> = _settings.asStateFlow()

    val status: StateFlow<PublishStatus> = machine.status

    private val _metrics = MutableStateFlow<PublishMetrics?>(null)
    val metrics: StateFlow<PublishMetrics?> = _metrics.asStateFlow()

    private val _request = MutableStateFlow(PublishRequest.AUTO)
    val request: StateFlow<PublishRequest> = _request.asStateFlow()

    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log.asStateFlow()

    private val _benchFile = MutableStateFlow<String?>(null)
    val benchFile: StateFlow<String?> = _benchFile.asStateFlow()

    private val _lastStop = MutableStateFlow<String?>(null)
    val lastStopReason: StateFlow<String?> = _lastStop.asStateFlow()

    private val _endpoints = MutableStateFlow(PublishEndpoints(null, null, null, null))
    val endpoints: StateFlow<PublishEndpoints> = _endpoints.asStateFlow()

    val availableSources: List<PublishSource>
        get() = sources.available

    private data class Desire(val run: Boolean, val whipUrl: String?, val source: PublishSource, val droneId: Int, val holdReason: String?)

    private class Active(
        val source: PublishSource,
        val whipUrl: String,
        val open: OpenSource,
        val publisher: WhipPublisher,
        val bench: BenchSink?,
        var ticker: ScheduledFuture<*>? = null,
    )

    private var active: Active? = null
    private var restart: ScheduledFuture<*>? = null
    private var desire: Desire? = null

    init {
        node.videoPublish = this
        scope.launch {
            val config = combine(node.setup, _settings, _request) { setup, settings, request -> Triple(setup, settings, request) }
            combine(config, node.running, node.link, session.aircraft.snapshot) { (setup, settings, request), running, link, aircraft ->
                val endpoints = try {
                    PublishEndpoints(
                        whipUrl = WhipEndpoint.whipUrl(setup.relayUrl, settings.mediaHost, settings.mediaPort, setup.droneId),
                        whepUrl = WhipEndpoint.whepUrl(setup.relayUrl, settings.mediaHost, settings.mediaPort, setup.droneId),
                        playerUrl = WhipEndpoint.playerUrl(setup.relayUrl, settings.mediaHost, settings.mediaPort, setup.droneId),
                        error = null,
                    )
                } catch (e: IllegalArgumentException) {
                    PublishEndpoints(null, null, null, e.message)
                }
                _endpoints.value = endpoints
                val aircraftOk = !settings.source.requiresAircraft || aircraft.aircraftConnected
                val run = endpoints.whipUrl != null && setup.loaded && when (request) {
                    PublishRequest.FORCE_OFF -> false
                    PublishRequest.FORCE_ON -> aircraftOk
                    PublishRequest.AUTO -> settings.autoStart && running && link.joined && aircraftOk
                }
                val hold = when {
                    run -> null
                    endpoints.whipUrl == null -> endpoints.error ?: "ground-station address incomplete"
                    !setup.loaded -> "setup still loading"
                    request == PublishRequest.FORCE_OFF -> "stopped by the pilot"
                    !aircraftOk -> PublishReasons.AIRCRAFT_DISCONNECTED
                    !settings.autoStart -> "auto-start is off"
                    !running -> "relay link not started"
                    !link.joined -> "relay link not joined"
                    else -> null
                }
                Desire(run, endpoints.whipUrl, settings.source, setup.droneId, hold)
            }.distinctUntilChanged().collect { next -> post { reconcile(next) } }
        }
    }

    override fun current(): VideoPublishState = machine.current.state

    fun startNow() {
        _request.value = PublishRequest.FORCE_ON
    }

    fun stopNow() {
        _request.value = PublishRequest.FORCE_OFF
    }

    fun resumeAuto() {
        _request.value = PublishRequest.AUTO
    }

    fun setAutoStart(enabled: Boolean) = save(_settings.value.copy(autoStart = enabled))

    fun setSource(source: PublishSource) {
        if (source in sources.available) save(_settings.value.copy(source = source))
    }

    /** A blank host means "the relay host"; an invalid port keeps the stored one. */
    fun saveGroundStation(host: String, port: Int?) {
        val current = _settings.value
        save(current.copy(mediaHost = host.trim(), mediaPort = port?.takeIf { it in 1..65535 } ?: current.mediaPort))
    }

    private fun save(settings: PublishSettings) {
        _settings.value = settings
        scope.launch(Dispatchers.IO) { store.save(settings) }
    }

    // ---- session lifecycle (loop thread) ----

    private fun reconcile(next: Desire) {
        desire = next
        val current = active
        when {
            next.run && current == null -> {
                restart?.cancel(false)
                restart = null
                startSession(next)
            }
            next.run && current != null && (current.whipUrl != next.whipUrl || current.source != next.source) -> {
                stopSession("setup changed")
                startSession(next)
            }
            !next.run && current != null -> stopSession(next.holdReason ?: "no longer wanted")
            !next.run && current == null && machine.current.state == VideoPublishState.FAILED && machine.current.reason != null -> {
                // A terminal failure is left on screen until the pilot changes something.
                if (next.holdReason == PublishReasons.AIRCRAFT_DISCONNECTED) {
                    machine.stop()
                    _lastStop.value = next.holdReason
                }
            }
        }
    }

    private fun startSession(next: Desire) {
        val whipUrl = next.whipUrl ?: return
        val status = machine.start(next.source, whipUrl)
        if (status.state != VideoPublishState.CONNECTING) return
        _lastStop.value = null
        _metrics.value = null
        aggregator.reset()
        logLine("publish start: ${next.source.wire} to $whipUrl")
        val listener = SourceEvents()
        val open = try {
            sources.open(next.source, next.droneId, listener)
        } catch (e: SourceUnavailableException) {
            machine.failed(PublishReasons.SOURCE_UNAVAILABLE, e.message)
            logLine("publish failed: source_unavailable (${e.message})")
            return
        } catch (e: RuntimeException) {
            machine.failed(PublishReasons.SOURCE_UNAVAILABLE, "${e.javaClass.simpleName}: ${e.message}")
            logLine("publish failed: source_unavailable (${e.javaClass.simpleName}: ${e.message})")
            return
        }
        open.codecLabel?.let { machine.codec(it) }
        val bench = BenchSink.open(application.filesDir, next.droneId)
        _benchFile.value = bench?.file?.absolutePath
        bench?.recorder?.note("publish start source=${next.source.wire} url=$whipUrl")
        val boundClient = node.wifiNetwork?.binding?.value?.client?.newBuilder()
            ?.readTimeout(WhipClient.DEFAULT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            ?.writeTimeout(WhipClient.DEFAULT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            ?.build()
        val publisher = WhipPublisher(
            context = application,
            videoCapturer = open.capturer,
            encoderFactory = open.encoderFactory,
            options = WebRTCMediaOptions.mini3LiveView(),
            whipUrl = whipUrl,
            whip = WhipClient(boundClient),
            targetBitrateBps = open.targetBitrateBps,
            maxBitrateBps = open.maxBitrateBps,
            passthrough = next.source == PublishSource.PASSTHROUGH,
            log = ::logLine,
        )
        val session = Active(next.source, whipUrl, open, publisher, bench)
        listener.session = session
        publisher.listener = PublisherEvents(session)
        active = session
        publisher.start()
        session.ticker = loop.scheduleAtFixedRate({ metricsTick(session) }, 1, 1, TimeUnit.SECONDS)
    }

    private fun stopSession(reason: String) {
        val session = active ?: return
        active = null
        release(session)
        machine.stop()
        _lastStop.value = reason
        logLine("publish stopped: $reason")
    }

    private fun release(session: Active) {
        session.ticker?.cancel(false)
        session.ticker = null
        session.publisher.listener = null
        session.publisher.stop()
        runCatching { session.open.close() }
        session.bench?.recorder?.note("publish end")
        session.bench?.close()
        aggregator.reset()
    }

    private fun scheduleRestart(delayMs: Long) {
        restart?.cancel(false)
        restart = loop.schedule(
            {
                restart = null
                val wanted = desire ?: return@schedule
                if (wanted.run && active == null && machine.current.retryPending) {
                    startSession(wanted)
                }
            },
            delayMs,
            TimeUnit.MILLISECONDS,
        )
    }

    private fun metricsTick(session: Active) {
        if (active !== session) return
        val state = machine.current.state
        if (state != VideoPublishState.CONNECTING && state != VideoPublishState.PUBLISHING) return
        session.open.extraDropped?.let { aggregator.onExtraDropped(it()) }
        session.publisher.getStats { sample ->
            post {
                if (active !== session) return@post
                val metrics = aggregator.onTransport(sample)
                _metrics.value = metrics
                session.bench?.recorder?.videoPublish(
                    source = session.source.wire,
                    bitrateKbps = metrics.bitrateKbps,
                    fps = metrics.fps,
                    framesSent = metrics.framesSent,
                    droppedFrames = metrics.droppedFrames,
                    iceState = metrics.iceState,
                    rttMs = metrics.rttMs,
                    processingMs = metrics.processingMs,
                    codec = machine.current.codec ?: metrics.codec,
                    width = metrics.width,
                    height = metrics.height,
                    keyframeIntervalMs = metrics.keyframeIntervalMs,
                )
            }
        }
    }

    /** Events from the publisher thread, fenced to the session they belong to. */
    private inner class PublisherEvents(private val session: Active) : WhipPublisher.Listener {
        override fun onAttempt(attempt: Int) = post {
            if (active !== session) return@post
            if (attempt > 1) machine.attempting()
            logLine("publish attempt $attempt to ${session.whipUrl}")
        }

        override fun onOfferAccepted(resourceUrl: String?, negotiatedCodec: String?) = post {
            if (active !== session) return@post
            machine.offerAccepted(resourceUrl)
            if (machine.current.codec == null && negotiatedCodec != null) {
                machine.codec(if (negotiatedCodec.equals("H264", ignoreCase = true)) "H264 (phone encoder)" else "$negotiatedCodec (no H.264 encoder on this phone)")
            }
        }

        override fun onPublishing() = post {
            if (active !== session) return@post
            machine.publishing()
            logLine("publishing: ICE connected; codec ${machine.current.codec ?: "unknown"}")
            session.bench?.recorder?.note("publishing codec=${machine.current.codec ?: "unknown"}")
        }

        override fun onIceState(state: String) = post {
            if (active !== session) return@post
            logLine("ICE $state")
        }

        override fun onFailure(reason: String, detail: String): Long? {
            // Synchronous: the publisher thread needs the delay before it sleeps.
            if (active !== session) return null
            val delay = machine.failed(reason, detail)
            logLine("publish failed: $reason ($detail)" + (delay?.let { "; retry in $it ms" } ?: "; not retrying"))
            // The bench recorder is single-threaded: write from the loop like the metrics tick does.
            post { if (active === session) session.bench?.recorder?.note("publish failed reason=$reason detail=$detail") }
            return delay
        }

        override fun onStopped() = post {
            if (active !== session) return@post
            // The loop ended on its own (terminal reason): keep the failed status, release the session.
            active = null
            release(session)
        }
    }

    /** Events from the frame source's threads. */
    private inner class SourceEvents : PublishSourceListener {
        @Volatile
        var session: Active? = null

        override fun onCodecEvidence(evidence: CodecEvidence, decision: CodecDecision) = post {
            val mine = session ?: return@post
            if (active !== mine) return@post
            logLine("codec evidence: ${decision.detail}")
            mine.bench?.recorder?.note("codec evidence ${decision.detail} supported=${decision.supported}")
            if (decision.supported) {
                machine.codec("${evidence.label()} passthrough")
            } else {
                active = null
                release(mine)
                machine.failed(decision.reason ?: PublishReasons.CODEC_UNSUPPORTED, decision.detail)
                logLine("publish failed: ${decision.reason} (${decision.detail})")
            }
        }

        override fun onSourceMetrics(metrics: WebRTCStreamMetrics) {
            aggregator.onSource(metrics)
        }

        override fun onSourceFailure(reason: String, detail: String) = post {
            val mine = session ?: return@post
            if (active !== mine) return@post
            active = null
            release(mine)
            val delay = machine.failed(reason, detail)
            logLine("publish failed: $reason ($detail)" + (delay?.let { "; retry in $it ms" } ?: "; not retrying"))
            if (delay != null) scheduleRestart(delay)
        }
    }

    private fun post(block: () -> Unit) {
        try {
            loop.execute {
                try {
                    block()
                } catch (error: RuntimeException) {
                    logLine("publisher task failed: $error")
                }
            }
        } catch (_: RejectedExecutionException) {
            // the publisher is closed
        }
    }

    private fun logLine(line: String) {
        Log.i(TAG, line)
        val stamped = "${timeFormat.format(Date())} $line"
        _log.update { (it + stamped).takeLast(MAX_LOG_LINES) }
    }

    private companion object {
        const val TAG = "SweepPublish"
        const val MAX_LOG_LINES = 100
    }
}
