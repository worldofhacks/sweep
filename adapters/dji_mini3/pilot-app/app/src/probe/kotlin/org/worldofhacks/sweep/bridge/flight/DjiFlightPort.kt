package org.worldofhacks.sweep.bridge.flight

import android.os.SystemClock
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
 * `KeyStartTakeoff` and `KeyStartAutoLanding` actions, direct Virtual Stick authority keys and
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
class DjiFlightPort(
    private val log: (name: String, detail: String) -> Unit,
    private val recordAuthorityKey: (key: String, event: String, status: String) -> Unit,
) : FlightPort {
    private val holder = Any()
    private val authorityHolder = Any()
    private var executor: FlightExecutor? = null
    private val authority = DirectAuthorityMonitor(
        nowMs = SystemClock::elapsedRealtime,
        record = recordAuthorityKey,
        read = ::readAuthorityKey,
        publish = { enabled, owner ->
            executor?.onVirtualStickState(enabled, owner == FlightControlAuthority.MSDK.name, owner)
        },
    )

    private val manager
        get() = VirtualStickManager.getInstance()

    private val managerDiagnostic = ManagerAuthorityDiagnostic(recordAuthorityKey, SystemClock::elapsedRealtime)

    private val managerDiagnosticListener = object : VirtualStickStateListener {
        override fun onVirtualStickStateUpdate(state: VirtualStickState) {
            val owner = state.currentFlightControlAuthorityOwner ?: FlightControlAuthority.UNKNOWN
            managerDiagnostic.record(
                enabled = state.isVirtualStickEnable,
                advanced = state.isVirtualStickAdvancedModeEnabled,
                owner = owner.name,
                directContext = authority.diagnosticContext(),
            )
        }

        override fun onChangeReasonUpdate(reason: FlightControlAuthorityChangeReason) {
            recordAuthorityKey(
                "VirtualStickManager.change_reason",
                "listener_value",
                "${reason.name} diagnostic_only ${authority.diagnosticContext()} t_ms=${SystemClock.elapsedRealtime()}",
            )
        }
    }

    /** Registers the takeover signals once the SDK is registered; safe to call again. */
    fun attach(executor: FlightExecutor) {
        if (this.executor != null) return
        this.executor = executor
        manager.setVirtualStickStateListener(managerDiagnosticListener)
        recordAuthorityKey(
            "VirtualStickManager.state",
            "listener_registered",
            "diagnostic_only ${authority.diagnosticContext()} t_ms=${SystemClock.elapsedRealtime()}",
        )
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
        val generation = authority.productConnected()
        val keyManager = KeyManager.getInstance()
        keyManager.cancelListen(authorityHolder)
        listenAuthorityKeys(generation)
        authority.productSupport(
            AuthorityKey.VIRTUAL_STICK_ENABLED,
            keyManager.isKeySupported(KeyTools.createKey(FlightControllerKey.KeyVirtualStickEnabled)),
        )
        authority.productSupport(
            AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY,
            keyManager.isKeySupported(KeyTools.createKey(FlightControllerKey.KeyFlightControlCurrentAuthority)),
        )
        authority.productSupport(
            AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON,
            keyManager.isKeySupported(
                KeyTools.createKey(FlightControllerKey.KeyFlightControlAuthorityChangeReason),
            ),
        )
    }

    fun onProductDisconnected() {
        KeyManager.getInstance().cancelListen(authorityHolder)
        authority.disconnected()
    }

    fun detach() {
        manager.removeVirtualStickStateListener(managerDiagnosticListener)
        KeyManager.getInstance().cancelListen(holder)
        KeyManager.getInstance().cancelListen(authorityHolder)
        authority.disconnected()
        executor = null
    }

    override fun enableVirtualStick(onResult: (PortResult) -> Unit) {
        val generation = authority.enableIssued()
        manager.enableVirtualStick(object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                if (authority.enableCompleted(generation, "ok")) onResult(PortResult.Ok)
            }

            override fun onFailure(error: IDJIError) {
                val detail = describe(error)
                if (authority.enableCompleted(generation, detail)) onResult(PortResult.Failed(detail))
            }
        })
    }

    override fun disableVirtualStick(onResult: (PortResult) -> Unit) {
        val generation = authority.disableIssued()
        manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                if (authority.disableCompleted(generation, "ok")) onResult(PortResult.Ok)
            }

            override fun onFailure(error: IDJIError) {
                val detail = describe(error)
                if (authority.disableCompleted(generation, detail)) onResult(PortResult.Failed(detail))
            }
        })
    }

    override fun setAdvancedMode(enabled: Boolean) = manager.setVirtualStickAdvancedModeEnabled(enabled)

    override fun sendStick(frame: StickFrame) {
        if (frame == StickFrame.NEUTRAL) {
            recordAuthorityKey("VirtualStickManager.sendVirtualStickAdvancedParam", "neutral_dispatch", "sent")
        }
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

    override fun stopTakeoff(onResult: (PortResult) -> Unit) = perform(KeyTools.createKey(FlightControllerKey.KeyStopTakeoff), onResult)

    override fun startLanding(onResult: (PortResult) -> Unit) = perform(KeyTools.createKey(FlightControllerKey.KeyStartAutoLanding), onResult)

    private fun listenAuthorityKeys(generation: Long) {
        listen(KeyTools.createKey(FlightControllerKey.KeyVirtualStickEnabled), authorityHolder) { enabled ->
            authority.listener(generation, AuthorityKey.VIRTUAL_STICK_ENABLED, enabled.toString())
        }
        listen(KeyTools.createKey(FlightControllerKey.KeyFlightControlCurrentAuthority), authorityHolder) { owner ->
            authority.listener(generation, AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY, owner.name)
        }
        listen(KeyTools.createKey(FlightControllerKey.KeyFlightControlAuthorityChangeReason), authorityHolder) { reason ->
            if (authority.listener(generation, AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON, reason.name)) {
                takeoverReason(reason.name)?.let { takeover(it, "FlightControlAuthorityChangeReason.${reason.name}") }
            }
        }
    }

    private fun readAuthorityKey(key: AuthorityKey, request: AuthorityReadRequest) {
        when (key) {
            AuthorityKey.VIRTUAL_STICK_ENABLED -> readAuthorityKey(
                key,
                KeyTools.createKey(FlightControllerKey.KeyVirtualStickEnabled),
                request,
            ) { it.toString() }
            AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY -> readAuthorityKey(
                key,
                KeyTools.createKey(FlightControllerKey.KeyFlightControlCurrentAuthority),
                request,
            ) { it.name }
            AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON -> Unit
        }
    }

    private fun <T : Any> readAuthorityKey(
        authorityKey: AuthorityKey,
        key: DJIKey<T>,
        request: AuthorityReadRequest,
        encode: (T) -> String,
    ) {
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            authority.readResult(authorityKey, request, "unsupported", null)
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<T> {
                override fun onSuccess(value: T?) {
                    authority.readResult(authorityKey, request, "ok", value?.let(encode))
                }

                override fun onFailure(error: IDJIError) = authority.readResult(
                    authorityKey,
                    request,
                    "error",
                    describe(error),
                )
            },
        )
    }

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
    private fun <T : Any> listen(key: DJIKey<T>, holder: Any = this.holder, apply: (T) -> Unit) {
        KeyManager.getInstance().listen(key, holder, CommonCallbacks.KeyListener<T> { _, newValue -> if (newValue != null) apply(newValue) })
    }

    private fun describe(error: IDJIError): String =
        "${error.errorType()} ${error.errorCode()} ${error.description().orEmpty()}".trim()

    private companion object {
        const val STICK_TAKEOVER_FRACTION = 0.3
    }
}

