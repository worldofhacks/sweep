package org.worldofhacks.sweep.bridge.core.json

import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode

/**
 * Renders a double exactly like CPython's `float.__repr__`, which `json.dumps` uses:
 * the shortest digit string that round-trips, fixed notation while the decimal point
 * position is in (-4, 16], exponent notation otherwise (`1e-05`, `1e+16`), and a
 * trailing `.0` on integral values.
 */
object PythonFloat {
    fun repr(value: Double): String {
        require(!value.isNaN() && !value.isInfinite()) { "Out of range float values are not JSON compliant" }
        if (value == 0.0) return if (1.0 / value < 0) "-0.0" else "0.0"
        val negative = value < 0
        val (digits, decpt) = shortestDigits(Math.abs(value))
        val body = if (decpt <= -4 || decpt > 16) exponent(digits, decpt) else fixed(digits, decpt)
        return if (negative) "-$body" else body
    }

    /**
     * Returns the shortest significant-digit string `d1..dn` (no leading or trailing zeros)
     * and `decpt` such that `0.d1..dn * 10^decpt` round-trips to [magnitude]. The nearest
     * candidate at each precision is tried first so ties resolve the way dtoa mode 0 does;
     * the directed roundings cover the asymmetric interval at powers of two.
     */
    private fun shortestDigits(magnitude: Double): Pair<String, Int> {
        val exact = BigDecimal(magnitude)
        for (precision in 1..17) {
            for (mode in ROUNDING_MODES) {
                val candidate = exact.round(MathContext(precision, mode))
                if (candidate.toDouble() == magnitude) {
                    val stripped = candidate.stripTrailingZeros()
                    val unscaled = stripped.unscaledValue().toString()
                    return unscaled to (unscaled.length - stripped.scale())
                }
            }
        }
        error("17 significant digits always round-trip a double")
    }

    private fun fixed(digits: String, decpt: Int): String = when {
        decpt <= 0 -> "0." + "0".repeat(-decpt) + digits
        decpt < digits.length -> digits.substring(0, decpt) + "." + digits.substring(decpt)
        else -> digits + "0".repeat(decpt - digits.length) + ".0"
    }

    private fun exponent(digits: String, decpt: Int): String {
        val mantissa = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
        val exp = decpt - 1
        val sign = if (exp < 0) "-" else "+"
        return mantissa + "e" + sign + Math.abs(exp).toString().padStart(2, '0')
    }

    private val ROUNDING_MODES = listOf(RoundingMode.HALF_EVEN, RoundingMode.DOWN, RoundingMode.UP)
}
