package org.worldofhacks.sweep.bridge.atlas

import android.app.Application
import android.location.Location
import android.os.SystemClock
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], application = Application::class)
class AtlasSensorsTest {
    private fun location() = Location("gps").apply {
        latitude = 37.4; longitude = -122.1; accuracy = 4f
        time = System.currentTimeMillis(); elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos()
    }
    @Test fun `only fresh sensor coordinates are recorded and GPS travel bearing is not camera heading`() {
        val sensors = AtlasSensors(RuntimeEnvironment.getApplication()) {}
        assertNull(sensors.position())
        sensors.onLocationChanged(location().apply { bearing = 123f })
        val measured = sensors.position()!!
        assertEquals(37.4, measured.getDouble("latitude"), 0.000001)
        assertEquals(4.0, measured.getDouble("accuracy"), 0.001)
        assertTrue(measured.isNull("heading"))
        assertTrue(measured.isNull("altitude"))
        // Robolectric advances the Android clock here; this is not a wall-clock sleep.
        SystemClock.sleep(11_000)
        assertNull(sensors.position())
    }
    @Test fun `invalid accuracy never becomes qualified capture metadata`() {
        val sensors = AtlasSensors(RuntimeEnvironment.getApplication()) {}
        sensors.onLocationChanged(location().apply { accuracy = Float.NaN })
        assertNull(sensors.position())
        sensors.onLocationChanged(location().apply { accuracy = 0f })
        assertNull(sensors.position())
    }
    @Test fun `older provider callbacks cannot overwrite a newer fix`() {
        val sensors = AtlasSensors(RuntimeEnvironment.getApplication()) {}
        val older = location().apply { latitude = 12.0 }
        SystemClock.sleep(1000)
        sensors.onLocationChanged(location())
        sensors.onLocationChanged(older)
        assertEquals(37.4, sensors.position()!!.getDouble("latitude"), 0.000001)
    }
}
