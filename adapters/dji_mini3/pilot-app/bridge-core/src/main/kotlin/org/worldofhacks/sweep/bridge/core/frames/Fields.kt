package org.worldofhacks.sweep.bridge.core.frames

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
        if (value !is JsonString || value.value.isEmpty() || value.value.length > MAX_STRING) {
            throw ContractError(code, "$field must be a non-empty string of at most $MAX_STRING chars")
        }
        return value.value
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
        val result = value.items.map { nonEmptyString(it, field, code) }
        if (result.isEmpty() && !allowEmpty) throw ContractError(code, "$field may not be empty")
        if (result.toSet().size != result.size) throw ContractError(code, "$field may not contain duplicates")
        return result
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
}
