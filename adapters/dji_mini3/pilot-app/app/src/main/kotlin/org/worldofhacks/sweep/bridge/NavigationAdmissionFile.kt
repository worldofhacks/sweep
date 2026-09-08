package org.worldofhacks.sweep.bridge

import android.system.Os
import android.system.OsConstants
import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.core.signing.Signing
import org.worldofhacks.sweep.bridge.node.NavigationAdmissionConfig

internal fun loadNavigationAdmission(
    filesDir: File,
    session: String,
    deviceId: Int,
    key: ByteArray,
): NavigationAdmissionConfig? {
    val configBytes = privateBytes(filesDir, "navigation-admission.json", 16 * 1024) ?: return null
    val json = Json.parse(configBytes.toString(Charsets.UTF_8)) as? JsonObject ?: error("navigation admission configuration must be an object")
    return parseNavigationAdmission(
        json,
        session,
        deviceId,
        key,
        readEvidence = { name, maximum -> privateBytes(filesDir, name, maximum) },
        evidenceFile = { name -> File(filesDir, name) },
    )
}

internal fun advertisedCapabilities(
    capabilities: List<String>,
    navigationAdmission: NavigationAdmissionConfig?,
): List<String> =
    if (navigationAdmission?.enabled == true) (capabilities + "navigate").distinct() else capabilities

internal fun parseNavigationAdmission(
    json: JsonObject,
    session: String,
    deviceId: Int,
    key: ByteArray,
    readEvidence: (name: String, maximum: Int) -> ByteArray?,
    evidenceFile: (name: String) -> File,
): NavigationAdmissionConfig? {
    fun text(name: String) = (json[name] as? JsonString)?.value ?: error("$name must be text")
    fun positive(name: String) = ((json[name] as? JsonInt)?.value ?: error("$name must be integer")).also { require(it > 0) { "$name must be positive" } }
    fun arrivalHoldTimeout() = ((json["arrival_hold_timeout_ms"] as? JsonInt)?.value ?: 0).also { require(it in 0..180_000) { "arrival hold timeout is invalid" } }
    require(json["v"] == JsonInt(1) && json["enabled"] is JsonBool) { "navigation admission version or enabled flag is invalid" }
    if (!(json["enabled"] as JsonBool).value) { require(json.keys == setOf("v", "enabled")) { "disabled navigation admission has extra fields" }; return null }
    val fields = setOf("v", "enabled", "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256", "geometry_sha256", "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256", "control_source_ids", "clock_lease_id", "clock_lease_expires_at_ms", "max_authorization_lifetime_ms", "provenance")
    require(json.keys == fields || json.keys == fields + "arrival_hold_timeout_ms") { "navigation admission fields are invalid" }
    val pins = linkedMapOf("navigation_config" to text("navigation_config_sha256"), "map" to text("map_sha256"), "geometry" to text("geometry_sha256"), "camera_calibration" to text("camera_calibration_sha256"), "body_extrinsics" to text("body_extrinsics_sha256"), "world_transform" to text("world_transform_sha256"))
    val provenance = json["provenance"] as? JsonObject ?: error("navigation provenance must be an object")
    require(provenance.keys == setOf("v", "session", "device_id", "bindings", "signature")) { "navigation provenance fields are invalid" }
    require(provenance["v"] == JsonInt(1) && provenance["session"] == JsonString(session) && provenance["device_id"] == JsonInt(deviceId.toLong())) {
        "navigation provenance is for another session or device"
    }
    val signature = (provenance["signature"] as? JsonString)?.value ?: error("navigation provenance signature must be text")
    require(Signing.verify(provenance.without("signature"), signature, key)) { "navigation provenance signature is invalid" }
    val bindings = (provenance["bindings"] as? JsonArray)?.items ?: error("navigation provenance bindings must be a list")
    require(bindings.size == pins.size) { "navigation provenance must bind every pin" }
    val files = pins.entries.zip(bindings).map { (entry, value) ->
        val (kind, pin) = entry
        val binding = value as? JsonObject ?: error("navigation provenance binding is invalid")
        require(binding.keys == setOf("kind", "file", "semantic_sha256", "byte_sha256")) { "navigation provenance binding fields are invalid" }
        val bindingKind = (binding["kind"] as? JsonString)?.value ?: error("navigation provenance kind must be text")
        val name = (binding["file"] as? JsonString)?.value ?: error("navigation provenance file must be text")
        val semantic = (binding["semantic_sha256"] as? JsonString)?.value ?: error("navigation provenance semantic digest must be text")
        val bytesDigest = (binding["byte_sha256"] as? JsonString)?.value ?: error("navigation provenance byte digest must be text")
        require(bindingKind == kind && semantic == pin && name.matches(Regex("[A-Za-z0-9._-]{1,128}")) && bytesDigest.matches(Regex("[0-9a-f]{64}"))) {
            "navigation provenance binding is invalid"
        }
        val bytes = requireNotNull(readEvidence(name, 4 * 1024 * 1024)) { "navigation provenance file is missing" }
        require(sha256(bytes) == bytesDigest) { "navigation provenance bytes do not match binding" }
        evidenceFile(name)
    }
    val sources = (json["control_source_ids"] as? JsonArray)?.items?.map { (it as? JsonString)?.value ?: error("control source must be text") } ?: error("control sources must be a list")
    return NavigationAdmissionConfig(text("navigation_config_id"), pins.getValue("navigation_config"), text("map_version"), pins.getValue("map"), pins.getValue("geometry"), pins.getValue("camera_calibration"), pins.getValue("body_extrinsics"), pins.getValue("world_transform"), sources, text("clock_lease_id"), positive("clock_lease_expires_at_ms"), positive("max_authorization_lifetime_ms"), files, enabled = true, arrivalHoldTimeoutMs = arrivalHoldTimeout())
}

private fun sha256(bytes: ByteArray): String =
    MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) }

private fun privateBytes(directory: File, name: String, maximum: Int): ByteArray? {
    val descriptor = try { Os.open(File(directory, name).path, OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW or OsConstants.O_NONBLOCK, 0) } catch (error: android.system.ErrnoException) { if (error.errno == OsConstants.ENOENT) return null; throw error }
    FileInputStream(descriptor).use { input ->
        val info = Os.fstat(descriptor); require(OsConstants.S_ISREG(info.st_mode) && info.st_size in 1..maximum.toLong()) { "private artifact is not a bounded regular file" }
        return input.readBytes().also { require(it.size <= maximum) { "private artifact is too large" } }
    }
}
