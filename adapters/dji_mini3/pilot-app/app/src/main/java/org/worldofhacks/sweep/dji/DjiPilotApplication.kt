package org.worldofhacks.sweep.dji

import android.app.Application
import android.content.Context
import dji.v5.common.error.IDJIError
import dji.v5.common.register.DJISDKInitEvent
import dji.v5.manager.SDKManager
import dji.v5.manager.interfaces.SDKManagerCallback
import java.util.concurrent.CopyOnWriteArraySet

class DjiPilotApplication : Application() {
    private val productListeners = CopyOnWriteArraySet<(Boolean) -> Unit>()
    @Volatile private var productConnected = false

    override fun attachBaseContext(base: Context?) {
        super.attachBaseContext(base)
        com.cySdkyc.clx.Helper.install(this)
    }

    override fun onCreate() {
        super.onCreate()
        SDKManager.getInstance().init(this, object : SDKManagerCallback {
            override fun onInitProcess(event: DJISDKInitEvent?, totalProcess: Int) {
                if (event == DJISDKInitEvent.INITIALIZE_COMPLETE) {
                    SDKManager.getInstance().registerApp()
                }
            }

            override fun onRegisterSuccess() = Unit

            override fun onRegisterFailure(error: IDJIError?) {
                publishProductConnection(false)
            }

            override fun onProductConnect(productId: Int) {
                publishProductConnection(true)
            }

            override fun onProductDisconnect(productId: Int) {
                publishProductConnection(false)
            }

            override fun onProductChanged(productId: Int) {
                publishProductConnection(false)
            }

            override fun onDatabaseDownloadProgress(current: Long, total: Long) = Unit
        })
    }

    fun addProductConnectionListener(listener: (Boolean) -> Unit) {
        productListeners += listener
        listener(productConnected)
    }

    fun removeProductConnectionListener(listener: (Boolean) -> Unit) {
        productListeners -= listener
    }

    private fun publishProductConnection(connected: Boolean) {
        productConnected = connected
        productListeners.forEach { it(connected) }
    }
}
