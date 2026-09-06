package org.worldofhacks.sweep.bridge.core.frames

import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonFloat
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonNull
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.json.JsonValue

/** A typed failure while decoding an untrusted relay frame; mirrors `relay.contracts.ContractError`. */
class ContractError(val code: String, val detail: String) : RuntimeException(detail)

/** Field validators with the same rules and messages as the private helpers in `relay/contracts.py`. */
internal object Fields {
    const val MAX_STRING = 512
    const val MAX_CAPABILITY_LIST_ITEMS = 64
    const val MAX_CAPABILITY_ITEM_UTF8_BYTES = 512
    const val MAX_CAPABILITY_LIST_CANONICAL_BYTES = 8 * 1024
    const val PROTOCOL_VERSION = 1L

    fun exact(value: JsonObject, fields: Set<String>, code: String) {
        if (value.keys != fields) throw ContractError(code, "frame fields do not match the v1 contract")
    }

    fun envelope(value: JsonObject, expectedType: String, code: String) {
        if (value["v"] != JsonInt(PROTOCOL_VERSION)) throw ContractError(code, "v must be integer 1")
        nonNegativeInt(value["t"], "t", code)
        if (value["type"] != JsonString(expectedType)) throw ContractError(code, "type must be $expectedType")
        nonEmptyString(value["event_id"], "event_id", code)
        nonEmptyString(value["session"], "session", code)
    }

    fun nonEmptyString(value: JsonValue?, field: String, code: String): String {
        if (
            value !is JsonString ||
            value.value.isEmpty() ||
            value.value.codePointCount(0, value.value.length) > MAX_STRING
        ) {
            throw ContractError(code, "$field must be a non-empty string of at most $MAX_STRING chars")
        }
        return value.value
    }

    fun boundedStateText(
        value: JsonValue?,
        field: String,
        code: String,
        maximumUtf8Bytes: Int = MAX_CAPABILITY_ITEM_UTF8_BYTES,
    ): String {
        val result = nonEmptyString(value, field, code)
        if (
            !isCanonicalPrintable(result, MAX_STRING) ||
            result.toByteArray(Charsets.UTF_8).size > maximumUtf8Bytes
        ) {
            throw ContractError(
                code,
                "$field must be canonical printable text of at most " +
                    "$maximumUtf8Bytes UTF-8 bytes",
            )
        }
        return result
    }

    fun nullableString(value: JsonValue?, field: String, code: String, machineReadable: Boolean = false): String? {
        if (value == null || value == JsonNull) return null
        val result = nonEmptyString(value, field, code)
        if (machineReadable && !isMachineCode(result)) throw ContractError(code, "$field must be snake_case")
        return result
    }

    fun exactString(value: JsonValue?, field: String, expected: String, code: String) {
        if (value != JsonString(expected)) throw ContractError(code, "$field must be $expected")
    }

    fun nonNegativeInt(value: JsonValue?, field: String, code: String): Long {
        if (value !is JsonInt || value.value < 0) throw ContractError(code, "$field must be a non-negative integer")
        return value.value
    }

    /** Any 64-bit integer, sign included (`relay.contracts._integer`). */
    fun integer(value: JsonValue?, field: String, code: String): Long {
        if (value !is JsonInt) throw ContractError(code, "$field must be an integer")
        return value.value
    }

    /** A finite number in `[0, 360)` (`relay.contracts._azimuth`). */
    fun azimuth(value: JsonValue?, field: String, code: String): Double {
        val result = finiteNumber(value, field, code)
        if (result < 0.0 || result >= 360.0) throw ContractError(code, "$field azimuth must be between 0 and 360")
        return result
    }

    fun positiveInt(value: JsonValue?, field: String, code: String): Long {
        val result = nonNegativeInt(value, field, code)
        if (result == 0L) throw ContractError(code, "$field must be a positive integer")
        return result
    }

    fun nonNegativeInt32(value: JsonValue?, field: String, code: String): Int {
        val result = nonNegativeInt(value, field, code)
        if (result > Int.MAX_VALUE) throw ContractError(code, "$field exceeds the 32-bit range")
        return result.toInt()
    }

    fun positiveInt32(value: JsonValue?, field: String, code: String): Int {
        val result = positiveInt(value, field, code)
        if (result > Int.MAX_VALUE) throw ContractError(code, "$field exceeds the 32-bit range")
        return result.toInt()
    }

    fun finiteNumber(value: JsonValue?, field: String, code: String): Double = when (value) {
        is JsonInt -> value.value.toDouble()
        is JsonFloat -> if (value.value.isFinite()) value.value else throw ContractError(code, "$field must be a finite number")
        else -> throw ContractError(code, "$field must be a finite number")
    }

    fun unitInterval(value: JsonValue?, field: String, code: String): Double {
        val result = finiteNumber(value, field, code)
        if (result < 0.0 || result > 1.0) throw ContractError(code, "$field must be between 0 and 1")
        return result
    }

    fun boolean(value: JsonValue?, field: String, code: String): Boolean {
        if (value !is JsonBool) throw ContractError(code, "$field must be a boolean")
        return value.value
    }

    fun obj(value: JsonValue?, field: String, code: String): JsonObject {
        if (value !is JsonObject) throw ContractError(code, "$field must be an object")
        return value
    }

