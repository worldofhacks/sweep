package org.worldofhacks.sweep.bridge

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.core.app.NotificationCompat
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowNetwork

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class PlatformCompatibilityTest {
    @Test
    @Config(sdk = [24, 28, 35])
    fun `wifi status label supports old Android without starting a relay or acquiring locks`() {
        val network = WifiRelayNetwork(RuntimeEnvironment.getApplication())
        val label = WifiRelayNetwork::class.java.getDeclaredMethod("label", Network::class.java, NetworkCapabilities::class.java)
            .apply { isAccessible = true }.invoke(network, ShadowNetwork.newInstance(1), NetworkCapabilities()) as String
        assertTrue(label.startsWith("wifi "))
        assertEquals(null, network.binding.value)
    }

    @Test
    @Config(sdk = [35])
    fun `notification denial does not interrupt status observation and a later grant permits updates`() {
        val application = RuntimeEnvironment.getApplication()
        val service = Robolectric.buildService(BridgeService::class.java).create().get()
        // Do not start the service: that would create a device session and relay socket.
        val notification = NotificationCompat.Builder(service, "bridge").setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("Connection status").build()
        val manager = shadowOf(application.getSystemService(NotificationManager::class.java))
        shadowOf(application).denyPermissions(Manifest.permission.POST_NOTIFICATIONS)
        service.updateNotification(notification)
        assertEquals(0, manager.size())
        shadowOf(application).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        service.updateNotification(notification)
        assertEquals(1, manager.size())
    }
}
