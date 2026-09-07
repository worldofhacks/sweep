package org.worldofhacks.sweep.bridge.flight

import android.os.SystemClock
import java.util.concurrent.locks.ReentrantLock
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
import dji.sdk.keyvalue.value.product.ProductType
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import dji.v5.manager.KeyManager
import dji.v5.manager.aircraft.virtualstick.Stick
import dji.v5.manager.aircraft.virtualstick.VirtualStickManager
import dji.v5.manager.aircraft.virtualstick.VirtualStickState
import dji.v5.manager.aircraft.virtualstick.VirtualStickStateListener
import kotlin.concurrent.withLock
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
    private val productFence = ProductGenerationFence()
    private val authorityHolder = Any()
    private var executor: FlightExecutor? = null
    private var pendingContractResult: Pair<AuthorityOperation, (PortResult) -> Unit>? = null
    private val authority = DirectAuthorityMonitor(
        nowMs = SystemClock::elapsedRealtime,
        record = recordAuthorityKey,
        readMode = ::readContractMode,
        publish = { enabled, owner ->
            executor?.onVirtualStickState(enabled, owner == FlightControlAuthority.MSDK.name, owner)
        },
        progress = ::onContractProgress,
    )

    private val manager
        get() = VirtualStickManager.getInstance()

    private val enableFence = VirtualStickEnableFence(
        issueCompensatingDisable = ::compensateLateEnable,
        record = recordAuthorityKey,
    )

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
        productFence.beginTransition()
        enableFence.abandonActive()
        val generation = authority.productConnected()
        enableFence.productChanged()
        productFence.completeTransition(generation)
        executor?.let { readFailsafeSetting(it) }
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
        productFence.beginTransition()
        enableFence.abandonActive()
        authority.disconnected()
        enableFence.productChanged()
        productFence.completeTransition(null)
    }

    /** Accepts the product key only after SdkSession's connection-generation fence has accepted it. */
    fun onProductType(productType: ProductType) {
        authority.productType(productType.name, productType == ProductType.DJI_MINI_3)
    }

    fun detach() {
        manager.removeVirtualStickStateListener(managerDiagnosticListener)
        KeyManager.getInstance().cancelListen(holder)
        KeyManager.getInstance().cancelListen(authorityHolder)
        productFence.beginTransition()
        enableFence.abandonActive()
        authority.disconnected()
        enableFence.productChanged()
        productFence.completeTransition(null)
        executor = null
    }

    override fun enableVirtualStick(onResult: (PortResult) -> Unit) {
        if (!enableFence.isAvailable()) {
            onResult(PortResult.Failed(enableFence.unavailableDetail()))
            return
        }
        val operation = authority.contractEnableIssued()
        if (operation == null) {
            onResult(PortResult.Failed("virtual stick mode contract requires the current connected product to identify as DJI_MINI_3"))
            return
        }
        synchronized(holder) { pendingContractResult = operation to onResult }
        manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() = authority.resetCompleted(operation, "ok")

            override fun onFailure(error: IDJIError) = authority.resetCompleted(operation, describe(error))
        })
    }

    override fun disableVirtualStick(onResult: (PortResult) -> Unit) {
        val operation = authority.disableIssued()
        synchronized(holder) {
            pendingContractResult?.first?.let(enableFence::abandon)
            pendingContractResult = null
        }
        enableFence.abandonActive()
        enableFence.cleanupIssued(operation)
        manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                enableFence.cleanupCompleted(operation)
                if (authority.disableCompleted(operation, "ok")) onResult(PortResult.Ok)
            }

            override fun onFailure(error: IDJIError) {
                val detail = describe(error)
                if (authority.disableCompleted(operation, detail)) onResult(PortResult.Failed(detail))
            }
        })
    }

    private fun compensateLateEnable(operation: AuthorityOperation, onResult: (String) -> Unit) {
        if (!productFence.reserveIssuance(operation)) {
            onResult("product_generation_changed")
            return
        }
        try {
            manager.disableVirtualStick(object : CommonCallbacks.CompletionCallback {
                override fun onSuccess() = onResult("ok")

                override fun onFailure(error: IDJIError) = onResult(describe(error))
            })
        } finally {
            productFence.releaseIssuance()
        }
    }

    private fun onContractProgress(progress: ContractProgress) {
        when (progress) {
            is ContractProgress.IssueEnable -> {
                val started = enableFence.start(
                    operation = progress.operation,
                    issueEnable = { complete ->
                        manager.enableVirtualStick(object : CommonCallbacks.CompletionCallback {
                            override fun onSuccess() = complete("ok")

                            override fun onFailure(error: IDJIError) = complete(describe(error))
                        })
                    },
                    onResult = { status -> authority.enableCompleted(progress.operation, status) },
                )
                if (!started) authority.enableCompleted(progress.operation, "previous virtual stick enable is still being cleaned up")
            }
            is ContractProgress.Verified -> completeContract(progress.operation, PortResult.Ok)
            is ContractProgress.Failed -> completeContract(progress.operation, PortResult.Failed(progress.detail))
        }
    }

    private fun completeContract(operation: AuthorityOperation, result: PortResult) {
        if (result is PortResult.Failed) enableFence.abandon(operation)
        val callback = synchronized(holder) {
            val pending = pendingContractResult
            if (pending?.first != operation) null else {
                pendingContractResult = null
                pending.second
            }
        }
        callback?.invoke(result)
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

    private fun readContractMode(request: AuthorityReadRequest) {
        val key = KeyTools.createKey(FlightControllerKey.KeyVirtualStickEnabled)
        val keyManager = KeyManager.getInstance()
        if (!keyManager.isKeySupported(key)) {
            authority.readResult(request, "unsupported", null)
            return
        }
        keyManager.getValue(
            key,
            object : CommonCallbacks.CompletionCallbackWithParam<Boolean> {
                override fun onSuccess(value: Boolean?) = authority.readResult(request, "ok", value?.toString())

                override fun onFailure(error: IDJIError) = authority.readResult(request, "error", describe(error))
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
    val issuedAtMs: Long,
    val expectedEnabled: Boolean,
)

internal sealed interface ContractProgress {
    data class IssueEnable(val operation: AuthorityOperation) : ContractProgress
    data class Verified(val operation: AuthorityOperation) : ContractProgress
    data class Failed(val operation: AuthorityOperation, val detail: String) : ContractProgress
}

internal class ProductGenerationFence {
    private val lock = ReentrantLock()
    private val noIssuance = lock.newCondition()
    private var changing = false
    private var currentGeneration: Long? = null
    private var issuanceReserved = false

    fun beginTransition() = lock.withLock {
        changing = true
        while (issuanceReserved) noIssuance.await()
    }

    fun completeTransition(generation: Long?) = lock.withLock {
        currentGeneration = generation
        changing = false
    }

    fun reserveIssuance(operation: AuthorityOperation): Boolean = lock.withLock {
        if (changing || currentGeneration != operation.productGeneration) false else {
            issuanceReserved = true
            true
        }
    }

    fun releaseIssuance() = lock.withLock {
        issuanceReserved = false
        noIssuance.signalAll()
    }
}

internal class VirtualStickEnableFence(
    private val issueCompensatingDisable: (AuthorityOperation, onResult: (String) -> Unit) -> Unit,
    private val record: (key: String, event: String, status: String) -> Unit,
) {
    private data class Pending(val operation: AuthorityOperation, var abandoned: Boolean = false)

    private val lock = Any()
    private var pending: Pending? = null
    private var compensating: AuthorityOperation? = null
    private var failedCompensation = false
    private val cleanups = mutableSetOf<AuthorityOperation>()

    fun isAvailable(): Boolean = synchronized(lock) {
        pending == null && compensating == null && !failedCompensation && cleanups.isEmpty()
    }

    fun unavailableDetail(): String = synchronized(lock) {
        if (failedCompensation) {
            "late virtual stick enable compensation failed; reconnect the aircraft before re-enabling virtual stick"
        } else {
            "a previous virtual stick enable is still being cleaned up"
        }
    }

    fun start(
        operation: AuthorityOperation,
        issueEnable: (onResult: (String) -> Unit) -> Unit,
        onResult: (String) -> Unit,
    ): Boolean {
        synchronized(lock) {
            if (pending != null || compensating != null || failedCompensation || cleanups.isNotEmpty()) return false
            pending = Pending(operation)
        }
        issueEnable { status -> completed(operation, status, onResult) }
        return true
    }

    fun abandon(operation: AuthorityOperation) {
        synchronized(lock) { pending?.takeIf { it.operation == operation }?.abandoned = true }
    }

    fun abandonActive() {
        synchronized(lock) { pending?.abandoned = true }
    }

    fun cleanupIssued(operation: AuthorityOperation) {
        synchronized(lock) { cleanups += operation }
    }

    fun cleanupCompleted(operation: AuthorityOperation) {
        synchronized(lock) { cleanups -= operation }
    }

    private fun compensationCompleted(operation: AuthorityOperation, result: String) {
        synchronized(lock) {
            if (compensating != operation) return
            compensating = null
            if (result != "ok") failedCompensation = true
        }
    }

    fun productChanged() {
        synchronized(lock) {
            pending = null
            compensating = null
            failedCompensation = false
            cleanups.clear()
        }
    }

    private fun completed(operation: AuthorityOperation, status: String, onResult: (String) -> Unit) {
        var deliver: String? = null
        val compensate = synchronized(lock) {
            val current = pending?.takeIf { it.operation == operation } ?: return
            pending = null
            if (current.abandoned && status == "ok") {
                compensating = operation
                true
            } else {
                if (!current.abandoned) deliver = status
                false
            }
        }
        deliver?.let(onResult)
        if (!compensate) return
        record(
            "VirtualStickManager.disableVirtualStick",
            "late_enable_compensation_issued",
            "generation=${operation.productGeneration}/${operation.token}",
        )
        issueCompensatingDisable(operation) { result ->
            compensationCompleted(operation, result)
            record(
                "VirtualStickManager.disableVirtualStick",
                "late_enable_compensation_completion",
                "$result generation=${operation.productGeneration}/${operation.token}" +
                    if (result == "ok") "" else " recovery=product_reconnect_required",
            )
        }
    }
}

/**
 * Confirms the DJI virtual-stick mode contract without recasting unavailable authority telemetry
 * as an MSDK owner value. The direct Virtual Stick key supplies state only; a current operation
 * needs reset acknowledgement plus a direct false read, then enable acknowledgement, a
 * post-enable true callback, and a fresh true read before the port admits a stick stream.
 */
internal class DirectAuthorityMonitor(
    private val nowMs: () -> Long,
    private val record: (key: String, event: String, status: String) -> Unit,
    private val readMode: (AuthorityReadRequest) -> Unit,
    private val publish: (enabled: Boolean, owner: String) -> Unit,
    private val progress: (ContractProgress) -> Unit,
) {
    private enum class Stage { RESETTING, RESET_READING, ENABLING, READING, VERIFIED }

    private var productGeneration = 0L
    private var operationToken = 0L
    private var mini3Verified = false
    private var active: AuthorityOperation? = null
    private var stage: Stage? = null
    private var resetIssuedAtMs = 0L
    private var resetAcknowledged = false
    private var resetFalseAtMs: Long? = null
    private var enableIssuedAtMs = 0L
    private var enableAcknowledged = false
    private var enableAcknowledgedAtMs: Long? = null
    private var enableTrueAtMs: Long? = null
    private var readRequest: AuthorityReadRequest? = null

    @Synchronized
    fun productConnected(): Long {
        productGeneration += 1
        invalidate()
        mini3Verified = false
        record("virtual_stick_mode_contract", "product_connected", "generation=$productGeneration product_type=unverified raw_owner=UNKNOWN t_ms=${nowMs()}")
        return productGeneration
    }

    @Synchronized
    fun productType(name: String, supported: Boolean) {
        mini3Verified = supported
        record(
            "virtual_stick_mode_contract",
            "product_type",
            "generation=$productGeneration product_type=$name supported_mini3=$supported raw_owner=UNKNOWN t_ms=${nowMs()}",
        )
        if (!supported) active?.let { fail(it, "product type changed during virtual stick mode contract verification") }
    }

    @Synchronized
    fun disconnected() {
        val operation = active
        productGeneration += 1
        invalidate()
        mini3Verified = false
        record("virtual_stick_mode_contract", "product_disconnected", "generation=$productGeneration raw_owner=UNKNOWN t_ms=${nowMs()}")
        if (operation != null) progress(ContractProgress.Failed(operation, "product disconnected during virtual stick mode contract verification"))
    }

    @Synchronized
    fun contractEnableIssued(): AuthorityOperation? {
        if (!mini3Verified) {
            record("virtual_stick_mode_contract", "rejected", "generation=$productGeneration reason=product_type_unverified raw_owner=UNKNOWN t_ms=${nowMs()}")
            return null
        }
        operationToken += 1
        val operation = AuthorityOperation(productGeneration, operationToken)
        active = operation
        stage = Stage.RESETTING
        resetIssuedAtMs = nowMs()
        resetAcknowledged = false
        resetFalseAtMs = null
        enableAcknowledged = false
        enableAcknowledgedAtMs = null
        enableTrueAtMs = null
        readRequest = null
        record("VirtualStickManager.disableVirtualStick", "contract_reset_issued", "generation=${operation.productGeneration}/${operation.token} proof_category=virtual_stick_mode_contract raw_owner=UNKNOWN t_ms=$resetIssuedAtMs")
        return operation
    }

    @Synchronized
    fun resetCompleted(operation: AuthorityOperation, status: String) {
        if (!matches(operation, Stage.RESETTING, "VirtualStickManager.disableVirtualStick", "reset_completion_dropped", status)) return
        record("VirtualStickManager.disableVirtualStick", "contract_reset_completion", "$status generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        if (status != "ok") {
            fail(operation, "virtual stick reset failed: $status")
            return
        }
        resetAcknowledged = true
        stage = Stage.RESET_READING
        val request = AuthorityReadRequest(operation.productGeneration, operation.token, nowMs(), expectedEnabled = false)
        readRequest = request
        record("KeyVirtualStickEnabled", "contract_reset_read_issued", "generation=${request.productGeneration}/${request.operationToken} t_ms=${request.issuedAtMs}")
        readMode(request)
    }

    @Synchronized
    fun enableCompleted(operation: AuthorityOperation, status: String) {
        if (!matches(operation, Stage.ENABLING, "VirtualStickManager.enableVirtualStick", "completion_dropped", status)) return
        record("VirtualStickManager.enableVirtualStick", "contract_enable_completion", "$status generation=${operation.productGeneration}/${operation.token} t_ms=${nowMs()}")
        if (status != "ok") {
            fail(operation, "virtual stick enable failed: $status")
            return
        }
        enableAcknowledged = true
        enableAcknowledgedAtMs = nowMs()
        maybeReadCurrentMode(operation)
    }

    @Synchronized
    fun disableIssued(): AuthorityOperation {
        operationToken += 1
        val operation = AuthorityOperation(productGeneration, operationToken)
        invalidate()
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
        val receivedAtMs = nowMs()
        record(key.wire, "listener_value", "$value generation=$generation/$operationToken t_ms=$receivedAtMs")
        when (key) {
            AuthorityKey.VIRTUAL_STICK_ENABLED -> when (value) {
                "false" -> {
                    publish(false, "UNKNOWN")
                    active?.takeIf { stage == Stage.ENABLING || stage == Stage.READING }?.let {
                        fail(it, "virtual stick disabled during mode contract verification")
                    }
                }
                "true" -> {
                    val operation = active
                    if (operation != null && stage == Stage.ENABLING && receivedAtMs >= enableIssuedAtMs) {
                        enableTrueAtMs = receivedAtMs
                        maybeReadCurrentMode(operation)
                    }
                }
            }
            AuthorityKey.FLIGHT_CONTROL_CURRENT_AUTHORITY -> {
                if (value != "MSDK" && value != "UNKNOWN") {
                    publish(true, value)
                    active?.takeIf { stage == Stage.ENABLING || stage == Stage.READING }?.let {
                        fail(it, "flight control authority changed to $value during mode contract verification")
                    }
                }
            }
            AuthorityKey.FLIGHT_CONTROL_AUTHORITY_CHANGE_REASON -> Unit
        }
        return true
    }

    @Synchronized
    fun productSupport(key: AuthorityKey, supported: Boolean) {
        record(key.wire, "product_support", "${if (supported) "supported" else "unsupported"} generation=$productGeneration t_ms=${nowMs()}")
    }

    @Synchronized
    fun diagnosticContext(): String =
        "direct_generation=$productGeneration/$operationToken proof_category=${if (active != null) "virtual_stick_mode_contract_pending" else "none"} raw_owner=UNKNOWN"

    @Synchronized
    fun readResult(request: AuthorityReadRequest, result: String, value: String?) {
        if (request != readRequest || request.productGeneration != productGeneration || request.operationToken != operationToken) {
            record("KeyVirtualStickEnabled", "read_dropped", "result_generation=${request.productGeneration}/${request.operationToken} current=$productGeneration/$operationToken t_ms=${nowMs()}")
            return
        }
        val expectedStage = if (request.expectedEnabled) Stage.READING else Stage.RESET_READING
        if (stage != expectedStage) {
            record("KeyVirtualStickEnabled", "read_dropped", "result_generation=${request.productGeneration}/${request.operationToken} current=$productGeneration/$operationToken stage=${stage ?: "none"} t_ms=${nowMs()}")
            return
        }
        val receivedAtMs = nowMs()
        record("KeyVirtualStickEnabled", "contract_read_result", "${value ?: result} result=$result generation=${request.productGeneration}/${request.operationToken} expected_enabled=${request.expectedEnabled} t_ms=$receivedAtMs")
        val operation = active ?: return
        if (result != "ok" || value != request.expectedEnabled.toString() || receivedAtMs - request.issuedAtMs > PROOF_TIMEOUT_MS) {
            val expected = request.expectedEnabled
            fail(operation, "virtual stick mode was not freshly $expected after its matching acknowledgement")
            return
        }
        if (!request.expectedEnabled) {
            resetFalseAtMs = receivedAtMs
            readRequest = null
            maybeIssueEnable(operation)
            return
        }
        stage = Stage.VERIFIED
        active = null
        readRequest = null
        record("virtual_stick_mode_contract", "verified", "generation=${operation.productGeneration}/${operation.token} proof_category=virtual_stick_mode_contract reset_false_t_ms=$resetFalseAtMs enable_ack_t_ms=$enableAcknowledgedAtMs direct_true_t_ms=$enableTrueAtMs read_true_t_ms=$receivedAtMs raw_owner=UNKNOWN")
        progress(ContractProgress.Verified(operation))
    }

    @Synchronized
    private fun maybeIssueEnable(operation: AuthorityOperation) {
        if (active != operation || stage != Stage.RESET_READING || !resetAcknowledged || resetFalseAtMs == null) return
        stage = Stage.ENABLING
        enableIssuedAtMs = nowMs()
        record("VirtualStickManager.enableVirtualStick", "contract_enable_issued", "generation=${operation.productGeneration}/${operation.token} proof_category=virtual_stick_mode_contract reset_false_t_ms=$resetFalseAtMs raw_owner=UNKNOWN t_ms=$enableIssuedAtMs")
        progress(ContractProgress.IssueEnable(operation))
    }

    @Synchronized
    private fun maybeReadCurrentMode(operation: AuthorityOperation) {
        if (active != operation || stage != Stage.ENABLING || !enableAcknowledged || enableTrueAtMs == null) return
        val now = nowMs()
        if (now - enableIssuedAtMs > PROOF_TIMEOUT_MS) {
            fail(operation, "virtual stick mode contract proof timed out")
            return
        }
        stage = Stage.READING
        val request = AuthorityReadRequest(operation.productGeneration, operation.token, now, expectedEnabled = true)
        readRequest = request
        record("KeyVirtualStickEnabled", "contract_read_issued", "generation=${request.productGeneration}/${request.operationToken} direct_true_t_ms=$enableTrueAtMs t_ms=$now")
        readMode(request)
    }

    @Synchronized
    private fun fail(operation: AuthorityOperation, detail: String) {
        if (active != operation) return
        active = null
        stage = null
        readRequest = null
        record("virtual_stick_mode_contract", "failed", "generation=${operation.productGeneration}/${operation.token} proof_category=virtual_stick_mode_contract $detail raw_owner=UNKNOWN t_ms=${nowMs()}")
        progress(ContractProgress.Failed(operation, detail))
    }

    @Synchronized
    private fun matches(operation: AuthorityOperation, expected: Stage, key: String, event: String, status: String): Boolean {
        if (active == operation && stage == expected) return true
        record(key, event, "$status generation=${operation.productGeneration}/${operation.token} current=$productGeneration/$operationToken stage=${stage ?: "none"} t_ms=${nowMs()}")
        return false
    }

    @Synchronized
    private fun invalidate() {
        active = null
        stage = null
        readRequest = null
        resetAcknowledged = false
        resetFalseAtMs = null
        enableAcknowledged = false
        enableAcknowledgedAtMs = null
        enableTrueAtMs = null
    }

    private companion object {
        const val PROOF_TIMEOUT_MS = 4_000L
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