    fun stringList(value: JsonValue?, field: String, code: String, allowEmpty: Boolean): List<String> {
        if (value !is JsonArray) throw ContractError(code, "$field must be a list")
        if (value.items.size > MAX_CAPABILITY_LIST_ITEMS) {
            throw ContractError(code, "$field may contain at most $MAX_CAPABILITY_LIST_ITEMS items")
        }
        val result = value.items.map { item ->
            val text = (item as? JsonString)?.value
                ?: throw ContractError(code, "$field items must be strings")
            if (
                !isCanonicalPrintable(text, MAX_CAPABILITY_ITEM_UTF8_BYTES) ||
                text.toByteArray(Charsets.UTF_8).size > MAX_CAPABILITY_ITEM_UTF8_BYTES
            ) {
                throw ContractError(
                    code,
                    "$field items must be canonical printable strings of at most " +
                        "$MAX_CAPABILITY_ITEM_UTF8_BYTES UTF-8 bytes",
                )
            }
            text
        }
        if (result.isEmpty() && !allowEmpty) throw ContractError(code, "$field may not be empty")
        if (result.toSet().size != result.size) throw ContractError(code, "$field may not contain duplicates")
        if (Json.canonicalBytes(Json.value(result)).size > MAX_CAPABILITY_LIST_CANONICAL_BYTES) {
            throw ContractError(
                code,
                "$field canonical JSON may contain at most " +
                    "$MAX_CAPABILITY_LIST_CANONICAL_BYTES UTF-8 bytes",
            )
        }
        return result
    }

    private fun requireStringList(value: List<String>, field: String, allowEmpty: Boolean) {
        require(value.size <= MAX_CAPABILITY_LIST_ITEMS) {
            "$field may contain at most $MAX_CAPABILITY_LIST_ITEMS items"
        }
        require(value.isNotEmpty() || allowEmpty) { "$field may not be empty" }
        require(value.toSet().size == value.size) { "$field may not contain duplicates" }
        require(
            value.all {
                isCanonicalPrintable(it, MAX_CAPABILITY_ITEM_UTF8_BYTES) &&
                    it.toByteArray(Charsets.UTF_8).size <= MAX_CAPABILITY_ITEM_UTF8_BYTES
            },
        ) {
            "$field items must be canonical printable strings of at most " +
                "$MAX_CAPABILITY_ITEM_UTF8_BYTES UTF-8 bytes"
        }
        require(Json.canonicalBytes(Json.value(value)).size <= MAX_CAPABILITY_LIST_CANONICAL_BYTES) {
            "$field canonical JSON may contain at most " +
                "$MAX_CAPABILITY_LIST_CANONICAL_BYTES UTF-8 bytes"
        }
    }

    fun validatedStringListSnapshot(value: List<String>, field: String, allowEmpty: Boolean): List<String> {
        val snapshot = boundedListSnapshot(value, MAX_CAPABILITY_LIST_ITEMS, field)
        requireStringList(snapshot, field, allowEmpty)
        return snapshot
    }

    fun <T> boundedListSnapshot(value: List<T>, maximumItems: Int, field: String): List<T> {
        val expectedSize = value.size
        require(expectedSize <= maximumItems) { "$field may contain at most $maximumItems items" }
        val snapshot = ArrayList<T>(expectedSize)
        for (item in value) {
            require(snapshot.size < maximumItems) { "$field may contain at most $maximumItems items" }
            snapshot.add(item)
        }
        return snapshot
    }

    fun requireBoundedStateText(
        value: String,
        field: String,
        maximumUtf8Bytes: Int = MAX_CAPABILITY_ITEM_UTF8_BYTES,
    ) {
        require(
            isCanonicalPrintable(value, MAX_STRING) &&
                value.toByteArray(Charsets.UTF_8).size <= maximumUtf8Bytes,
        ) {
            "$field must be canonical printable text of at most " +
                "$maximumUtf8Bytes UTF-8 bytes"
        }
    }

    fun intList(value: JsonValue?, field: String, code: String): List<Int> {
        if (value !is JsonArray) throw ContractError(code, "$field must be a list")
        return value.items.map {
            if (it !is JsonInt || it.value < Int.MIN_VALUE || it.value > Int.MAX_VALUE) {
                throw ContractError(code, "$field must contain integers")
            }
            it.value.toInt()
        }
    }

    fun isMachineCode(value: String): Boolean =
        value.isNotEmpty() && value.all { it in 'a'..'z' || it in '0'..'9' || it == '_' }

    /** Mirrors Python's trimmed `str.isprintable()` identifier rule. */
    fun isCanonicalPrintable(value: String, maxLength: Int): Boolean {
        if (value.isEmpty() || value != value.trim() || value.codePointCount(0, value.length) > maxLength) return false
        var index = 0
        while (index < value.length) {
            val codePoint = value.codePointAt(index)
            val nonPrintable = codePoint != 0x20 && when (Character.getType(codePoint)) {
                Character.CONTROL.toInt(),
                Character.FORMAT.toInt(),
                Character.SURROGATE.toInt(),
                Character.PRIVATE_USE.toInt(),
                Character.UNASSIGNED.toInt(),
                Character.LINE_SEPARATOR.toInt(),
                Character.PARAGRAPH_SEPARATOR.toInt(),
                Character.SPACE_SEPARATOR.toInt(),
                -> true
                else -> false
            }
            if (nonPrintable) return false
            index += Character.charCount(codePoint)
        }
        return true
    }
}
