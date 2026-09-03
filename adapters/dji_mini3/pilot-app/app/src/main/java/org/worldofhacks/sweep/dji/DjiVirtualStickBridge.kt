package org.worldofhacks.sweep.dji

import dji.sdk.keyvalue.value.flightcontroller.FlightCoordinateSystem
import dji.sdk.keyvalue.value.flightcontroller.RollPitchControlMode
import dji.sdk.keyvalue.value.flightcontroller.VerticalControlMode
import dji.sdk.keyvalue.value.flightcontroller.VirtualStickFlightControlParam
import dji.sdk.keyvalue.value.flightcontroller.YawControlMode
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager

data class AuthenticatedVirtualStickCommand(
    val sequence: Long,
    val issuedAtMs: Long,
    val parameter: VirtualStickFlightControlParam,
)

sealed interface VirtualStickDecision {
    data object Sent : VirtualStickDecision
    data object Expired : VirtualStickDecision
    data object OutOfOrder : VirtualStickDecision
    data object Disconnected : VirtualStickDecision
}

class DjiVirtualStickBridge(commandTtlMs: Long) {
    private val admission = CommandAdmissionGate(commandTtlMs)
    private val cadence = SendCadence()
    private val watchdog = BridgeWatchdog()
    private val stopCallback = object : CommonCallbacks.CompletionCallback {
        override fun onSuccess() = Unit

        override fun onFailure(error: IDJIError) = Unit
    }

    @Synchronized
    fun enable(callback: CommonCallbacks.CompletionCallback): Boolean {
        if (!watchdog.canDispatch()) return false
        VirtualStickManager.getInstance().setVirtualStickAdvancedModeEnabled(true)
        VirtualStickManager.getInstance().enableVirtualStick(object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                if (watchdog.startDispatch()) {
                    callback.onSuccess()
                } else {
                    VirtualStickManager.getInstance().disableVirtualStick(stopCallback)
                }
            }

            override fun onFailure(error: IDJIError) {
                watchdog.stopDispatch()
                callback.onFailure(error)
            }
        })
        return true
    }

    @Synchronized
    fun disable(callback: CommonCallbacks.CompletionCallback) {
        watchdog.stopDispatch()
        VirtualStickManager.getInstance().disableVirtualStick(callback)
    }

    @Synchronized
    fun acceptAuthenticated(command: AuthenticatedVirtualStickCommand, nowMs: Long): VirtualStickDecision {
        if (!watchdog.dispatchActive()) return VirtualStickDecision.Disconnected
        when (admission.admit(CommandEnvelope(command.sequence, command.issuedAtMs), nowMs)) {
            CommandAdmission.Expired -> return VirtualStickDecision.Expired
            CommandAdmission.OutOfOrder -> return VirtualStickDecision.OutOfOrder
            CommandAdmission.Accepted -> Unit
        }
        VirtualStickManager.getInstance().sendVirtualStickAdvancedParam(command.parameter)
        cadence.recordSend(nowMs)
        return VirtualStickDecision.Sent
    }

    @Synchronized
    fun observedSendRateHz(): Double? = cadence.observedHz()

    fun onProductConnectionChanged(connected: Boolean) {
        onConnectionChanged(ConnectionSource.PRODUCT, connected)
    }

    fun onRelayConnectionChanged(connected: Boolean) {
        onConnectionChanged(ConnectionSource.RELAY, connected)
    }

    fun onLanConnectionChanged(connected: Boolean) {
        onConnectionChanged(ConnectionSource.LAN, connected)
    }

    @Synchronized
    private fun onConnectionChanged(source: ConnectionSource, connected: Boolean) {
        if (watchdog.onConnectionChanged(source, connected) is WatchdogAction.HoldAndStop) {
            VirtualStickManager.getInstance().sendVirtualStickAdvancedParam(holdParameter())
            VirtualStickManager.getInstance().disableVirtualStick(stopCallback)
        }
    }

    private fun holdParameter() = VirtualStickFlightControlParam().apply {
        rollPitchCoordinateSystem = FlightCoordinateSystem.BODY
        rollPitchControlMode = RollPitchControlMode.VELOCITY
        verticalControlMode = VerticalControlMode.VELOCITY
        yawControlMode = YawControlMode.ANGULAR_VELOCITY
        pitch = 0.0
        roll = 0.0
        yaw = 0.0
        verticalThrottle = 0.0
    }
}
