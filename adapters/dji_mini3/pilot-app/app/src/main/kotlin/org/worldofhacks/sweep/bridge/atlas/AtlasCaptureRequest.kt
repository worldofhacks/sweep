package org.worldofhacks.sweep.bridge.atlas

import org.json.JSONObject

/** Immutable request binding across the picker, camera activity and durable upload queue. */
@ConsistentCopyVisibility
data class AtlasCaptureRequest private constructor(val targetJson: String, val label: String, val note: String) {
    fun target() = JSONObject(targetJson)
    fun json() = JSONObject().put("target", target()).put("label", label).put("note", note)
    companion object {
        fun fromPayload(value: JSONObject): AtlasCaptureRequest? =
            if (value.isNull("request")) null else parse(value.getJSONObject("request"))
        fun parse(value: JSONObject): AtlasCaptureRequest {
            val label = value.getString("label")
            val note = value.getString("note")
            require(label.isNotBlank() && label.length <= 100 && note.length <= 240)
            return AtlasCaptureRequest(canonicalTarget(value.getJSONObject("target")), label, note)
        }
        private fun canonicalTarget(value: JSONObject): String {
            val kind = value.getString("kind")
            val fields = when (kind) {
                "location" -> mapOf("cell_id" to "[0-9]{1,2}:[0-9]{1,2}")
                "surface" -> mapOf("job_id" to "[0-9a-f-]{36}", "artifact_sha256" to "[0-9a-f]{64}", "region_id" to "[0-9a-f]{16}")
                else -> error("This capture request is not supported.")
            }
            require(value.length() == fields.size + 1)
            return JSONObject().put("kind", kind).also { canonical ->
                fields.forEach { (field, pattern) ->
                    val text = value.getString(field)
                    require(text.matches(Regex(pattern))) { "This capture request is invalid." }
                    canonical.put(field, text)
                }
            }.toString()
        }
        fun matches(expected: JSONObject, actual: JSONObject?): Boolean =
            actual != null && runCatching { canonicalTarget(expected) == canonicalTarget(actual) }.getOrDefault(false)
    }
}
