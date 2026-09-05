package org.worldofhacks.sweep.bridge.core.video

enum class VideoCodec(val mime: String) {
    H264("video/avc"),
    H265("video/hevc");

    companion object {
        fun fromMime(mime: String): VideoCodec? = entries.firstOrNull { it.mime.equals(mime, ignoreCase = true) }
    }
}

/** Profile and level read from a sequence parameter set; the codec evidence Phase D records. */
data class SpsInfo(
    val codec: VideoCodec,
    val profileIdc: Int,
    val profileName: String,
    val levelIdc: Int,
    val level: String,
    val tier: String?,
    val constraintFlags: Int?,
)

class SpsParseException(message: String) : RuntimeException(message)

/**
 * Reads profile and level from an H.264 (type 7) or H.265 (type 33) SPS NAL unit. Accepts a
 * bare NAL, an Annex B buffer with start codes, or a codec-specific-data buffer holding
 * VPS/SPS/PPS together (`MediaFormat` `csd-0`), and strips emulation-prevention bytes first.
 */
object SpsParser {
    private const val H264_SPS = 7
    private const val H265_SPS = 33

    fun parse(data: ByteArray, codecHint: VideoCodec? = null): SpsInfo {
        for (nal in splitNalUnits(data)) {
            val codec = classify(nal) ?: continue
            if (codecHint != null && codecHint != codec) continue
            return when (codec) {
                VideoCodec.H264 -> parseH264(unescape(nal))
                VideoCodec.H265 -> parseH265(unescape(nal))
            }
        }
        val wanted = codecHint?.name ?: "H.264 or H.265"
        throw SpsParseException("no $wanted SPS NAL unit found in ${data.size} bytes")
    }

    private fun classify(nal: ByteArray): VideoCodec? {
        if (nal.isEmpty()) return null
        val first = nal[0].toInt() and 0xFF
        if (first and 0x80 != 0) return null // forbidden_zero_bit
        if (nal.size >= 2) {
            val hevcType = (first and 0x7E) shr 1
            val second = nal[1].toInt() and 0xFF
            val layerIdHigh = first and 0x01
            val temporalIdPlus1 = second and 0x07
            if (hevcType == H265_SPS && layerIdHigh == 0 && (second and 0xF8) == 0 && temporalIdPlus1 == 1) {
                return VideoCodec.H265
            }
        }
        if (first and 0x1F == H264_SPS) return VideoCodec.H264
        return null
    }

    /** Splits on 00 00 01 start codes (with an optional leading zero); no start code means one NAL. */
    private fun splitNalUnits(data: ByteArray): List<ByteArray> {
        val starts = ArrayList<Int>()
        var i = 0
        while (i + 2 < data.size) {
            if (data[i] == ZERO && data[i + 1] == ZERO && data[i + 2] == ONE) {
                starts.add(i + 3)
                i += 3
            } else {
                i++
            }
        }
        if (starts.isEmpty()) return listOf(data)
        val units = ArrayList<ByteArray>(starts.size)
        for ((index, start) in starts.withIndex()) {
            var end = if (index + 1 < starts.size) starts[index + 1] - 3 else data.size
            // A four-byte start code leaves a trailing zero on the previous unit.
            while (end > start && data[end - 1] == ZERO && index + 1 < starts.size) end--
            if (end > start) units.add(data.copyOfRange(start, end))
        }
        return units
    }

    private fun unescape(nal: ByteArray): ByteArray {
        val out = ByteArray(nal.size)
        var size = 0
        var zeros = 0
        for (byte in nal) {
            if (zeros >= 2 && byte == THREE) {
                zeros = 0
                continue
            }
            out[size++] = byte
            zeros = if (byte == ZERO) zeros + 1 else 0
        }
        return out.copyOf(size)
    }

    private fun parseH264(rbsp: ByteArray): SpsInfo {
        if (rbsp.size < 4) throw SpsParseException("H.264 SPS shorter than profile_idc, constraint flags and level_idc")
        val profile = rbsp[1].toInt() and 0xFF
        val constraints = rbsp[2].toInt() and 0xFF
        val level = rbsp[3].toInt() and 0xFF
        return SpsInfo(
            codec = VideoCodec.H264,
            profileIdc = profile,
            profileName = h264ProfileName(profile, constraints),
            levelIdc = level,
            level = h264Level(level, constraints, profile),
            tier = null,
            constraintFlags = constraints,
        )
    }

    private fun parseH265(rbsp: ByteArray): SpsInfo {
        // 2-byte NAL header, 1 byte of vps id / max sub layers / temporal nesting, then
        // profile_tier_level: profile byte, 4 compatibility bytes, 6 constraint bytes, level.
        if (rbsp.size < 15) throw SpsParseException("H.265 SPS shorter than profile_tier_level")
        val profileByte = rbsp[3].toInt() and 0xFF
        val tier = (profileByte shr 5) and 0x01
        val profile = profileByte and 0x1F
        val level = rbsp[14].toInt() and 0xFF
        return SpsInfo(
            codec = VideoCodec.H265,
            profileIdc = profile,
            profileName = h265ProfileName(profile),
            levelIdc = level,
            level = "${level / 30}.${(level % 30) / 3}",
            tier = if (tier == 1) "high" else "main",
            constraintFlags = null,
        )
    }

    private fun h264ProfileName(profile: Int, constraints: Int): String = when (profile) {
        66 -> if (constraints and CONSTRAINT_SET1 != 0) "Constrained Baseline" else "Baseline"
        77 -> "Main"
        88 -> "Extended"
        100 -> if (constraints and (CONSTRAINT_SET4 or CONSTRAINT_SET5) == (CONSTRAINT_SET4 or CONSTRAINT_SET5)) "Constrained High" else "High"
        110 -> "High 10"
        122 -> "High 4:2:2"
        244 -> "High 4:4:4 Predictive"
        44 -> "CAVLC 4:4:4 Intra"
        83 -> "Scalable Baseline"
        86 -> "Scalable High"
        118 -> "Multiview High"
        128 -> "Stereo High"
        else -> "profile_idc $profile"
    }

    private fun h264Level(levelIdc: Int, constraints: Int, profile: Int): String {
        val level1b = levelIdc == 9 ||
            (levelIdc == 11 && constraints and CONSTRAINT_SET3 != 0 && profile in setOf(66, 77, 88))
        return if (level1b) "1b" else "${levelIdc / 10}.${levelIdc % 10}"
    }

    private fun h265ProfileName(profile: Int): String = when (profile) {
        1 -> "Main"
        2 -> "Main 10"
        3 -> "Main Still Picture"
        4 -> "Format Range Extensions"
        5 -> "High Throughput"
        9 -> "Screen Content Coding"
        else -> "profile_idc $profile"
    }

    private const val ZERO: Byte = 0
    private const val ONE: Byte = 1
    private const val THREE: Byte = 3
    private const val CONSTRAINT_SET1 = 0x40
    private const val CONSTRAINT_SET3 = 0x10
    private const val CONSTRAINT_SET4 = 0x08
    private const val CONSTRAINT_SET5 = 0x04
}
