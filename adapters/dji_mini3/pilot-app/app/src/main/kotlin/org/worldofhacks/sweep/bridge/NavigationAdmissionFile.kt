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
import org.worldofhacks.sweep.bridge.node.NavigationAdmissionConfig

internal fun loadNavigationAdmission(filesDir: File): NavigationAdmissionConfig? {
    val configBytes = privateBytes(filesDir, "navigation-admission.json", 16 * 1024) ?: return null
    val json = Json.parse(configBytes.toString(Charsets.UTF_8)) as? JsonObject ?: error("navigation admission configuration must be an object")
    fun text(name: String) = (json[name] as? JsonString)?.value ?: error("$name must be text")
    fun positive(name: String) = ((json[name] as? JsonInt)?.value ?: error("$name must be integer")).also { require(it > 0) { "$name must be positive" } }
    require(json["v"] == JsonInt(1) && json["enabled"] is JsonBool) { "navigation admission version or enabled flag is invalid" }
    if (!(json["enabled"] as JsonBool).value) { require(json.keys == setOf("v", "enabled")) { "disabled navigation admission has extra fields" }; return null }
    val fields = setOf("v", "enabled", "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256", "geometry_sha256", "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256", "control_source_ids", "clock_lease_id", "clock_lease_expires_at_ms", "max_authorization_lifetime_ms", "evidence")
    require(json.keys == fields) { "navigation admission fields are invalid" }
    val pins = linkedMapOf("navigation_config" to text("navigation_config_sha256"), "map" to text("map_sha256"), "geometry" to text("geometry_sha256"), "camera_calibration" to text("camera_calibration_sha256"), "body_extrinsics" to text("body_extrinsics_sha256"), "world_transform" to text("world_transform_sha256"))
    val evidence = json["evidence"] as? JsonObject ?: error("navigation evidence must be an object")
    require(evidence.keys == pins.keys) { "navigation evidence must cover every pin" }
    val files = pins.map { (kind, pin) ->
        val record = evidence[kind] as? JsonObject ?: error("navigation evidence record is invalid")
        require(record.keys == setOf("file", "sha256")) { "navigation evidence record fields are invalid" }
        val name = (record["file"] as? JsonString)?.value ?: error("navigation evidence file must be text")
        val declared = (record["sha256"] as? JsonString)?.value ?: error("navigation evidence digest must be text")
        require(name.matches(Regex("[A-Za-z0-9._-]{1,128}")) && declared == pin && declared.matches(Regex("[0-9a-f]{64}"))) { "navigation evidence pin is invalid" }
        val bytes = requireNotNull(privateBytes(filesDir, name, 4 * 1024 * 1024)) { "navigation evidence file is missing" }
        require(MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 0xff) } == pin) { "navigation evidence bytes do not match pin" }
        File(filesDir, name)
    }
    val sources = (json["control_source_ids"] as? JsonArray)?.items?.map { (it as? JsonString)?.value ?: error("control source must be text") } ?: error("control sources must be a list")
    return NavigationAdmissionConfig(text("navigation_config_id"), pins.getValue("navigation_config"), text("map_version"), pins.getValue("map"), pins.getValue("geometry"), pins.getValue("camera_calibration"), pins.getValue("body_extrinsics"), pins.getValue("world_transform"), sources, text("clock_lease_id"), positive("clock_lease_expires_at_ms"), positive("max_authorization_lifetime_ms"), files, enabled = true)
}

private fun privateBytes(directory: File, name: String, maximum: Int): ByteArray? {
    val descriptor = try { Os.open(File(directory, name).path, OsConstants.O_RDONLY or OsConstants.O_NOFOLLOW, 0) } catch (error: android.system.ErrnoException) { if (error.errno == OsConstants.ENOENT) return null; throw error }
    FileInputStream(descriptor).use { input ->
        val info = Os.fstat(descriptor); require(OsConstants.S_ISREG(info.st_mode) && info.st_size in 1..maximum.toLong()) { "private artifact is not a bounded regular file" }
        return input.readBytes().also { require(it.size <= maximum) { "private artifact is too large" } }
    }
}
