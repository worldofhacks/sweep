package org.worldofhacks.sweep.bridge

import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import java.security.MessageDigest
import org.worldofhacks.sweep.bridge.node.CaptureAlignmentConfig

/** Operator-installed calibration artifact; missing means body-camera localization stays disabled. */
internal fun loadCaptureAlignment(filesDir: File): CaptureAlignmentConfig? {
    val descriptor = try {
        Os.open(File(filesDir, "capture-alignment.json").path, OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW or OsConstants.O_NONBLOCK, 0)
    } catch (error: ErrnoException) {
        if (error.errno == OsConstants.ENOENT) return null
        throw error
    }
    var closeDescriptor = true
    try {
        val info = Os.fstat(descriptor)
        require(OsConstants.S_ISREG(info.st_mode) && info.st_size <= CaptureAlignmentConfig.MAX_CONFIG_BYTES) { "capture alignment configuration must be a bounded regular file" }
        val bytes = FileInputStream(descriptor).also { closeDescriptor = false }.use { input ->
            val buffer = ByteArray(CaptureAlignmentConfig.MAX_CONFIG_BYTES + 1)
            var size = 0
            while (size < buffer.size) {
                val count = input.read(buffer, size, buffer.size - size)
                if (count < 0) break
                size += count
            }
            require(size <= CaptureAlignmentConfig.MAX_CONFIG_BYTES) { "capture alignment configuration is too large" }
            buffer.copyOf(size)
        }
        return CaptureAlignmentConfig.decode(
            Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString(),
            MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) },
        )
    } catch (error: Exception) {
        if (closeDescriptor) runCatching { Os.close(descriptor) }
        throw error
    }
}
