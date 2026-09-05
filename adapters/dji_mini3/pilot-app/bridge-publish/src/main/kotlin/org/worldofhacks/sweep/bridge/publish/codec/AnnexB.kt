package org.worldofhacks.sweep.bridge.publish.codec

/**
 * Byte-stream helpers for the passthrough path. WebRTC's H.264 packetizer wants Annex B
 * (start-code delimited NAL units); the SDK's `onReceiveStream` buffers are expected to be
 * Annex B already, and a length-prefixed (AVCC) buffer is converted rather than dropped.
 */
object AnnexB {
    private const val ZERO: Byte = 0
    private const val ONE: Byte = 1

    /** True when the buffer opens with a 3- or 4-byte start code. */
    fun startsWithStartCode(data: ByteArray, offset: Int = 0, length: Int = data.size - offset): Boolean {
        if (length >= 3 && data[offset] == ZERO && data[offset + 1] == ZERO && data[offset + 2] == ONE) return true
        return length >= 4 && data[offset] == ZERO && data[offset + 1] == ZERO && data[offset + 2] == ZERO && data[offset + 3] == ONE
    }

    /** True when the buffer parses as a sequence of 4-byte big-endian length-prefixed NAL units. */
    fun looksLikeAvcc(data: ByteArray, offset: Int = 0, length: Int = data.size - offset): Boolean {
        var position = offset
        val end = offset + length
        var units = 0
        while (position + 4 <= end) {
            val size = readLength(data, position)
            if (size <= 0 || position + 4 + size > end) return false
            position += 4 + size
            units++
        }
        return units > 0 && position == end
    }

    /** Copies [length] bytes from [offset] as Annex B, converting AVCC length prefixes to start codes. */
    fun normalize(data: ByteArray, offset: Int = 0, length: Int = data.size - offset): ByteArray {
        if (startsWithStartCode(data, offset, length) || !looksLikeAvcc(data, offset, length)) {
            return data.copyOfRange(offset, offset + length)
        }
        val out = ByteArray(length)
        var position = offset
        var written = 0
        val end = offset + length
        while (position + 4 <= end) {
            val size = readLength(data, position)
            out[written] = 0
            out[written + 1] = 0
            out[written + 2] = 0
            out[written + 3] = 1
            written += 4
            System.arraycopy(data, position + 4, out, written, size)
            written += size
            position += 4 + size
        }
        return out
    }

    /** NAL units of an Annex B buffer as (offset, length) pairs, without their start codes. */
    fun nalUnits(annexB: ByteArray): List<IntRange> {
        val starts = ArrayList<Int>()
        var i = 0
        while (i + 2 < annexB.size) {
            if (annexB[i] == ZERO && annexB[i + 1] == ZERO && annexB[i + 2] == ONE) {
                starts.add(i + 3)
                i += 3
            } else {
                i++
            }
        }
        if (starts.isEmpty()) return if (annexB.isEmpty()) emptyList() else listOf(annexB.indices)
        val units = ArrayList<IntRange>(starts.size)
        for ((index, start) in starts.withIndex()) {
            var end = if (index + 1 < starts.size) starts[index + 1] - 3 else annexB.size
            while (end > start && annexB[end - 1] == ZERO && index + 1 < starts.size) end--
            if (end > start) units.add(start until end)
        }
        return units
    }

    /** `nal_unit_type` of an H.264 NAL unit's first byte. */
    fun h264NalType(firstByte: Byte): Int = firstByte.toInt() and 0x1F

    /** Removes emulation-prevention bytes (00 00 03) so the exp-Golomb fields can be read. */
    fun unescape(nal: ByteArray, range: IntRange): ByteArray {
        val out = ByteArray(range.last - range.first + 1)
        var size = 0
        var zeros = 0
        for (i in range) {
            val byte = nal[i]
            if (zeros >= 2 && byte == THREE) {
                zeros = 0
                continue
            }
            out[size++] = byte
            zeros = if (byte == ZERO) zeros + 1 else 0
        }
        return out.copyOf(size)
    }

    private fun readLength(data: ByteArray, position: Int): Int =
        ((data[position].toInt() and 0xFF) shl 24) or
            ((data[position + 1].toInt() and 0xFF) shl 16) or
            ((data[position + 2].toInt() and 0xFF) shl 8) or
            (data[position + 3].toInt() and 0xFF)

    private const val THREE: Byte = 3
}

/**
 * The slice-level facts the codec gate needs from an H.264 access unit: whether it carries
 * B slices (browsers' WebRTC decoders do not reorder, so MediaMTX cannot pass them) and its
 * parameter sets, which are cached and prepended to keyframes that arrive without them.
 */
object H264Slices {
    const val NAL_NON_IDR = 1
    const val NAL_IDR = 5
    const val NAL_SPS = 7
    const val NAL_PPS = 8

    /** True when any coded slice in the buffer has `slice_type` B (1 or 6). */
    fun containsBSlice(annexB: ByteArray): Boolean {
        for (range in AnnexB.nalUnits(annexB)) {
            val type = AnnexB.h264NalType(annexB[range.first])
            if (type !in NAL_NON_IDR..NAL_IDR) continue
            val rbsp = AnnexB.unescape(annexB, range)
            if (rbsp.size < 2) continue
            val reader = BitReader(rbsp, startByte = 1)
            reader.readUe() ?: continue // first_mb_in_slice
            val sliceType = reader.readUe() ?: continue
            if (sliceType % 5 == 1) return true
        }
        return false
    }

    fun hasSps(annexB: ByteArray): Boolean =
        AnnexB.nalUnits(annexB).any { AnnexB.h264NalType(annexB[it.first]) == NAL_SPS }

    /** SPS and PPS NAL units (with 4-byte start codes) found in the buffer, in order. */
    fun parameterSets(annexB: ByteArray): ByteArray? {
        val units = AnnexB.nalUnits(annexB).filter { AnnexB.h264NalType(annexB[it.first]) == NAL_SPS || AnnexB.h264NalType(annexB[it.first]) == NAL_PPS }
        if (units.isEmpty()) return null
        val out = java.io.ByteArrayOutputStream()
        for (range in units) {
            out.write(START_CODE)
            out.write(annexB, range.first, range.last - range.first + 1)
        }
        return out.toByteArray()
    }

    /** The buffer with [parameterSets] prepended; used when a keyframe arrives without its SPS. */
    fun prependParameterSets(annexB: ByteArray, parameterSets: ByteArray): ByteArray = parameterSets + annexB

    private val START_CODE = byteArrayOf(0, 0, 0, 1)

    /** MSB-first bit reader with unsigned exp-Golomb; returns null on running out of bits. */
    private class BitReader(private val data: ByteArray, startByte: Int) {
        private var position = startByte * 8

        fun readBit(): Int? {
            val byteIndex = position shr 3
            if (byteIndex >= data.size) return null
            val bit = (data[byteIndex].toInt() shr (7 - (position and 7))) and 1
            position++
            return bit
        }

        fun readUe(): Int? {
            var leadingZeros = 0
            while (true) {
                val bit = readBit() ?: return null
                if (bit == 1) break
                leadingZeros++
                if (leadingZeros > 31) return null
            }
            var value = 0
            repeat(leadingZeros) {
                val bit = readBit() ?: return null
                value = (value shl 1) or bit
            }
            return (1 shl leadingZeros) - 1 + value
        }
    }
}
