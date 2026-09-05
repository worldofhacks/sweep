package org.worldofhacks.sweep.bridge

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
import java.net.InetAddress
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.Dns
import okhttp3.OkHttpClient
import org.worldofhacks.sweep.bridge.node.LinkTiming
import org.worldofhacks.sweep.bridge.node.RelayClients

/**
 * Binds the relay socket to the Wi-Fi transport and keeps the radio and CPU awake for the
 * foreground service's lifetime (issue #43 deployment note: the phone's only USB-C port holds
 * the RC-N1, so the relay link is Wi-Fi, and the flight-room AP may fail internet validation).
 *
 * - `ConnectivityManager.requestNetwork(TRANSPORT_WIFI)` yields a [Network] whose
 *   `socketFactory` and DNS OkHttp uses, so Android never routes the relay socket over cellular
 *   when the AP has no internet. Requires `CHANGE_NETWORK_STATE`.
 * - A `WifiManager.WifiLock` in `WIFI_MODE_FULL_LOW_LATENCY` (falling back to `FULL_HIGH_PERF`
 *   below API 29) and a partial `PowerManager.WakeLock` are held while bound and released on
 *   [stop].
 * - The label is `wifi <ssid-or-bssid or interface>` for the connectivity status; SSID/BSSID
 *   are only available with a location grant, so the interface name is the privacy-preserving
 *   fallback rather than requesting location here.
 *
 * The watchdog and telemetry stay local to the node; nothing about aircraft stability depends
 * on this Wi-Fi binding — it only decides which radio the relay socket uses.
 */
class WifiRelayNetwork(context: Context, private val timing: LinkTiming = LinkTiming()) {
    private val application = context.applicationContext
    private val connectivity = application.getSystemService(ConnectivityManager::class.java)
    private val wifiManager = application.getSystemService(WifiManager::class.java)
    private val powerManager = application.getSystemService(PowerManager::class.java)

    private val _binding = MutableStateFlow<Binding?>(null)
    val binding: StateFlow<Binding?> = _binding.asStateFlow()

    private var callback: ConnectivityManager.NetworkCallback? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    data class Binding(val network: Network, val label: String, val client: OkHttpClient)

    fun start() {
        if (callback != null) return
        acquireLocks()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()
        val networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                _binding.value = Binding(network, label(network), clientFor(network))
            }

            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                val current = _binding.value
                _binding.value = Binding(network, label(network, caps), current?.client ?: clientFor(network))
            }

            override fun onLost(network: Network) {
                if (_binding.value?.network == network) _binding.value = null
            }
        }
        callback = networkCallback
        connectivity?.requestNetwork(request, networkCallback)
    }

    fun stop() {
        callback?.let { runCatching { connectivity?.unregisterNetworkCallback(it) } }
        callback = null
        _binding.value = null
        releaseLocks()
    }

    private fun clientFor(network: Network): OkHttpClient {
        // okhttp3.Dns is a Kotlin interface (not a fun interface), so implement it explicitly:
        // resolve names on the bound Wi-Fi network so a no-internet AP still reaches the relay.
        val dns = object : Dns {
            override fun lookup(hostname: String): List<InetAddress> =
                network.getAllByName(hostname)?.toList() ?: InetAddress.getAllByName(hostname).toList()
        }
        return RelayClients.build(timing, socketFactory = network.socketFactory, dns = dns)
    }

    private fun label(network: Network, caps: NetworkCapabilities? = connectivity?.getNetworkCapabilities(network)): String {
        val wifiInfo = (caps?.transportInfo as? WifiInfo) ?: legacyWifiInfo()
        val ssid = wifiInfo?.ssid?.trim('"')?.takeIf { it.isNotEmpty() && it != WifiManager.UNKNOWN_SSID }
        val bssid = wifiInfo?.bssid?.takeIf { it.isNotEmpty() && it != "02:00:00:00:00:00" }
        val iface = connectivity?.getLinkProperties(network)?.let(LinkProperties::getInterfaceName)
        val detail = ssid ?: bssid ?: iface ?: "connected"
        return "wifi $detail"
    }

    @Suppress("DEPRECATION")
    private fun legacyWifiInfo(): WifiInfo? =
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) wifiManager?.connectionInfo else null

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        } else {
            WifiManager.WIFI_MODE_FULL_HIGH_PERF
        }
        wifiLock = wifiManager?.createWifiLock(mode, WIFI_LOCK_TAG)?.apply {
            setReferenceCounted(false)
            acquire()
        }
        wakeLock = powerManager?.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)?.apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseLocks() {
        wifiLock?.let { if (it.isHeld) it.release() }
        wifiLock = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    private companion object {
        const val WIFI_LOCK_TAG = "sweep-bridge:relay"
        const val WAKE_LOCK_TAG = "sweep-bridge:relay-wake"
    }
}
