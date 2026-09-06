package org.worldofhacks.sweep.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch
import org.worldofhacks.sweep.bridge.node.LinkState

/**
 * Foreground service that owns the relay link's lifetime (Phase C1). It promotes itself with
 * the `connectedDevice` type (the node is the phone clamped to the USB-attached RC), asks
 * [BridgeNode] to start the link from the stored setup, and mirrors the link state into its
 * notification. Stopping the service closes the socket; the relay then records the loss and
 * a later start rejoins with the next connection epoch.
 */
class BridgeService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var observer: Job? = null
    private var wifiNetwork: WifiRelayNetwork? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val node = (application as BridgeApplication).node
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            notification(node.link.value),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
        )
        when (intent?.action) {
            ACTION_STOP -> {
                node.stopLink()
                releaseNetwork(node)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RECONNECT -> node.reconnect()
            else -> {
                // The Wi-Fi lock, wake lock, and Wi-Fi-bound socket live for the service lifetime.
                if (wifiNetwork == null) {
                    wifiNetwork = WifiRelayNetwork(this).also {
                        node.wifiNetwork = it
                        it.start()
                    }
                }
                node.startLink()
            }
        }
        if (observer == null) {
            observer = scope.launch {
                node.link.map { summary(it) }.distinctUntilChanged().collect { text ->
                    NotificationManagerCompat.from(this@BridgeService).notify(NOTIFICATION_ID, notification(text))
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        observer?.cancel()
        scope.cancel()
        val node = (application as BridgeApplication).node
        node.stopLink()
        releaseNetwork(node)
        super.onDestroy()
    }

    private fun releaseNetwork(node: BridgeNode) {
        wifiNetwork?.stop()
        wifiNetwork = null
        node.wifiNetwork = null
    }

    private fun notification(state: LinkState): Notification = notification(summary(state))

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun summary(state: LinkState): String = buildString {
        append(getString(R.string.notification_relay, state.connection.wire))
        state.connectionEpoch?.let { append(" · ").append(getString(R.string.notification_epoch, it)) }
        state.membership?.let { append(" · ").append(it) }
        if (state.halted) append(" · ").append(getString(R.string.notification_halted))
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, getString(R.string.service_channel), NotificationManager.IMPORTANCE_LOW),
            )
        }
    }

    companion object {
        private const val CHANNEL_ID = "bridge"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_START = "org.worldofhacks.sweep.bridge.START"
        private const val ACTION_STOP = "org.worldofhacks.sweep.bridge.STOP"
        private const val ACTION_RECONNECT = "org.worldofhacks.sweep.bridge.RECONNECT"

        fun start(context: Context) = ContextCompat.startForegroundService(context, intent(context, ACTION_START))

        fun stop(context: Context) = ContextCompat.startForegroundService(context, intent(context, ACTION_STOP))

        fun reconnect(context: Context) = ContextCompat.startForegroundService(context, intent(context, ACTION_RECONNECT))

        private fun intent(context: Context, action: String) = Intent(context, BridgeService::class.java).setAction(action)
    }
}
