package org.worldofhacks.sweep.bridge

import java.io.File
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonBool
import org.worldofhacks.sweep.bridge.core.json.JsonInt
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.json.JsonString
import org.worldofhacks.sweep.bridge.node.NavigationAdmissionConfig

/** Private operator artifact. Missing or explicitly disabled means no route may reach flight control. */
internal fun loadNavigationAdmission(filesDir: File): NavigationAdmissionConfig? {
    val file = File(filesDir, "navigation-admission.json")
    if (!file.exists()) return null
    val bytes = file.readBytes().also { require(it.size <= 16 * 1024) { "navigation admission configuration is too large" } }
    val json = Json.parse(bytes.toString(Charsets.UTF_8)) as? JsonObject ?: error("navigation admission configuration must be an object")
    fun text(name: String) = (json[name] as? JsonString)?.value ?: error("$name must be text")
    fun positive(name: String) = ((json[name] as? JsonInt)?.value ?: error("$name must be integer")).also { require(it > 0) { "$name must be positive" } }
    require(json["v"] == JsonInt(1) && json["enabled"] is JsonBool) { "navigation admission version or enabled flag is invalid" }
    if ((json["enabled"] as JsonBool).value.not()) { require(json.keys == setOf("v", "enabled")) { "disabled navigation admission has extra fields" }; return null }
    require(json.keys == setOf("v", "enabled", "navigation_config_id", "navigation_config_sha256", "map_version", "map_sha256", "geometry_sha256", "camera_calibration_sha256", "body_extrinsics_sha256", "world_transform_sha256", "control_source_ids", "clock_lease_id", "clock_lease_expires_at_ms", "max_authorization_lifetime_ms", "evidence_files")) { "navigation admission fields are invalid" }
    val names = (json["evidence_files"] as? JsonArray)?.items?.map { (it as? JsonString)?.value ?: error("evidence file must be text") } ?: error("evidence files must be a list")
    require(names.isNotEmpty() && names.size <= 16 && names.all { it.matches(Regex("[A-Za-z0-9._-]{1,128}")) }) { "navigation evidence file names are invalid" }
    val files = names.map { name -> File(filesDir, name).also { candidate -> require(candidate.isFile && candidate.canRead() && candidate.length() in 1..(4L * 1024 * 1024)) { "navigation evidence file is unavailable" } } }
    val sources = (json["control_source_ids"] as? JsonArray)?.items?.map { (it as? JsonString)?.value ?: error("control source must be text") } ?: error("control sources must be a list")
    return NavigationAdmissionConfig(text("navigation_config_id"), text("navigation_config_sha256"), text("map_version"), text("map_sha256"), text("geometry_sha256"), text("camera_calibration_sha256"), text("body_extrinsics_sha256"), text("world_transform_sha256"), sources, text("clock_lease_id"), positive("clock_lease_expires_at_ms"), positive("max_authorization_lifetime_ms"), files, enabled = true)
}
