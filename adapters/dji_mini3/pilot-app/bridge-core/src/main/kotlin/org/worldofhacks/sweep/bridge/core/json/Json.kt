package org.worldofhacks.sweep.bridge.core.json

/**
 * Minimal JSON model with a strict parser and a canonical encoder.
 *
 * The encoder byte-matches Python's
 * `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`,
 * which is what `relay/auth.py` signs. Integers and floats are distinct types, as in
 * Python, so `1` and `1.0` never collapse into each other on the wire.
 */
sealed interface JsonValue

data class JsonObject(val fields: Map<String, JsonValue>) : JsonValue {
    val keys: Set<String>
        get() = fields.keys

    operator fun get(key: String): JsonValue? = fields[key]

    fun with(key: String, value: JsonValue): JsonObject = JsonObject(fields + (key to value))

    fun without(key: String): JsonObject = JsonObject(fields - key)
}

data class JsonArray(val items: List<JsonValue>) : JsonValue

data class JsonString(val value: String) : JsonValue

data class JsonInt(val value: Long) : JsonValue

data class JsonFloat(val value: Double) : JsonValue

data class JsonBool(val value: Boolean) : JsonValue

data object JsonNull : JsonValue

class JsonParseException(message: String) : RuntimeException(message)

object Json {
    fun parse(text: String): JsonValue = JsonParser(text).document()

    fun canonical(value: JsonValue): String = StringBuilder().also { encode(value, it) }.toString()

    fun canonicalBytes(value: JsonValue): ByteArray = canonical(value).toByteArray(Charsets.UTF_8)

    /** Build an object from Kotlin values; see [value] for the accepted types. */
    fun json(vararg pairs: Pair<String, Any?>): JsonObject {
        val fields = LinkedHashMap<String, JsonValue>(pairs.size)
        for ((key, raw) in pairs) fields[key] = value(raw)
        return JsonObject(fields)
    }

    fun value(raw: Any?): JsonValue = when (raw) {
        null -> JsonNull
        is JsonValue -> raw
        is Boolean -> JsonBool(raw)
        is Int -> JsonInt(raw.toLong())
        is Long -> JsonInt(raw)
        is Short -> JsonInt(raw.toLong())
        is Byte -> JsonInt(raw.toLong())
        is Double -> JsonFloat(raw)
        is Float -> JsonFloat(raw.toDouble())
        is String -> JsonString(raw)
        is List<*> -> JsonArray(raw.map(::value))
        is Array<*> -> JsonArray(raw.map(::value))
        is Map<*, *> -> JsonObject(
            raw.entries.associate { (key, item) ->
                (key as? String ?: throw IllegalArgumentException("object keys must be strings")) to value(item)
            },
        )
        else -> throw IllegalArgumentException("unsupported JSON value type ${raw::class.qualifiedName}")
    }

    /** Python compares `str` keys by code point; UTF-16 unit order differs for astral characters. */
    val codePointOrder: Comparator<String> = Comparator { a, b ->
        var i = 0
        var j = 0
        var result = 0
        while (result == 0 && i < a.length && j < b.length) {
            val ca = a.codePointAt(i)
            val cb = b.codePointAt(j)
            result = ca.compareTo(cb)
            i += Character.charCount(ca)
            j += Character.charCount(cb)
        }
        when {
            result != 0 -> result
            i < a.length -> 1
            j < b.length -> -1
            else -> 0
        }
    }

    private fun encode(value: JsonValue, out: StringBuilder) {
        when (value) {
            JsonNull -> out.append("null")
            is JsonBool -> out.append(if (value.value) "true" else "false")
            is JsonInt -> out.append(value.value)
            is JsonFloat -> out.append(PythonFloat.repr(value.value))
            is JsonString -> encodeString(value.value, out)
            is JsonArray -> {
                out.append('[')
                value.items.forEachIndexed { index, item ->
                    if (index > 0) out.append(',')
                    encode(item, out)
                }
                out.append(']')
            }
            is JsonObject -> {
                out.append('{')
                value.fields.keys.sortedWith(codePointOrder).forEachIndexed { index, key ->
                    if (index > 0) out.append(',')
                    encodeString(key, out)
                    out.append(':')
                    encode(value.fields.getValue(key), out)
                }
                out.append('}')
            }
        }
    }

    private fun encodeString(text: String, out: StringBuilder) {
        out.append('"')
        for (char in text) {
            when {
                char == '"' -> out.append("\\\"")
                char == '\\' -> out.append("\\\\")
                char == '\n' -> out.append("\\n")
                char == '\r' -> out.append("\\r")
                char == '\t' -> out.append("\\t")
                char == '\b' -> out.append("\\b")
                char == '\u000C' -> out.append("\\f")
                char < ' ' -> out.append("\\u").append(String.format("%04x", char.code))
                else -> out.append(char)
            }
        }
        out.append('"')
    }
}

