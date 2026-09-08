package org.worldofhacks.sweep.bridge

import java.nio.file.Files
import java.nio.file.Path
import kotlin.io.path.writeText
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.io.TempDir
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.signing.Signing

class NavigationAdmissionFileTest {
    @TempDir
    lateinit var temporaryDirectory: Path

    @Test
    fun `python signed provenance fixture binds semantic pins to evidence bytes`() {
        val fixture = fixture()
        val key = fixture.text("key_utf8").toByteArray()
        val manifest = fixture.objectAt("manifest")
        val evidence = fixture.objectAt("evidence_utf8")
        evidence.fields.forEach { (name, value) -> temporaryDirectory.resolve(name).writeText((value as JsonString).value) }

        val provenance = manifest.objectAt("provenance")
        assertTrue(Signing.verify(provenance.without("signature"), provenance.text("signature"), key))
        val admission = parse(manifest, key)

        assertEquals("navigation-config-7", admission?.navigationConfigId)
        assertEquals(6, admission?.approvedEvidenceFiles?.size)
    }

    @Test
    fun `admission accepts a bounded explicit arrival hold timeout`() {
        val fixture = fixture()
        val key = fixture.text("key_utf8").toByteArray()
        val evidence = fixture.objectAt("evidence_utf8")
        evidence.fields.forEach { (name, value) -> temporaryDirectory.resolve(name).writeText((value as JsonString).value) }

        val manifest = fixture.objectAt("manifest").with("arrival_hold_timeout_ms", JsonInt(1_000))
        assertEquals(1_000, parse(manifest, key)?.arrivalHoldTimeoutMs)
    }

    @Test
    fun `provenance rejects another session device signature or artifact bytes`() {
        val fixture = fixture()
        val key = fixture.text("key_utf8").toByteArray()
        val manifest = fixture.objectAt("manifest")
        val evidence = fixture.objectAt("evidence_utf8")
        evidence.fields.forEach { (name, value) -> temporaryDirectory.resolve(name).writeText((value as JsonString).value) }

        assertThrows(IllegalArgumentException::class.java) { parse(manifest, key, session = "other-session") }
        assertThrows(IllegalArgumentException::class.java) { parse(manifest, "wrong-key".toByteArray()) }
        temporaryDirectory.resolve("map.json").writeText("substituted map artifact\n")
        assertThrows(IllegalArgumentException::class.java) { parse(manifest, key) }
    }

    @Test
    fun `advertisement requires an enabled signed navigation admission`() {
        val fixture = fixture()
        val key = fixture.text("key_utf8").toByteArray()
        val evidence = fixture.objectAt("evidence_utf8")
        evidence.fields.forEach { (name, value) -> temporaryDirectory.resolve(name).writeText((value as JsonString).value) }

        assertEquals(listOf("flight"), advertisedCapabilities(listOf("flight"), null))
        assertEquals(
            listOf("flight", "navigate"),
            advertisedCapabilities(listOf("flight"), parse(fixture.objectAt("manifest"), key)),
        )
    }

    private fun parse(
        manifest: JsonObject,
        key: ByteArray,
        session: String = "survey-session-7",
        deviceId: Int = 19,
    ) = parseNavigationAdmission(
        manifest,
        session,
        deviceId,
        key,
        readEvidence = { name, maximum ->
            val bytes = Files.readAllBytes(temporaryDirectory.resolve(name))
            require(bytes.size <= maximum)
            bytes
        },
        evidenceFile = { name -> temporaryDirectory.resolve(name).toFile() },
    )

    private fun fixture(): JsonObject = Json.parse(
        requireNotNull(requireNotNull(javaClass.classLoader).getResource("navigation/python_signed_admission_fixture.json")).readText(),
    ) as JsonObject

    private fun JsonObject.objectAt(name: String): JsonObject = this[name] as? JsonObject ?: error("$name must be an object")

    private fun JsonObject.text(name: String): String = (this[name] as? JsonString)?.value ?: error("$name must be text")
}
