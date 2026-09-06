package org.worldofhacks.sweep.bridge.core.admission

import org.worldofhacks.sweep.bridge.core.frames.CommandFrame

/** Injectable millisecond clock so admission and the watchdog are testable without sleeping. */
fun interface Clock {
    fun nowMs(): Long
}

object SystemClock : Clock {
    override fun nowMs(): Long = System.currentTimeMillis()
}

/**
 * Why a command was not admitted. [wire] is the acknowledgement `reason` the node returns.
 * The relay contract fixes exactly two: `stale_command` (expired TTL, a roster version other
 * than the last state received, or an epoch other than the current one) and
 * `out_of_order_command` (a `seq` not above the last admitted one). A frame whose signature
 * does not verify, or that addresses another drone, is dropped and logged locally and never
 * acknowledged ([acknowledged] is false), so a forged frame cannot draw a response from the
 * node.
 */
enum class AdmissionReason(val wire: String, val acknowledged: Boolean) {
    STALE_COMMAND("stale_command", true),
    OUT_OF_ORDER_COMMAND("out_of_order_command", true),
    STALE_ROSTER("stale_command", true),
    STALE_CONNECTION_EPOCH("stale_command", true),
    INVALID_SIGNATURE("invalid_signature", false),
    INVALID_SELECTION("invalid_selection", false),
}

sealed interface AdmissionResult {
    val command: CommandFrame

    data class Admitted(override val command: CommandFrame, val relayNowMs: Long) : AdmissionResult

    data class Rejected(override val command: CommandFrame, val reason: AdmissionReason, val detail: String) : AdmissionResult
}

/**
 * Node-side command gate: signature, drone identity, `connection_epoch`, `roster_version`,
 * strictly increasing `seq` per epoch, then `issued_at + ttl_ms` against the relay clock
 * reconstructed from the local clock plus the offset measured at authentication. Rejections
 * never mutate state, so a stale or out-of-order command cannot poison the sequence for the
 * commands that follow it. A rejected command is never resent by the relay.
 */
class CommandAdmission(
    private val key: ByteArray,
    val droneId: Int,
    private val clock: Clock,
    val futureSkewMs: Long = DEFAULT_FUTURE_SKEW_MS,
) {
    /** Relay clock minus local clock in milliseconds, measured from the auth exchange. */
    var relayOffsetMs: Long = 0

    var connectionEpoch: Int? = null
        private set

    var rosterVersion: Int? = null
        private set

    var lastSeq: Long? = null
        private set

    fun bind(connectionEpoch: Int, rosterVersion: Int) {
        if (this.connectionEpoch != connectionEpoch) lastSeq = null
        this.connectionEpoch = connectionEpoch
        this.rosterVersion = rosterVersion
    }

    fun updateRosterVersion(rosterVersion: Int) {
        this.rosterVersion = rosterVersion
    }

    fun relayNowMs(): Long = clock.nowMs() + relayOffsetMs

    fun admit(command: CommandFrame): AdmissionResult {
        if (!command.verify(key)) {
            return reject(command, AdmissionReason.INVALID_SIGNATURE, "command signature did not verify")
        }
        if (command.droneId != droneId) {
            return reject(
                command,
                AdmissionReason.INVALID_SELECTION,
                "command addresses drone ${command.droneId}; this node is drone $droneId",
            )
        }
        val epoch = connectionEpoch
            ?: return reject(command, AdmissionReason.STALE_CONNECTION_EPOCH, "node has not bound a connection epoch")
        if (command.connectionEpoch != epoch) {
            return reject(
                command,
                AdmissionReason.STALE_CONNECTION_EPOCH,
                "command epoch ${command.connectionEpoch} is not the current epoch $epoch",
            )
        }
        val roster = rosterVersion
            ?: return reject(command, AdmissionReason.STALE_ROSTER, "node has not bound a roster version")
        if (command.rosterVersion != roster) {
            return reject(
                command,
                AdmissionReason.STALE_ROSTER,
                "command roster_version ${command.rosterVersion} is not the current roster $roster",
            )
        }
        val last = lastSeq
        if (last != null && command.seq <= last) {
            return reject(command, AdmissionReason.OUT_OF_ORDER_COMMAND, "seq ${command.seq} is not above $last")
        }
        val now = relayNowMs()
        if (command.issuedAt > now + futureSkewMs) {
            return reject(
                command,
                AdmissionReason.STALE_COMMAND,
                "issued_at is ${command.issuedAt - now} ms in the future beyond the $futureSkewMs ms skew allowance",
            )
        }
        val expiresAt = command.issuedAt + command.ttlMs
        if (expiresAt < now) {
            return reject(
                command,
                AdmissionReason.STALE_COMMAND,
                "issued_at plus ttl_ms elapsed ${now - expiresAt} ms ago on the relay clock",
            )
        }
        lastSeq = command.seq
        return AdmissionResult.Admitted(command, now)
    }

    private fun reject(command: CommandFrame, reason: AdmissionReason, detail: String) =
        AdmissionResult.Rejected(command, reason, detail)

    companion object {
        const val DEFAULT_FUTURE_SKEW_MS = 1_000L
    }
}