internal enum class AuthorityKey(val wire: String) {
    VIRTUAL_STICK_ENABLED("KeyVirtualStickEnabled"),
    FLIGHT_CONTROL_CURRENT_AUTHORITY("KeyFlightControlCurrentAuthority"),
    FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON("KeyFlightControlAuthorityChangeReason"),
}

internal data class AuthorityOperation(val productGeneration: Long, val token: Long)

internal data class AuthorityReadRequest(
    val productGeneration: Long,
    val operationToken: Long,
    val snapshotToken: Long,
    val issuedAtMs: Long,
    val postEnable: Boolean,
)

internal class DirectAuthorityMonitor(
    private val nowMs: () -> Long,
    private val record: (key: String, event: String, status: String) -> Unit,
    private val read: (AuthorityKey, AuthorityReadRequest) -> Unit,
    private val publish: (enabled: Boolean, owner: String) -> Unit,
) {
    private data class Sample(val value: String, val receivedAtMs: Long)

    private var productGeneration = 0L
    private var operationToken = 0L
    private var snapshotToken = 0L
    private var activeEnable: AuthorityOperation? = null
    private var snapshot: AuthorityReadRequest? = null
    private var samples = mutableMapOf<AuthorityKey, Sample>()

    @Synchronized
    fun productConnected(): Long {
        productGeneration += 1
        invalidate()
        beginSnapshot(postEnable = false)
        return productGeneration
    }

    @Synchronized
    fun disconnected() {
        productGeneration += 1
        invalidate()
        record("direct_authority", "product_disconnected", "generation=$productGeneration t_ms=${nowMs()}")
    }

    @Synchronized
    fun enableIssued(): AuthorityOperation {
        operationToken += 1
        snapshot = null
        samples.clear()
        val operation = AuthorityOperation(productGeneration, operationToken)
        activeEnable = operation
        record("VirtualStickManager.enableVirtualStick", "issued", "generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        return operation
    }

    @Synchronized
    fun enableCompleted(operation: AuthorityOperation, status: String): Boolean {
        if (operation != activeEnable) {
            record("VirtualStickManager.enableVirtualStick", "completion_dropped", "$status generation=${operation.productGeneration}/${operation.token} current=$productGeneration/$operationToken t_ms=${nowMs()}")
            return false
        }
        record("VirtualStickManager.enableVirtualStick", "completion", "$status generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        if (status == "ok") beginSnapshot(postEnable = true) else activeEnable = null
        return true
    }

    @Synchronized
    fun disableIssued(): AuthorityOperation {
        operationToken += 1
        activeEnable = null
        snapshot = null
        samples.clear()
        val operation = AuthorityOperation(productGeneration, operationToken)
        record("VirtualStickManager.disableVirtualStick", "issued", "generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        return operation
    }

    @Synchronized
    fun disableCompleted(operation: AuthorityOperation, status: String): Boolean {
        if (operation.productGeneration != productGeneration || operation.token != operationToken) {
            record("VirtualStickManager.disableVirtualStick", "completion_dropped", "$status generation=${operation.productGeneration}/${operation.token} current=$productGeneration/$operationToken t_ms=${nowMs()}")
            return false
        }
        record("VirtualStickManager.disableVirtualStick", "completion", "$status generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        return true
    }

    @Synchronized
    fun listener(generation: Long, key: AuthorityKey, value: String): Boolean {
        if (generation != productGeneration) {
            record(key.wire, "listener_dropped", "$value generation=$generation current=$productGeneration t_ms=${nowMs()}")
            return false
        }
        record(key.wire, "listener_value", "$value generation=$generation/$operationToken t_ms=${nowMs()}")
        when {
            key == AuthorityKey.VIRTUAL_STICK_ENABLED && value == "false" -> publish(false, "UNKNOWN")
            key == AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY && value != "MSDK" -> publish(true, value)
        }
        return true
    }

    @Synchronized
    fun productSupport(key: AuthorityKey, supported: Boolean) {
        record(key.wire, "product_support", "${if (supported) "supported" else "unsupported"} generation=$productGeneration t_ms=${nowMs()}")
    }

    @Synchronized
    fun diagnosticContext(): String =
        "direct_generation=$productGeneration/$operationToken snapshot=${snapshot?.snapshotToken ?: "none"}"

    @Synchronized
    fun readResult(key: AuthorityKey, request: AuthorityReadRequest, result: String, value: String?) {
        val current = snapshot
        if (request != current) {
            record(key.wire, "read_dropped", "result_generation=${request.productGeneration}/${request.operationToken}/${request.snapshotToken} current=${current?.productGeneration}/${current?.operationToken}/${current?.snapshotToken} t_ms=${nowMs()}")
            return
        }
        val receivedAtMs = nowMs()
        record(key.wire, "read_result", "${value ?: result} result=$result generation=${request.productGeneration}/${request.operationToken}/${request.snapshotToken} t_ms=$receivedAtMs")
        if (result != "ok" || value == null) return
        samples[key] = Sample(value, receivedAtMs)
        val enabled = samples[AuthorityKey.VIRTUAL_STICK_ENABLED] ?: return
        val owner = samples[AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY] ?: return
        val currentAttempt = !request.postEnable || (
            activeEnable != null && request.operationToken == activeEnable?.token
        )
        if (
            !currentAttempt ||
            receivedAtMs - request.issuedAtMs > MAX_SNAPSHOT_AGE_MS ||
            kotlin.math.abs(enabled.receivedAtMs - owner.receivedAtMs) > MAX_READ_SKEW_MS
        ) {
            record("direct_authority", "pair_dropped", "generation=${request.productGeneration}/${request.operationToken}/${request.snapshotToken} t_ms=$receivedAtMs")
            return
        }
        val enabledValue = enabled.value.toBooleanStrictOrNull() ?: return
        publish(enabledValue, owner.value)
    }

    @Synchronized
    private fun invalidate() {
        operationToken += 1
        activeEnable = null
        snapshot = null
        samples.clear()
    }

    @Synchronized
    private fun beginSnapshot(postEnable: Boolean) {
        snapshotToken += 1
        samples.clear()
        val request = AuthorityReadRequest(
            productGeneration = productGeneration,
            operationToken = operationToken,
            snapshotToken = snapshotToken,
            issuedAtMs = nowMs(),
            postEnable = postEnable,
        )
        snapshot = request
        record("direct_authority", "read_issued", "generation=${request.productGeneration}/${request.operationToken}/${request.snapshotToken} post_enable=$postEnable t_ms=${request.issuedAtMs}")
        AuthorityKey.entries
            .filter { it != AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON }
            .forEach { read(it, request) }
    }

    private companion object {
        const val MAX_SNAPSHOT_AGE_MS = 1_000L
        const val MAX_READ_SKEW_MS = 250L
    }
}

internal class ManagerAuthorityDiagnostic(
    private val record: (key: String, event: String, status: String) -> Unit,
    private val nowMs: () -> Long,
) {
    fun record(enabled: Boolean, advanced: Boolean, owner: String, directContext: String) {
        record(
            "VirtualStickManager.state",
            "listener_value",
            "enabled=$enabled advanced=$advanced owner=$owner diagnostic_only owner_freshness=unproven $directContext t_ms=${nowMs()}",
        )
    }
}

internal fun takeoverReason(reason: String): String? = when (reason) {
    "MSDK_REQUEST" -> null
    "RC_LOST" -> "rc_lost"
    "RC_NOT_P_MODE" -> "rc_not_p_mode"
    "RC_SWITCH" -> "rc_mode_switch"
    "RC_PAUSE_STOP" -> "rc_pause"
    "RC_ONE_KEY_GO_HOME" -> "rc_go_home"
    "BATTERY_LOW_GO_HOME" -> "battery_low_go_home"
    "BATTERY_SUPER_LOW_LANDING" -> "battery_low_landing"
    "NEAR_BOUNDARY" -> "near_boundary"
    else -> "authority_${reason.lowercase()}"
}
