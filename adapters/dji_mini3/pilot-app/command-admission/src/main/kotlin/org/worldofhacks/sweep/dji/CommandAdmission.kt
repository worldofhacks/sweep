package org.worldofhacks.sweep.dji

data class CommandEnvelope(val sequence: Long, val issuedAtMs: Long) {
    init {
        require(sequence >= 0)
        require(issuedAtMs >= 0)
    }
}

sealed interface CommandAdmission {
    data object Accepted : CommandAdmission
    data object Expired : CommandAdmission
    data object OutOfOrder : CommandAdmission
}

class CommandAdmissionGate(private val commandTtlMs: Long) {
    private var newestSequence = -1L

    init {
        require(commandTtlMs > 0)
    }

    fun admit(command: CommandEnvelope, receivedAtMs: Long): CommandAdmission {
        require(receivedAtMs >= command.issuedAtMs)
        if (command.sequence <= newestSequence) return CommandAdmission.OutOfOrder

        newestSequence = command.sequence
        return if (receivedAtMs - command.issuedAtMs > commandTtlMs) {
            CommandAdmission.Expired
        } else {
            CommandAdmission.Accepted
        }
    }
}

class SendCadence {
    private var firstSentAtMs: Long? = null
    private var lastSentAtMs: Long? = null
    private var sentCount = 0L

    fun recordSend(sentAtMs: Long) {
        require(sentAtMs >= 0)
        require(lastSentAtMs == null || sentAtMs >= lastSentAtMs!!)
        if (firstSentAtMs == null) firstSentAtMs = sentAtMs
        lastSentAtMs = sentAtMs
        sentCount += 1
    }

    fun observedHz(): Double? {
        val first = firstSentAtMs ?: return null
        val last = lastSentAtMs ?: return null
        val durationMs = last - first
        if (sentCount < 2 || durationMs <= 0) return null
        return (sentCount - 1) * 1_000.0 / durationMs
    }
}
