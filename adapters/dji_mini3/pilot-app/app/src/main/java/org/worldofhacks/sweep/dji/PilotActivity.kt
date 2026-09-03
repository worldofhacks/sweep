package org.worldofhacks.sweep.dji

import android.graphics.SurfaceTexture
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Bundle
import android.os.SystemClock
import android.view.Surface
import android.view.TextureView
import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.appcompat.app.AppCompatActivity

class PilotActivity : AppCompatActivity() {
    private val bridge = DjiVirtualStickBridge(commandTtlMs = 250)
    private val feed = DjiMediaAndVideo()
    private val feedMonitor = FeedMonitor(staleAfterMs = 1_000)
    private lateinit var feedView: TextureView
    private lateinit var overlay: VisualAdvisoryOverlay
    private var feedSurface: Surface? = null
    private var networkCallbackRegistered = false
    private var started = false
    private val productListener: (Boolean) -> Unit = bridge::onProductConnectionChanged
    private val advisoryRefresh = object : Runnable {
        override fun run() {
            overlay.show(feedMonitor.advisory(SystemClock.elapsedRealtime()))
            overlay.postDelayed(this, 250)
        }
    }
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = updateLanConnection()

        override fun onLost(network: Network) = updateLanConnection()

        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
            updateLanConnection()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        feedView = TextureView(this)
        overlay = VisualAdvisoryOverlay(this)
        feedView.surfaceTextureListener = surfaceListener
        setContentView(FrameLayout(this).apply {
            addView(feedView, matchParentLayout())
            addView(this@PilotActivity.overlay, matchParentLayout())
        })
    }

    override fun onStart() {
        super.onStart()
        started = true
        (application as DjiPilotApplication).addProductConnectionListener(productListener)
        attachFeedSurface()
        feed.startCameraAvailability { available ->
            runOnUiThread { feedMonitor.onCameraChanged(available) }
        }
        connectivityManager().registerDefaultNetworkCallback(networkCallback)
        networkCallbackRegistered = true
        updateLanConnection()
        overlay.post(advisoryRefresh)
    }

    override fun onStop() {
        started = false
        overlay.removeCallbacks(advisoryRefresh)
        feed.stopCameraAvailability()
        feedSurface?.let(feed::detachSurface)
        feedMonitor.onSurfaceChanged(false)
        if (networkCallbackRegistered) {
            connectivityManager().unregisterNetworkCallback(networkCallback)
            networkCallbackRegistered = false
        }
        bridge.onLanConnectionChanged(false)
        bridge.onRelayConnectionChanged(false)
        (application as DjiPilotApplication).removeProductConnectionListener(productListener)
        bridge.onProductConnectionChanged(false)
        super.onStop()
    }

    fun onRelayConnectionChanged(connected: Boolean) {
        bridge.onRelayConnectionChanged(connected)
    }

    private fun updateLanConnection() {
        val manager = connectivityManager()
        val capabilities = manager.getNetworkCapabilities(manager.activeNetwork)
        bridge.onLanConnectionChanged(
            capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true ||
                capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) == true,
        )
    }

    private fun connectivityManager(): ConnectivityManager =
        getSystemService(ConnectivityManager::class.java)

    private fun attachFeedSurface() {
        if (!started) return
        val surface = feedSurface ?: return
        if (feedView.width <= 0 || feedView.height <= 0) return
        feed.attachPrimarySurface(surface, feedView.width, feedView.height)
        feedMonitor.onSurfaceChanged(true)
    }

    private fun matchParentLayout() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT,
    )

    private val surfaceListener = object : TextureView.SurfaceTextureListener {
        override fun onSurfaceTextureAvailable(texture: SurfaceTexture, width: Int, height: Int) {
            val surface = Surface(texture)
            feedSurface = surface
            if (started) {
                feed.attachPrimarySurface(surface, width, height)
                feedMonitor.onSurfaceChanged(true)
            }
        }

        override fun onSurfaceTextureSizeChanged(texture: SurfaceTexture, width: Int, height: Int) {
            if (started) feedSurface?.let { feed.attachPrimarySurface(it, width, height) }
        }

        override fun onSurfaceTextureDestroyed(texture: SurfaceTexture): Boolean {
            feedSurface?.let {
                feed.detachSurface(it)
                it.release()
            }
            feedSurface = null
            feedMonitor.onSurfaceChanged(false)
            return true
        }

        override fun onSurfaceTextureUpdated(texture: SurfaceTexture) {
            feedMonitor.onFrame(
                quality = feed.primaryFrameQuality(),
                observedAtMs = SystemClock.elapsedRealtime(),
            )
        }
    }
}
