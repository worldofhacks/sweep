package org.worldofhacks.sweep.bridge.flight

import dji.sdk.keyvalue.key.DJIKey
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.key.RemoteControllerKey
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthority
import dji.sdk.keyvalue.value.flightcontroller.FlightControlAuthorityChangeReason
import dji.sdk.keyvalue.value.flightcontroller.FlightCoordinateSystem
import dji.sdk.keyvalue.value.flightcontroller.RollPitchControlMode
import dji.sdk.keyvalue.value.flightcontroller.VerticalControlMode
import dji.sdk.keyvalue.value.flightcontroller.VirtualStickFlightControlParam
import dji.sdk.keyvalue.value.flightcontroller.YawControlMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import dji.v5.manager.aircraft.virtualstick.Stick
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager
import dji.v5.manager.aircraft.virtualstick.VirtualStickState
import dji.v5.manager.aircraft.virtualstick.VirtualStickStateListener
import kotlin.math.abs
import org.worldofhacks.sweep.bridge.core.flight.FlightPort
import org.worldofhacks.sweep.bridge.core.flight.PortResult
import org.worldofhacks.sweep.bridge.core.flight.StickFrame
import org.worldofhacks.sweep.bridge.core.flight.YawMode

/**
 * The probe flavor's [FlightPort] on MSDK 5.18.0: `IVirtualStickManager` for enable,
 * disable, advanced mode, and `sendVirtualStickAdvancedParam` (velocity, BODY frame), the
 * `KeyStartTakeoff` and `KeyStartAutoLanding` actions, the Virtual Stick state listener and
 * the RC stick, pause, and RTH keys as the takeover signals (E4), and a read-only look at
 * `KeyFailsafeAction` (documented, never changed: the node's own deadman lands indoors).
 *
 * A physical stick past [STICK_TAKEOVER_FRACTION] of full deflection is a takeover
 * (WildBridge's latch pattern from the prior-art notes); plain stick input is not a listed
 * `FlightControlAuthorityChangeReason`, so the node watches the keys itself. Every such event
 * is forwarded: the loop, on its own thread and in order with the commands it admits, is the
 * only judge of whether there is anything to cancel, so no state here can go stale between
 * one activation and the next.
 */
class DjiFlightPort(private val log: (name: String, detail: String) -> Unit) : FlightPort {
    private val holder = Any()
    private var executor: FlightExecutor? = null

    private val manager
        get() = VirtualStickManager.getInstance()

    private val listener = object : VirtualStickStateListener {
        override fun onVirtualStickStateUpdate(stickState: VirtualStickState) {
            val owner = stickState.currentFlightControlAuthorityOwner ?: FlightControlAuthority.UNKNOWN
            log("Virtual stick state", "enabled ${stickState.isVirtualStickEnable}, advanced ${stickState.isVirtualStickAdvancedModeEnabled}, authority ${owner.name}")
            executor?.onVirtualStickState(stickState.isVirtualStickEnable, owner == FlightControlAuthority.MSDK, owner.name)
        }

        override fun onChangeReasonUpdate(reason: FlightControlAuthorityChangeReason) {
            log("Flight control authority change", reason.name)
            val word = when (reason) {
                FlightControlAuthorityChangeReason.MSDK_REQUEST -> null // the node's own enable or disable
                FlightControlAuthorityChangeReason.RC_LOST -> "rc_lost"
                FlightControlAuthorityChangeReason.RC_NOT_P_MODE -> "rc_not_p_mode"
                FlightControlAuthorityChangeReason.RC_SWITCH -> "rc_mode_switch"
                FlightControlAuthorityChangeReason.RC_PAUSE_STOP -> "rc_pause"
                FlightControlAuthorityChangeReason.RC_ONE_KEY_GO_HOME -> "rc_go_home"
                FlightControlAuthorityChangeReason.BATTERY_LOW_GO_HOME -> "battery_low_go_home"
                FlightControlAuthorityChangeReason.BATTERY_SUPER_LOW_LANDING -> "battery_low_landing"
                FlightControlAuthorityChangeReason.NEAR_BOUNDARY -> "near_boundary"
                else -> "authority_${reason.name.lowercase()}"
            }
            if (word != null) executor?.onTakeover(word, "FlightControlAuthorityChangeReason.${reason.name}")
        }
    }

    /** Registers the takeover signals once the SDK is registered; safe to call again. */
    fun attach(executor: FlightExecutor) {
        if (this.executor != null) return
        this.executor = executor
        manager.setVirtualStickStateListener(listener)
        listenStick("left horizontal", RemoteControllerKey.KeyStickLeftHorizontal)
        listenStick("left vertical", RemoteControllerKey.KeyStickLeftVertical)
        listenStick("right horizontal", RemoteControllerKey.KeyStickRightHorizontal)
        listenStick("right vertical", RemoteControllerKey.KeyStickRightVertical)
        listenButton("rc_pause", "pause button", KeyTools.createKey(RemoteControllerKey.KeyPauseButtonDown))
        listenButton("rc_go_home", "return-to-home button", KeyTools.createKey(RemoteControllerKey.KeyGoHomeButtonDown))
    }

