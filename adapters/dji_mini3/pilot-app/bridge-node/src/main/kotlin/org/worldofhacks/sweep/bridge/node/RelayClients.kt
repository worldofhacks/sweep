package org.worldofhacks.sweep.bridge.node

import java.util.concurrent.TimeUnit
import javax.net.SocketFactory
import okhttp3.Dns
import okhttp3.OkHttpClient

/**
 * Builds the OkHttp client the relay link uses. Kept here (pure JVM) so the timeouts and the
 * 3-second ping are set in one place; the Android layer passes a [socketFactory] and [dns]
 * bound to the Wi-Fi network so the relay socket never routes over cellular when the Wi-Fi AP
 * fails internet validation (issue #43 deployment note). The read timeout is 0 because a
 * WebSocket is long-lived; liveness comes from the ping interval, not a read deadline.
 */
object RelayClients {
    fun build(
        timing: LinkTiming,
        socketFactory: SocketFactory? = null,
        dns: Dns? = null,
    ): OkHttpClient {
        val builder = OkHttpClient.Builder()
            .connectTimeout(timing.connectTimeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(timing.pingIntervalMs, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(false)
        if (socketFactory != null) builder.socketFactory(socketFactory)
        if (dns != null) builder.dns(dns)
        return builder.build()
    }
}
