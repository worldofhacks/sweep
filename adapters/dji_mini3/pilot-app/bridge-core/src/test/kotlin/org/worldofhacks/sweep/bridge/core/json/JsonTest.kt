package org.worldofhacks.sweep.bridge.core.json

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.Fixtures
import org.worldofhacks.sweep.bridge.core.Fixtures.any
import org.worldofhacks.sweep.bridge.core.Fixtures.string

class JsonTest {
    @Test
    fun `canonical encoding byte-matches python json dumps for every generated vector`() {
        val cases = Fixtures.cases("canonical_json.json")
        assertTrue(cases.size >= 20, "expected a rich vector set, got ${cases.size}")
        for (case in cases) {
            val expected = case.string("canonical")
            val actual = Json.canonical(case.any("value"))
            assertEquals(expected, actual, "case ${case.string("name")}")
            assertEquals(
                expected.toByteArray(Charsets.UTF_8).toList(),
                Json.canonicalBytes(case.any("value")).toList(),
                "bytes of case ${case.string("name")}",
            )
        }
    }

    @Test
    fun `float repr follows python rules`() {
        val expectations = mapOf(
            0.0 to "0.0",
            -0.0 to "-0.0",
            1.0 to "1.0",
            0.1 to "0.1",
            -2.5 to "-2.5",
            100.0 to "100.0",
            1e15 to "1000000000000000.0",
            1e16 to "1e+16",
            1.5e16 to "1.5e+16",
            0.0001 to "0.0001",
            0.00001 to "1e-05",
            1.25e-7 to "1.25e-07",
            123456789.123 to "123456789.123",
            5e-324 to "5e-324",
            1.7976931348623157e308 to "1.7976931348623157e+308",
            0.30000000000000004 to "0.30000000000000004",
            9007199254740993.0 to "9007199254740992.0",
        )
        for ((value, text) in expectations) {
            assertEquals(text, PythonFloat.repr(value), "repr($value)")
        }
    }

    @Test
    fun `nan and infinity are refused like allow_nan false`() {
        assertThrows(IllegalArgumentException::class.java) { Json.canonical(JsonFloat(Double.NaN)) }
        assertThrows(IllegalArgumentException::class.java) {
            Json.canonical(JsonFloat(Double.POSITIVE_INFINITY))
        }
    }

    @Test
    fun `keys sort by unicode code point not utf16 unit`() {
        val astral = String(Character.toChars(0x1D51E)) // U+1D51E, surrogate pair D835 DD1E
        val fullwidth = "ｚ" // U+FF5A sorts after U+D835 by UTF-16 unit but before U+1D51E by code point
        val encoded = Json.canonical(Json.json(astral to 1, fullwidth to 2))
        assertEquals("{\"$fullwidth\":2,\"$astral\":1}", encoded)
    }

    @Test
    fun `parser round-trips numbers strings and structure`() {
        val parsed = Json.parse(
            """
            {"int": -42, "big": 9223372036854775807, "float": 1.5e3, "exp": 1E-2, "neg0": -0.0,
             "s": "a\"b\\c\né🚀", "t": true, "f": false, "n": null,
             "list": [1, 2.0, "x", [], {}], "obj": {"z": 1, "a": 2}}
            """.trimIndent(),
        ) as JsonObject
        assertEquals(JsonInt(-42), parsed["int"])
        assertEquals(JsonInt(Long.MAX_VALUE), parsed["big"])
        assertEquals(JsonFloat(1500.0), parsed["float"])
        assertEquals(JsonFloat(0.01), parsed["exp"])
        assertEquals(JsonFloat(-0.0), parsed["neg0"])
        assertEquals(JsonString("a\"b\\c\né🚀"), parsed["s"])
        assertEquals(JsonBool(true), parsed["t"])
        assertEquals(JsonBool(false), parsed["f"])
        assertEquals(JsonNull, parsed["n"])
        assertEquals(
            JsonArray(listOf(JsonInt(1), JsonFloat(2.0), JsonString("x"), JsonArray(emptyList()), JsonObject(emptyMap()))),
            parsed["list"],
        )
        assertEquals("{\"a\":2,\"z\":1}", Json.canonical(parsed["obj"]!!))
    }

    @Test
    fun `parser rejects malformed input`() {
        for (text in listOf("", "{", "[1,]", "{\"a\":}", "01", "1.", "\"unterminated", "tru", "{\"a\":1} x", "NaN", "-")) {
            assertThrows(JsonParseException::class.java, { Json.parse(text) }, "input <$text>")
        }
    }

    @Test
    fun `parser rejects integers outside long range`() {
        assertThrows(JsonParseException::class.java) { Json.parse("9223372036854775808") }
    }

    @Test
    fun `builder converts kotlin values`() {
        val built = Json.json(
            "i" to 1,
            "l" to 2L,
            "d" to 3.0,
            "f" to 4.5f,
            "s" to "s",
            "b" to true,
            "n" to null,
            "list" to listOf(1, "two"),
            "map" to mapOf("k" to 1),
        )
        assertEquals(
            "{\"b\":true,\"d\":3.0,\"f\":4.5,\"i\":1,\"l\":2,\"list\":[1,\"two\"],\"map\":{\"k\":1},\"n\":null,\"s\":\"s\"}",
            Json.canonical(built),
        )
    }
}
