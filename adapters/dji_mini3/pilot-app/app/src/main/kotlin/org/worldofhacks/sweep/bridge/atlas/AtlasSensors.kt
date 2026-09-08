package org.worldofhacks.sweep.bridge.atlas

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.GeomagneticField
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock
import androidx.core.content.ContextCompat
import org.json.JSONObject

/** Capture-time measurements only. No background tracking or synthetic GPS fallback. */
class AtlasSensors(private val context: Context, private val changed: (String) -> Unit) : LocationListener, SensorEventListener {
    private val locations = context.getSystemService(LocationManager::class.java)
    private val sensors = context.getSystemService(SensorManager::class.java)
    private var fix: Location? = null
    private var magneticHeading: Double? = null
    private var headingTime = 0L
    private var reliableHeading = false
    fun start() {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            for (provider in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
                try {
                    locations.getLastKnownLocation(provider)?.let(::onLocationChanged)
                    locations.requestLocationUpdates(provider, 1000, 0f, this, Looper.getMainLooper())
                } catch (_: SecurityException) { /* Approximate-only permission can exclude GPS. */ }
                catch (_: IllegalArgumentException) { /* Provider absent on this device. */ }
            }
        }
        sensors.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR)?.let {
            sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_UI)
        }
    }
    fun stop() { locations.removeUpdates(this); sensors.unregisterListener(this); magneticHeading = null }
    override fun onLocationChanged(location: Location) {
        if (fix == null || location.elapsedRealtimeNanos >= fix!!.elapsedRealtimeNanos) fix = Location(location)
        changed(if (position() != null) "GPS ±${location.accuracy.toInt()} m" else "Waiting for a fresh GPS fix")
    }
    override fun onProviderEnabled(provider: String) = Unit
    override fun onProviderDisabled(provider: String) { changed("Location provider unavailable") }
    @Deprecated("Legacy Android callback") override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) { reliableHeading = accuracy >= SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM }
    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type != Sensor.TYPE_ROTATION_VECTOR) return
        val matrix = FloatArray(9)
        SensorManager.getRotationMatrixFromVector(matrix, event.values)
        // Direction of the rear camera's -Z optical axis projected onto the horizontal plane.
        val east = -matrix[2].toDouble(); val north = -matrix[5].toDouble()
        magneticHeading = if (kotlin.math.hypot(east, north) > 0.15) Math.toDegrees(kotlin.math.atan2(east, north)) else null
        headingTime = event.timestamp
        reliableHeading = event.accuracy >= SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM
    }
    fun position(): JSONObject? {
        val location = fix ?: return null
        val now = SystemClock.elapsedRealtimeNanos()
        val age = (now - location.elapsedRealtimeNanos) / 1_000_000
        if (age !in 0..10_000 || !location.hasAccuracy() || !location.accuracy.isFinite() || location.accuracy <= 0 || location.accuracy > 10_000 ||
            location.latitude !in -85.0..85.0 || location.longitude !in -180.0..180.0) return null
        val altitude = location.altitude.takeIf { location.hasAltitude() && it in -500.0..15_000.0 }
        val heading = magneticHeading?.takeIf { reliableHeading && now - headingTime in 0..2_000_000_000L }?.let {
            val declination = GeomagneticField(location.latitude.toFloat(), location.longitude.toFloat(),
                (altitude ?: 0.0).toFloat(), System.currentTimeMillis()).declination
            (it + declination + 360) % 360
        }
        return JSONObject().put("latitude", location.latitude).put("longitude", location.longitude)
            .put("accuracy", location.accuracy.toDouble()).put("timestamp", System.currentTimeMillis() - age)
            .put("altitude", altitude ?: JSONObject.NULL).put("heading", heading ?: JSONObject.NULL)
    }
}