    /** Reads the flight controller's failsafe setting for the record; the node never changes it. */
    fun onProductConnected() {
        executor?.let { readFailsafeSetting(it) }
    }

    fun detach() {
        manager.removeVirtualStickStateListener(listener)
        KeyManager.getInstance().cancelListen(holder)
        executor = null
    }

    override fun enableVirtualStick(onResult: (PortResult) -> Unit) = manager.enableVirtualStick(completion(onResult))

    override fun disableVirtualStick(onResult: (PortResult) -> Unit) = manager.disableVirtualStick(completion(onResult))

    override fun setAdvancedMode(enabled: Boolean) = manager.setVirtualStickAdvancedModeEnabled(enabled)

    override fun sendStick(frame: StickFrame) {
        manager.sendVirtualStickAdvancedParam(
            VirtualStickFlightControlParam(
                frame.pitch,
                frame.roll,
                frame.yaw,
                frame.verticalThrottle,
                VerticalControlMode.VELOCITY,
                RollPitchControlMode.VELOCITY,
                if (frame.yawMode == YawMode.ANGLE) YawControlMode.ANGLE else YawControlMode.ANGULAR_VELOCITY,
                FlightCoordinateSystem.BODY,
            ),
        )
    }

    override fun startTakeoff(onResult: (PortResult) -> Unit) = perform(KeyTools.createKey(FlightControllerKey.KeyStartTakeoff), onResult)

    override fun startLanding(onResult: (PortResult) -> Unit) = perform(KeyTools.createKey(FlightControllerKey.KeyStartAutoLanding), onResult)

    private fun perform(key: DJIKey.ActionKey<EmptyMsg, EmptyMsg>, onResult: (PortResult) -> Unit) {
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            onResult(PortResult.Failed("${key.keyInfo.identifier} is not supported by this product"))
            return
        }
        keyManager.performAction(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
                override fun onSuccess(value: EmptyMsg?) = onResult(PortResult.Ok)

                override fun onFailure(error: IDJIError) = onResult(PortResult.Failed(describe(error)))
            },
        )
    }

    private fun completion(onResult: (PortResult) -> Unit) = object : CommonCallbacks.CompletionCallback {
        override fun onSuccess() = onResult(PortResult.Ok)

        override fun onFailure(error: IDJIError) = onResult(PortResult.Failed(describe(error)))
    }

    private fun listenStick(name: String, info: dji.sdk.keyvalue.key.DJIKeyInfo<Int>) {
        listen(KeyTools.createKey(info)) { value ->
            val fraction = abs(value) / Stick.MAX_STICK_POSITION_ABS.toDouble()
            if (fraction >= STICK_TAKEOVER_FRACTION) takeover("rc_takeover", "$name stick ${(fraction * 100).toInt()}%")
        }
    }

    private fun listenButton(word: String, name: String, key: DJIKey<Boolean>) {
        listen(key) { down ->
            if (down) {
                log("RC button", "$name pressed")
                takeover(word, "$name pressed")
            }
        }
    }

    /**
     * Every event goes to the loop. Stick keys fire at the RC update rate while the pilot
     * flies, so nothing is logged here: the loop notes idle input once and logs the takeover
     * itself when it cancels something. Reading the published status here would drop events
     * in the window between a command's admission and the tick that publishes it.
     */
    private fun takeover(word: String, detail: String) {
        executor?.onTakeover(word, detail)
    }

    private fun readFailsafeSetting(executor: FlightExecutor) {
        val key = KeyTools.createKey(FlightControllerKey.KeyFailsafeAction)
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            executor.reportFailsafeSetting("KeyFailsafeAction not supported")
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<dji.sdk.keyvalue.value.flightcontroller.FailsafeAction> {
                override fun onSuccess(value: dji.sdk.keyvalue.value.flightcontroller.FailsafeAction?) =
                    executor.reportFailsafeSetting("KeyFailsafeAction=${value?.name ?: "null"}")

                override fun onFailure(error: IDJIError) = executor.reportFailsafeSetting("KeyFailsafeAction read failed: ${describe(error)}")
            },
        )
    }

    /** Listeners are registered without a support check: the product may connect after registration. */
    private fun <T : Any> listen(key: DJIKey<T>, apply: (T) -> Unit) {
        KeyManager.getInstance().listen(key, holder, CommonCallbacks.KeyListener<T> { _, newValue -> if (newValue != null) apply(newValue) })
    }

    private fun describe(error: IDJIError): String =
        "${error.errorType()} ${error.errorCode()} ${error.description().orEmpty()}".trim()

    private companion object {
        const val STICK_TAKEOVER_FRACTION = 0.3
    }
}
