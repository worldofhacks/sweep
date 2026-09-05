package org.worldofhacks.sweep.bridge.session

import java.io.File

/** Plain-text evidence export for Phase B4; the generator is pure so it is unit-testable. */
object ProbeReport {
    data class Environment(
        val aircraftVariant: String,
        val applicationId: String,
        val appVersion: String,
        val msdkVersion: String,
        val phone: String,
        val android: String,
    )

    fun render(state: SessionState, environment: Environment, exportedAtMs: Long): String = buildString {
        appendLine("Sweep DJI Mini 3 bridge probe report")
        appendLine("exported_at_ms: $exportedAtMs")
        appendLine("aircraft_variant: ${environment.aircraftVariant}")
        appendLine("application_id: ${environment.applicationId}")
        appendLine("app_version: ${environment.appVersion}")
        appendLine("msdk_version: ${environment.msdkVersion}")
        appendLine("phone: ${environment.phone}")
        appendLine("android: ${environment.android}")
        appendLine()
        appendLine("registration: ${state.registration}")
        appendLine("registration_detail: ${state.registrationDetail ?: "-"}")
        appendLine("sdk_init_stage: ${state.initStage}")
        appendLine("product_connection: ${state.product}")
        appendLine("product_id: ${state.productId ?: "-"}")
        appendLine("connection_generation: ${state.generation}")
        appendLine("dropped_callbacks: ${state.droppedCallbacks}")
        appendLine()
        appendLine("identity")
        appendLine("  product_type: ${state.identity.productType ?: "-"}")
        appendLine("  is_dji_mini_3: ${state.identity.isMini3 ?: "-"}")
        appendLine("  aircraft_firmware: ${state.identity.aircraftFirmware ?: "-"}")
        appendLine("  rc_firmware_type: ${state.identity.rcFirmwareType ?: "-"}")
        appendLine("  rc_firmware_versions: ${state.identity.rcFirmwareVersions.ifEmpty { listOf("-") }.joinToString()}")
        appendLine("  rc_firmware: ${state.identity.rcFirmware ?: "-"}")
        appendLine()
        appendLine("events")
        for (event in state.events) {
            appendLine("  ${event.seq} | ${event.name} | ${event.detail.replace("|", "/")}")
        }
        appendLine()
        appendLine("Metadata and state changes only; no image contents.")
    }

    fun write(directory: File, state: SessionState, environment: Environment, exportedAtMs: Long): ExportResult =
        runCatching {
            val reports = File(directory, "probe-reports").apply { mkdirs() }
            val file = File(reports, "probe-report-$exportedAtMs.txt")
            file.writeText(render(state, environment, exportedAtMs))
            ExportResult.Saved(file.absolutePath)
        }.getOrElse { error -> ExportResult.Failed(error.message ?: error.javaClass.simpleName) }
}
