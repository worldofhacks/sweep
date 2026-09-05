package org.worldofhacks.sweep.bridge.node

/**
 * What the node knows about one telemetry key's listener: whether it is registered, what
 * `isKeySupported` answered when it was registered (normally right after SDK registration,
 * before any aircraft is connected, when the answer is usually no) and again at the last
 * product connect, and when the key's first value arrived. Shown on the node status card
 * and written to the bench log so the first on-phone run says which keys reported.
 */
data class TelemetryKeyStatus(
    val listening: Boolean = false,
    val supportedAtAttach: Boolean? = null,
    val supportedAtConnect: Boolean? = null,
    val firstValueAtMs: Long? = null,
)

/**
 * The decisions behind the probe flavor's `KeyManager` listeners, free of DJI classes so
 * they run on the JVM. `isKeySupported` answers for the product connected right now, and
 * registration usually completes before the aircraft is there, so the answer is recorded
 * and never used to skip a key: every key is listened at [attach], and the answer is asked
 * again at every [productConnected]. Both return only the keys that have no listener yet,
 * so a reconnect registers nothing twice (MSDK v5 adds one more listener per `listen` call
 * and removes them by holder), and [detach] forgets them all for the caller's
 * `cancelListen(holder)`.
 */
class TelemetryKeyLedger(val keys: List<String>) {
    private val status: LinkedHashMap<String, TelemetryKeyStatus> = keys.associateWithTo(LinkedHashMap()) { TelemetryKeyStatus() }

    /** When the listeners were registered; null until [attach] and again after [detach]. */
    var attachedAtMs: Long? = null
        private set

    /**
     * Registers listeners: returns every key without one, whatever [supported] answers, and
     * records that answer as the key's support at registration.
     */
    fun attach(nowMs: Long, supported: (String) -> Boolean): List<String> {
        if (attachedAtMs == null) attachedAtMs = nowMs
        val missing = keys.filter { !status.getValue(it).listening }
        for (name in missing) status[name] = status.getValue(name).copy(listening = true, supportedAtAttach = supported(name))
        return missing
    }

    /**
     * A product connected: asks [supported] again for every key, for the record, and returns
     * the keys that still have no listener (none when [attach] ran first), so they are
     * listened before the first frame and nothing already listened is registered again.
     */
    fun productConnected(supported: (String) -> Boolean): List<String> {
        val missing = ArrayList<String>()
        for (name in keys) {
            val current = status.getValue(name)
            if (!current.listening) missing += name
            status[name] = current.copy(listening = true, supportedAtConnect = supported(name))
        }
        return missing
    }

    /** Forgets every listener and its evidence; the caller cancels them with the holder they were registered under. */
    fun detach() {
        attachedAtMs = null
        for (name in keys) status[name] = TelemetryKeyStatus()
    }

    /** Records a value's arrival; true when it is the key's first, false afterwards and for a key this ledger does not know. */
    fun value(name: String, nowMs: Long): Boolean {
        val current = status[name] ?: return false
        if (current.firstValueAtMs != null) return false
        status[name] = current.copy(firstValueAtMs = nowMs)
        return true
    }

    fun status(name: String): TelemetryKeyStatus? = status[name]

    /** A copy, in key order. */
    fun snapshot(): Map<String, TelemetryKeyStatus> = LinkedHashMap(status)
}
