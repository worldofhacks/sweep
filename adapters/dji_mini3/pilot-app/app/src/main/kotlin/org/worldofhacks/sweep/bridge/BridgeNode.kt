package org.worldofhacks.sweep.bridge

import android.app.Application
import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.worldofhacks.sweep.bridge.node.LinkState
import org.worldofhacks.sweep.bridge.node.NodeConfig
import org.worldofhacks.sweep.bridge.node.ReadinessInput
import org.worldofhacks.sweep.bridge.node.RelayLink
import org.worldofhacks.sweep.bridge.node.VideoPublishSource
import org.worldofhacks.sweep.bridge.session.AircraftSession

/**
 * Process-wide owner of the relay link: the encrypted setup store, the pilot's readiness
 * toggles, the link's observable state, and a recent log for the screen. [BridgeService]
 * keeps the process in the foreground and starts or stops the link; the activity only
 * observes and forwards pilot input.
 */
class BridgeNode(private val application: Application, val session: AircraftSession) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val store = BridgeSetupStore(application)
    private val phone = AndroidPhoneStatus(application)
    private val lock = Any()
    private val timeFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    private val _setup = MutableStateFlow(SetupSummary())
    val setup: StateFlow<SetupSummary> = _setup.asStateFlow()

    private val _link = MutableStateFlow(LinkState())
    val link: StateFlow<LinkState> = _link.asStateFlow()

    private val _running = MutableStateFlow(false)
    val running: StateFlow<Boolean> = _running.asStateFlow()

    private val _log = MutableStateFlow<List<String>>(emptyList())
    val log: StateFlow<List<String>> = _log.asStateFlow()

    private val _relayNetwork = MutableStateFlow<String?>(null)
    val relayNetwork: StateFlow<String?> = _relayNetwork.asStateFlow()

    /** Set by [BridgeService]; supplies the Wi-Fi-bound OkHttp client and network label. */
    @Volatile
    var wifiNetwork: WifiRelayNetwork? = null

    /** Set by the Phase F publisher; the link reads it for `node_status.video_publish_state`. */
    @Volatile
    var videoPublish: VideoPublishSource = VideoPublishSource { org.worldofhacks.sweep.bridge.core.frames.VideoPublishState.STOPPED }

    private var relayLink: RelayLink? = null
    private var mirror: Job? = null
    private var networkMirror: Job? = null

    @Volatile
    private var readiness = ReadinessInput()

    init {
        // EncryptedSharedPreferences opens the keystore-backed keyset; keep it off the main thread.
        scope.launch(Dispatchers.IO) { _setup.value = store.summary() }
        // Phase E hook: the flight loop reads thresholds, join, estop, and verified heartbeat time.
        session.flight?.executor?.observe(link)
    }

    /** Saves the Setup fields (a null token keeps the stored one) and optionally (re)connects. */
    fun saveSetup(relayUrl: String, session: String, droneId: Int, token: String?, connect: Boolean) {
        scope.launch(Dispatchers.IO) {
            store.save(relayUrl, session, droneId, token)
            _setup.value = store.summary()
            logLine("setup saved: relay $relayUrl, session $session, drone $droneId" + if (token.isNullOrEmpty()) "" else ", token replaced")
            if (connect) {
                val restart = synchronized(lock) { relayLink != null }
                if (restart) stopLink()
                connect()
            }
        }
    }

    fun connect() = BridgeService.start(application)

    fun disconnect() = BridgeService.stop(application)

    fun reconnect() {
        val link = synchronized(lock) { relayLink }
        if (link == null) connect() else link.reconnectNow()
    }

    /** Starts the service (and the link) only when a complete setup is stored. */
    fun connectIfConfigured() {
        scope.launch(Dispatchers.IO) {
            if (store.load() != null) connect() else logLine("no setup stored yet; enter the relay values on the Setup card")
        }
    }

    /** Called by the foreground service; builds the link from the stored setup. */
    fun startLink() {
        scope.launch(Dispatchers.IO) {
            val setup = store.load()
            if (setup == null) {
                logLine("cannot start the relay link: no setup stored")
                return@launch
            }
            val loopback = isLoopback(hostOf(setup.relayUrl))
            val wifi = wifiNetwork
            // Loopback (adb reverse over USB) is not on the Wi-Fi network, so do not bind it there.
            val binding = if (loopback || wifi == null) null else awaitBinding(wifi)
            val networkLabel = when {
                loopback -> "loopback (adb reverse over USB)"
                binding != null -> binding.label
                else -> "wifi unavailable (relay blocked; no cellular fallback)"
            }
            _relayNetwork.value = networkLabel
            logLine("relay network: $networkLabel")
            synchronized(lock) {
                if (relayLink != null) return@launch
                val config = NodeConfig(
                    relayUrl = setup.relayUrl,
                    session = setup.session,
                    droneId = setup.droneId,
                    token = setup.token,
                    adapterId = "${BuildConfig.AIRCRAFT}-${setup.droneId}",
                    capabilities = AircraftVariant.capabilities,
                )
                val link = RelayLink(
                    config = config,
                    aircraft = session.aircraft,
                    executor = session.executor,
                    phone = phone,
                    log = { line -> logLine(line) },
                    clientProvider = if (loopback) null else ({ wifi?.binding?.value?.client }),
                    videoPublish = { videoPublish.current() },
                )
                relayLink = link
                mirror = scope.launch { link.state.collect { state -> _link.value = state.copy(relayNetwork = _relayNetwork.value) } }
                if (wifi != null && !loopback) {
                    networkMirror = scope.launch {
                        var activeNetwork = binding?.network
                        wifi.binding.collect { current ->
                            val label = current?.label ?: "wifi unavailable (relay blocked; no cellular fallback)"
                            if (label != _relayNetwork.value) {
                                _relayNetwork.value = label
                                logLine("relay network changed: $label")
                            }
                            if (current?.network != activeNetwork) {
                                activeNetwork = current?.network
                                logLine("Wi-Fi binding identity changed; reconnecting with the current bound client")
                                link.reconnectNow()
                            }
                        }
                    }
                }
                link.setReadiness(readiness)
                link.start()
                _running.value = true
            }
        }
    }

    fun stopLink() {
        val link: RelayLink?
        synchronized(lock) {
            link = relayLink
            relayLink = null
            mirror?.cancel()
            mirror = null
            networkMirror?.cancel()
            networkMirror = null
            _running.value = false
        }
        link?.close()
        _link.update { LinkState(readiness = readiness) }
    }

    /** Waits briefly for the Wi-Fi callback's first binding so the first socket is already bound. */
    private suspend fun awaitBinding(wifi: WifiRelayNetwork): WifiRelayNetwork.Binding? {
        wifi.binding.value?.let { return it }
        return withTimeoutOrNull(BINDING_WAIT_MS) {
            wifi.binding.first { it != null }
        }
    }

    private fun hostOf(url: String): String =
        url.substringAfter("://", url).substringBefore('/').substringBefore(':').trim('[', ']')

    private fun isLoopback(host: String): Boolean =
        host == "127.0.0.1" || host.equals("localhost", ignoreCase = true) || host == "::1"

    fun setReadiness(input: ReadinessInput) {
        readiness = input
        val link = synchronized(lock) { relayLink }
        if (link == null) _link.update { it.copy(readiness = input) } else link.setReadiness(input)
    }

    private fun logLine(line: String) {
        Log.i(TAG, line)
        val stamped = "${timeFormat.format(Date())} $line"
        _log.update { (it + stamped).takeLast(MAX_LOG_LINES) }
    }

    private companion object {
        const val TAG = "SweepBridge"
        const val MAX_LOG_LINES = 200
        const val BINDING_WAIT_MS = 3_000L
    }
}