private class JsonParser(private val text: String) {
    private var index = 0
    private var depth = 0

    fun document(): JsonValue {
        skipWhitespace()
        val value = parseValue()
        skipWhitespace()
        if (index != text.length) fail("unexpected trailing content")
        return value
    }

    private fun parseValue(): JsonValue {
        if (index >= text.length) fail("unexpected end of input")
        return when (val char = text[index]) {
            '{' -> parseObject()
            '[' -> parseArray()
            '"' -> JsonString(parseString())
            't' -> literal("true", JsonBool(true))
            'f' -> literal("false", JsonBool(false))
            'n' -> literal("null", JsonNull)
            '-', in '0'..'9' -> parseNumber()
            else -> fail("unexpected character '$char'")
        }
    }

    private fun parseObject(): JsonObject {
        enter()
        index++ // '{'
        val fields = LinkedHashMap<String, JsonValue>()
        skipWhitespace()
        if (peek() == '}') {
            index++
            depth--
            return JsonObject(fields)
        }
        while (true) {
            skipWhitespace()
            if (peek() != '"') fail("expected string key")
            val key = parseString()
            skipWhitespace()
            if (peek() != ':') fail("expected ':' after key")
            index++
            skipWhitespace()
            fields[key] = parseValue()
            skipWhitespace()
            when (peek()) {
                ',' -> index++
                '}' -> {
                    index++
                    depth--
                    return JsonObject(fields)
                }
                else -> fail("expected ',' or '}' in object")
            }
        }
    }

    private fun parseArray(): JsonArray {
        enter()
        index++ // '['
        val items = ArrayList<JsonValue>()
        skipWhitespace()
        if (peek() == ']') {
            index++
            depth--
            return JsonArray(items)
        }
        while (true) {
            skipWhitespace()
            items.add(parseValue())
            skipWhitespace()
            when (peek()) {
                ',' -> index++
                ']' -> {
                    index++
                    depth--
                    return JsonArray(items)
                }
                else -> fail("expected ',' or ']' in array")
            }
        }
    }

    private fun parseString(): String {
        index++ // opening quote
        val out = StringBuilder()
        while (true) {
            if (index >= text.length) fail("unterminated string")
            val char = text[index++]
            when {
                char == '"' -> return out.toString()
                char == '\\' -> {
                    if (index >= text.length) fail("unterminated escape")
                    when (val escape = text[index++]) {
                        '"' -> out.append('"')
                        '\\' -> out.append('\\')
                        '/' -> out.append('/')
                        'b' -> out.append('\b')
                        'f' -> out.append('\u000C')
                        'n' -> out.append('\n')
                        'r' -> out.append('\r')
                        't' -> out.append('\t')
                        'u' -> {
                            if (index + 4 > text.length) fail("truncated \\u escape")
                            val hex = text.substring(index, index + 4)
                            val code = hex.toIntOrNull(16) ?: fail("invalid \\u escape '$hex'")
                            out.append(code.toChar())
                            index += 4
                        }
                        else -> fail("invalid escape '\\$escape'")
                    }
                }
                char < ' ' -> fail("control character in string")
                else -> out.append(char)
            }
        }
    }

    private fun parseNumber(): JsonValue {
        val start = index
        if (peek() == '-') index++
        when (peek()) {
            '0' -> index++
            in '1'..'9' -> while (peek() in '0'..'9') index++
            else -> fail("invalid number")
        }
        var isFloat = false
        if (peek() == '.') {
            isFloat = true
            index++
            if (peek() !in '0'..'9') fail("invalid fraction")
            while (peek() in '0'..'9') index++
        }
        if (peek() == 'e' || peek() == 'E') {
            isFloat = true
            index++
            if (peek() == '+' || peek() == '-') index++
            if (peek() !in '0'..'9') fail("invalid exponent")
            while (peek() in '0'..'9') index++
        }
        val token = text.substring(start, index)
        return if (isFloat) {
            val value = token.toDoubleOrNull() ?: fail("invalid number '$token'")
            if (value.isInfinite()) fail("number out of double range '$token'")
            JsonFloat(value)
        } else {
            JsonInt(token.toLongOrNull() ?: fail("integer out of 64-bit range '$token'"))
        }
    }

    private fun literal(word: String, value: JsonValue): JsonValue {
        if (!text.startsWith(word, index)) fail("invalid literal")
        index += word.length
        return value
    }

    private fun enter() {
        depth++
        if (depth > MAX_DEPTH) fail("nesting deeper than $MAX_DEPTH")
    }

    private fun peek(): Char = if (index < text.length) text[index] else ' '

    private fun skipWhitespace() {
        while (index < text.length && (text[index] == ' ' || text[index] == '\n' || text[index] == '\r' || text[index] == '\t')) {
            index++
        }
    }

    private fun fail(message: String): Nothing = throw JsonParseException("$message at offset $index")

    private companion object {
        const val MAX_DEPTH = 256
    }
}
