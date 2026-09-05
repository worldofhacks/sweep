package org.worldofhacks.sweep.bridge.core.signing

import java.security.MessageDigest
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

/** Detached HMAC-SHA256 over canonical JSON, the scheme `relay/auth.py` uses for membership. */
object Signing {
    private const val ALGORITHM = "HmacSHA256"
    private const val HEX_LENGTH = 64

    fun sign(unsigned: JsonObject, key: ByteArray): String {
        require(key.isNotEmpty()) { "signing key must not be empty" }
        val mac = Mac.getInstance(ALGORITHM)
        mac.init(SecretKeySpec(key, ALGORITHM))
        return mac.doFinal(Json.canonicalBytes(unsigned)).toHex()
    }

    fun verify(unsigned: JsonObject, signature: String, key: ByteArray): Boolean {
        if (!isWellFormed(signature)) return false
        val expected = sign(unsigned, key)
        return MessageDigest.isEqual(signature.toByteArray(Charsets.US_ASCII), expected.toByteArray(Charsets.US_ASCII))
    }

    /** Lowercase hex of exactly 32 bytes, as the relay checks before comparing. */
    fun isWellFormed(signature: String): Boolean =
        signature.length == HEX_LENGTH && signature.all { it in '0'..'9' || it in 'a'..'f' }

    private fun ByteArray.toHex(): String = joinToString("") { byte -> "%02x".format(byte.toInt() and 0xFF) }
}
