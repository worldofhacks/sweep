package org.worldofhacks.sweep.bridge.publish.whip

import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import okhttp3.Credentials

/** A media-only credential. Its secret is deliberately excluded from generated strings. */
class MediaCredential(val username: String, internal val password: String) {
    init {
        require(username.isNotBlank()) { "media username must not be blank" }
        require(password.isNotEmpty()) { "media password must not be empty" }
    }

    internal fun authorization(): String = Credentials.basic(username, password, Charsets.UTF_8)

    override fun toString(): String = "MediaCredential(username=$username, password=<redacted>)"

    companion object {
        private const val ALGORITHM = "HmacSHA256"
        private const val CONTEXT = "sweep-media-publish-v1:drone"

        /** Derive a least-privilege media password; the relay/control token never crosses HTTP Basic. */
        fun publisher(droneId: Int, adapterKey: ByteArray): MediaCredential {
            require(droneId > 0) { "drone id must be positive" }
            require(adapterKey.isNotEmpty()) { "adapter key must not be empty" }
            val mac = Mac.getInstance(ALGORITHM)
            mac.init(SecretKeySpec(adapterKey, ALGORITHM))
            val password = mac.doFinal("$CONTEXT$droneId".toByteArray(Charsets.UTF_8)).toHex()
            return MediaCredential("drone$droneId", password)
        }

        private fun ByteArray.toHex(): String = joinToString("") { byte -> "%02x".format(byte.toInt() and 0xFF) }
    }
}
