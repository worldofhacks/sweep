package org.worldofhacks.sweep.bridge

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import org.worldofhacks.sweep.bridge.core.frames.PhoneThermalState
import org.worldofhacks.sweep.bridge.node.PhoneStatus
import org.worldofhacks.sweep.bridge.node.PhoneStatusSource

/** Phone battery and thermal status for `node_status`, read at most once a second. */
class AndroidPhoneStatus(context: Context) : PhoneStatusSource {
    private val application = context.applicationContext

    @Volatile
    private var cached = PhoneStatus(batteryPercent = 0, thermalState = PhoneThermalState.NONE)

    @Volatile
    private var cachedAtMs = 0L

    override fun current(): PhoneStatus {
        val now = System.currentTimeMillis()
        if (now - cachedAtMs >= CACHE_MS) {
            cached = read()
            cachedAtMs = now
        }
        return cached
    }

    private fun read(): PhoneStatus {
        val battery = application.getSystemService(BatteryManager::class.java)
            ?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            ?.takeIf { it in 0..100 }
            ?: 0
        val power = application.getSystemService(PowerManager::class.java)
        val thermal = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && power != null) {
            when (power.currentThermalStatus) {
                PowerManager.THERMAL_STATUS_LIGHT -> PhoneThermalState.LIGHT
                PowerManager.THERMAL_STATUS_MODERATE -> PhoneThermalState.MODERATE
                PowerManager.THERMAL_STATUS_SEVERE -> PhoneThermalState.SEVERE
                PowerManager.THERMAL_STATUS_CRITICAL -> PhoneThermalState.CRITICAL
                PowerManager.THERMAL_STATUS_EMERGENCY -> PhoneThermalState.EMERGENCY
                PowerManager.THERMAL_STATUS_SHUTDOWN -> PhoneThermalState.SHUTDOWN
                else -> PhoneThermalState.NONE
            }
        } else {
            PhoneThermalState.NONE
        }
        return PhoneStatus(batteryPercent = battery, thermalState = thermal)
    }

    private companion object {
        const val CACHE_MS = 1_000L
    }
}
