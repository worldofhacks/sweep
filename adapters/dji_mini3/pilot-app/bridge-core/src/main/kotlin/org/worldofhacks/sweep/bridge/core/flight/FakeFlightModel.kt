package org.worldofhacks.sweep.bridge.core.flight

import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Simple kinematics standing in for the Mini 3 under Virtual Stick so the whole command
 * path, deadman, and takeover run end to end without an aircraft: first-order velocity lag
 * toward the commanded body velocity, yaw angle or rate tracking, auto takeoff to a hover
 * altitude, auto landing to motors off. Like the real flight controller it hovers when stick
 * frames stop arriving for a fraction of a second and it drops Virtual Stick on landing.
 *
 * The model interprets stick frames with DJI's documented convention (`roll` is the body X
 * velocity, `pitch` the body Y velocity) through its own [interpretation] mapping, so a loop
 * configured with the wrong mapping shows the transposed motion the #85 probe looks for.
 */
class FakeFlightModel(
    private val interpretation: AxisMapping = AxisMapping(),
    private val hoverAltitudeM: Double = 1.2,
    private val climbRateMS: Double = 0.5,
    private val descentRateMS: Double = 0.5,
    private val velocityLagS: Double = 0.3,
    private val maxYawRateDegS: Double = 60.0,
    private val stickHoldMs: Long = 500,
) : FlightPort {
    @Volatile
    var connected: Boolean = true
    var enableResult: PortResult = PortResult.Ok
    var takeoffResult: PortResult = PortResult.Ok
    var landingResult: PortResult = PortResult.Ok

    /**
     * How many [advance] calls an [enableVirtualStick] waits before it answers: 0 answers at
     * once; N answers on the Nth advance, standing in for the SDK's asynchronous enable so a
     * test can put the loop in `enabling_virtual_stick` across ticks.
     */
    var deferEnableTicks: Int = 0

    private var pendingEnable: ((PortResult) -> Unit)? = null
    private var pendingEnableTicks = 0

    var xEast = 0.0
        private set
    var yNorth = 0.0
        private set
    var zUp = 0.0
        private set
    var yawDeg = 0.0
        private set
    var vEast = 0.0
        private set
    var vNorth = 0.0
        private set
    var vUp = 0.0
        private set
    var motorsOn = false
        private set
    var virtualStickEnabled = false
        private set
    var advancedMode = false
        private set
    var takingOff = false
        private set
    var landing = false
        private set

    private var commanded = BodyVelocity.ZERO
    private var lastFrameMs: Long? = null
    private var nowMs = 0L
    private var lastAdvanceMs: Long? = null

    val flying: Boolean
        get() = motorsOn && (zUp > 0.0 || takingOff)

    val flightState: String
        get() = when {
            !motorsOn -> "landed"
            takingOff -> "taking_off"
            landing -> "landing"
            !flying -> "armed"
            hypot(vEast, vNorth) < HOVER_SPEED_MS && abs(vUp) < HOVER_SPEED_MS -> "hovering"
            else -> "airborne"
        }

    val facts: AircraftFacts
        get() = AircraftFacts(
            aircraftConnected = connected,
            rcConnected = connected,
            flightState = flightState,
            flying = flying,
            onGround = !flying,
            xEast = xEast,
            yNorth = yNorth,
            zUp = zUp,
            vxEast = vEast,
            vyNorth = vNorth,
            vzUp = vUp,
            yawDeg = yawDeg,
        )

    /** Places the fixture; used by tests and by the fake session's reset. */
    fun place(xEast: Double = 0.0, yNorth: Double = 0.0, zUp: Double = 0.0, yawDeg: Double = 0.0, flying: Boolean = false) {
        this.xEast = xEast
        this.yNorth = yNorth
        this.zUp = zUp
        this.yawDeg = AxisMapping.normalizeYaw(yawDeg)
        vEast = 0.0
        vNorth = 0.0
        vUp = 0.0
        motorsOn = flying
        takingOff = false
        landing = false
        if (flying && this.zUp <= 0.0) this.zUp = hoverAltitudeM
    }

    override fun enableVirtualStick(onResult: (PortResult) -> Unit) {
        if (deferEnableTicks > 0) {
            pendingEnable = onResult
            pendingEnableTicks = deferEnableTicks
            return
        }
        answerEnable(onResult)
    }

    private fun answerEnable(onResult: (PortResult) -> Unit) {
        if (!connected) {
            onResult(PortResult.Failed("aircraft not connected"))
            return
        }
        val result = enableResult
        if (result == PortResult.Ok) virtualStickEnabled = true
        onResult(result)
    }

    override fun disableVirtualStick(onResult: (PortResult) -> Unit) {
        virtualStickEnabled = false
        advancedMode = false
        commanded = BodyVelocity.ZERO
        onResult(PortResult.Ok)
    }

    override fun setAdvancedMode(enabled: Boolean) {
        advancedMode = enabled
    }

    override fun sendStick(frame: StickFrame) {
        if (!virtualStickEnabled || !advancedMode) return
        commanded = interpretation.toBody(frame)
        lastFrameMs = nowMs
    }

    override fun startTakeoff(onResult: (PortResult) -> Unit) {
        if (!connected) {
            onResult(PortResult.Failed("aircraft not connected"))
            return
        }
        if (flying) {
            onResult(PortResult.Failed("already flying"))
            return
        }
        val result = takeoffResult
        if (result == PortResult.Ok) {
            motorsOn = true
            takingOff = true
            landing = false
        }
        onResult(result)
    }

    override fun startLanding(onResult: (PortResult) -> Unit) {
        if (!connected) {
            onResult(PortResult.Failed("aircraft not connected"))
            return
        }
        if (!flying) {
            onResult(PortResult.Failed("on the ground"))
            return
        }
        val result = landingResult
        if (result == PortResult.Ok) {
            landing = true
            takingOff = false
        }
        onResult(result)
    }

    override fun advance(nowMs: Long) {
        this.nowMs = nowMs
        pendingEnable?.let { waiting ->
            pendingEnableTicks -= 1
            if (pendingEnableTicks <= 0) {
                pendingEnable = null
                answerEnable(waiting)
            }
        }
        val previous = lastAdvanceMs
        lastAdvanceMs = nowMs
        if (previous == null) return
        val dt = min((nowMs - previous) / 1000.0, MAX_STEP_S)
        if (dt <= 0.0) return
        when {
            takingOff -> {
                vEast = 0.0
                vNorth = 0.0
                vUp = climbRateMS
                zUp += vUp * dt
                if (zUp >= hoverAltitudeM) {
                    zUp = hoverAltitudeM
                    vUp = 0.0
                    takingOff = false
                }
            }
            landing -> {
                vEast *= 0.5
                vNorth *= 0.5
                vUp = -descentRateMS
                zUp += vUp * dt
                if (zUp <= 0.0) {
                    zUp = 0.0
                    vUp = 0.0
                    vEast = 0.0
                    vNorth = 0.0
                    landing = false
                    motorsOn = false
                    // The flight controller drops Virtual Stick when it lands.
                    virtualStickEnabled = false
                    advancedMode = false
                    commanded = BodyVelocity.ZERO
                }
            }
            flying -> {
                val fresh = lastFrameMs?.let { nowMs - it <= stickHoldMs } ?: false
                val target = if (virtualStickEnabled && advancedMode && fresh) commanded else BodyVelocity.ZERO
                val yawTarget = target.yawTargetDeg
                if (yawTarget != null) {
                    val delta = AxisMapping.yawDelta(yawDeg, yawTarget)
                    val step = maxYawRateDegS * dt
                    yawDeg = if (abs(delta) <= step) yawTarget else AxisMapping.normalizeYaw(yawDeg + if (delta > 0) step else -step)
                } else {
                    yawDeg = AxisMapping.normalizeYaw(yawDeg + target.yawRateDegS.coerceIn(-maxYawRateDegS, maxYawRateDegS) * dt)
                }
                val (targetEast, targetNorth) = GroundFrame.toGround(target.forwardMS, target.rightMS, yawDeg)
                val alpha = min(1.0, dt / velocityLagS)
                vEast += (targetEast - vEast) * alpha
                vNorth += (targetNorth - vNorth) * alpha
                vUp += (target.upMS - vUp) * alpha
                xEast += vEast * dt
                yNorth += vNorth * dt
                zUp = (zUp + vUp * dt).coerceAtLeast(0.05)
            }
            else -> {
                vEast = 0.0
                vNorth = 0.0
                vUp = 0.0
            }
        }
    }

    val speedMS: Double
        get() = sqrt(vEast * vEast + vNorth * vNorth + vUp * vUp)

    private companion object {
        const val HOVER_SPEED_MS = 0.2
        const val MAX_STEP_S = 0.5
    }
}
