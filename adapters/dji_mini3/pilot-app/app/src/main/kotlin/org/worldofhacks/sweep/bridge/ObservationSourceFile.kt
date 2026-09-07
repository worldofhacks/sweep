package org.worldofhacks.sweep.bridge

import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import java.io.File
import java.io.FileInputStream
import java.nio.ByteBuffer
import org.worldofhacks.sweep.bridge.node.ObservationSourceConfig

internal fun loadObservationSource(filesDir: File): ObservationSourceConfig? {
    val descriptor = try {
        Os.open(File(filesDir, "observation-source.json").path, OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW or OsConstants.O_NONBLOCK, 0)
    } catch (error: ErrnoException) {
        if (error.errno == OsConstants.ENOENT) return null
        throw error
    }
    var closeDescriptor = true
    try {
        val info = Os.fstat(descriptor)
        require(OsConstants.S_ISREG(info.st_mode) && info.st_size <= ObservationSourceConfig.MAX_CONFIG_BYTES) {
            "observation source configuration must be a bounded regular file"
        }
        val input = FileInputStream(descriptor)
        closeDescriptor = false
        val bytes = input.use {
            val buffer = ByteArray(ObservationSourceConfig.MAX_CONFIG_BYTES + 1)
            var size = 0
            while (size < buffer.size) {
                val count = it.read(buffer, size, buffer.size - size)
                if (count < 0) break
                size += count
            }
            require(size <= ObservationSourceConfig.MAX_CONFIG_BYTES) { "observation source configuration is too large" }
            buffer.copyOf(size)
        }
        return ObservationSourceConfig.decode(Charsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString())
    } catch (error: Exception) {
        if (closeDescriptor) runCatching { Os.close(descriptor) }
        throw error
    }
}
