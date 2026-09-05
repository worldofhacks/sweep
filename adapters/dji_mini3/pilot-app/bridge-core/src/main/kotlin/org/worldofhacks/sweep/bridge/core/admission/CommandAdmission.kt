package org.worldofhacks.sweep.bridge.core.admission

import org.worldofhacks.sweep.bridge.core.frames.CommandFrame

/** Injectable millisecond clock so admission and the watchdog are testable without sleeping. */
fun interface Clock {
    fun nowMs(): Long
}

object SystemClock : Clock {
    override fun nowMs(): Long = System.currentTimeMillis()
}

/** Machine-readable rejection reasons; the wire names are acknowledgement `reason` codes. */
enum class AdmissionReason(val wire: String) {
    STALE_COMMAND("stale_command"),
    OUT_OF_ORDER_COMMAND("out_of_order_command"),
    STALE_ROSTER("stale_roster"),
    STALE_CONNECTION_EPOCH("stale_connection_epoch"),
    INVALID_SIGNATURE("invalid_signature"),
    INVALID_SELECTION("invalid_selection"),
}

sealed interface AdmissionResult {
    val command: CommandFrame

    data class Admitted(override val command: CommandFrame, val relayNowMs: Long) : AdmissionResult

    data class Rejected(override val command: CommandFrame, val reason: AdmissionReason, val detail: String) : AdmissionResult
}

/**
 * Node-side command gate (Phase E1): signature, drone identity, `connection_epoch`,
 * `roster_version`, strictly increasing `seq` per epoch, then `issued_at + ttl_ms`
 * against the relay clock reconstructed from the local clock plus the offset measured
 * at authentication. Rejections never mutate state, so a stale or out-of-order command
 * cannot poison the sequence for the commands that follow it.
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
