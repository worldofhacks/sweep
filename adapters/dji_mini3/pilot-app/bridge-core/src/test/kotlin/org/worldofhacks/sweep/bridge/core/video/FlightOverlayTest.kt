package org.worldofhacks.sweep.bridge.core.video

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.frames.DeltaKind
import org.worldofhacks.sweep.bridge.core.frames.GuidanceMode
import org.worldofhacks.sweep.bridge.core.frames.SuggestedDelta

class FlightOverlayTest {
    private fun inputs(
        aircraft: Boolean = true,
        rc: Boolean = true,
        relay: Boolean = true,
        watchdog: String = "nominal",
        estop: Boolean = false,
        authority: Boolean = true,
        reason: String? = null,
        yaw: Double? = 0.0,
        hfov: Double? = null,
        frameAge: Long? = 40,
        capture: CaptureProgress = CaptureProgress(),
    ) = OverlayInputs(
        aircraftConnected = aircraft,
        rcConnected = rc,
        relayConnected = relay,
        watchdog = watchdog,
        estop = estop,
        controlAuthority = authority,
        authorityChangeReason = reason,
        yawDeg = yaw,
        measuredHfovDeg = hfov,
        lastFrameAgeMs = frameAge,
        capture = capture,
    )

    @Test
    fun `the five capture states`() {
        assertEquals(CaptureState.READY, FlightOverlay.derive(inputs()).captureState)
        val capturing = inputs(capture = CaptureProgress(phase = CapturePhase.Capturing(3, 8)))
        assertEquals(CaptureState.CAPTURING, FlightOverlay.derive(capturing).captureState)
        val downloading = inputs(capture = CaptureProgress(phase = CapturePhase.Downloading(2, 8)))
        assertEquals(CaptureState.DOWNLOADING, FlightOverlay.derive(downloading).captureState)
        val retake = inputs(capture = CaptureProgress(phase = CapturePhase.NeedsRetake(listOf(90.0, 135.0))))
        assertEquals(CaptureState.NEEDS_RETAKE, FlightOverlay.derive(retake).captureState)
        assertEquals(CaptureState.DISCONNECTED, FlightOverlay.derive(inputs(aircraft = false)).captureState)
        assertEquals(CaptureState.DISCONNECTED, FlightOverlay.derive(inputs(rc = false)).captureState)
        assertEquals(
            listOf("Ready", "Capturing", "Downloading", "Needs retake", "Disconnected"),
            CaptureState.entries.map { it.label },
        )
    }

    @Test
    fun `disconnected wins over an active capture and relay loss does not`() {
        val capturing = CaptureProgress(phase = CapturePhase.Capturing(3, 8))
        assertEquals(CaptureState.DISCONNECTED, FlightOverlay.derive(inputs(aircraft = false, capture = capturing)).captureState)
        val relayDown = FlightOverlay.derive(inputs(relay = false, capture = capturing))
        assertEquals(CaptureState.CAPTURING, relayDown.captureState)
        assertEquals(listOf("Relay disconnected"), relayDown.degraded)
    }

    @Test
    fun `progress labels`() {
        assertNull(FlightOverlay.derive(inputs()).progressLabel)
        assertEquals("3 of 8", FlightOverlay.derive(inputs(capture = CaptureProgress(phase = CapturePhase.Capturing(3, 8)))).progressLabel)
        assertEquals("42%", FlightOverlay.derive(inputs(capture = CaptureProgress(phase = CapturePhase.Capturing(42, 100, percent = true)))).progressLabel)
        assertEquals("file 2 of 8", FlightOverlay.derive(inputs(capture = CaptureProgress(phase = CapturePhase.Downloading(2, 8)))).progressLabel)
        assertEquals(
            "missing 90°, 135°",
            FlightOverlay.derive(inputs(capture = CaptureProgress(phase = CapturePhase.NeedsRetake(listOf(90.0, 135.0))))).progressLabel,
        )
    }

    @Test
    fun `sectors come from the measured field of view or fall back to reconstruct_8 provisionally`() {
        val measured = FlightOverlay.derive(inputs(hfov = 66.0))
        assertEquals(6, measured.sectors.size)
        assertEquals(60.0, measured.sectorWidthDeg, 1e-9)
        assertFalse(measured.sectorsProvisional)
        assertEquals(0.0, measured.sectors.first().startDeg, 1e-9)
        assertEquals(360.0, measured.sectors.last().endDeg, 1e-9)

        val provisional = FlightOverlay.derive(inputs(hfov = null))
        assertEquals(8, provisional.sectors.size)
        assertEquals(45.0, provisional.sectorWidthDeg, 1e-9)
        assertTrue(provisional.sectorsProvisional)
        assertTrue(provisional.sectors.all { it.mark == SectorMark.UNSEEN })

        // A published lens value is not a measured horizontal field of view; the derivation
        // only ever sees the measured field, and an unusable one is treated as unmeasured.
        assertTrue(FlightOverlay.derive(inputs(hfov = 0.0)).sectorsProvisional)
        assertTrue(FlightOverlay.derive(inputs(hfov = 180.0)).sectorsProvisional)
    }

    @Test
    fun `sector marks accepted beats weak and headings wrap`() {
        val capture = CaptureProgress(acceptedHeadingsDeg = listOf(10.0, 370.0), weakHeadingsDeg = listOf(100.0, 20.0, -5.0))
        val state = FlightOverlay.derive(inputs(capture = capture))
        assertEquals(SectorMark.ACCEPTED, state.sectors[0].mark)
        assertEquals(SectorMark.WEAK, state.sectors[2].mark)
        assertEquals(SectorMark.WEAK, state.sectors[7].mark)
        assertEquals(SectorMark.UNSEEN, state.sectors[1].mark)
        assertEquals(listOf("unseen", "weak", "accepted"), SectorMark.entries.map { it.wire })
    }

    @Test
    fun `next heading is the nearest unaccepted sector centre and the delta is a yaw`() {
        // Heading 352.7: the nearest centre is 337.5, but that sector is accepted, so 22.5 wins over 292.5.
        val capture = CaptureProgress(acceptedHeadingsDeg = listOf(340.0))
        val state = FlightOverlay.derive(inputs(yaw = -7.3, capture = capture))
        assertEquals(352.7, state.headingDeg!!, 1e-9)
        assertEquals(22.5, state.nextHeadingDeg!!, 1e-9)
        assertEquals(DeltaKind.YAW, state.suggestedDelta?.kind)
        assertEquals(29.8, state.suggestedDelta!!.degrees, 1e-9)
        assertEquals("yaw +30°", state.deltaLabel)
    }

    @Test
    fun `the first heading is where the aircraft already looks`() {
        val state = FlightOverlay.derive(inputs(yaw = 30.2))
        assertEquals(22.5, state.nextHeadingDeg!!, 1e-9)
        assertEquals("yaw −8°", state.deltaLabel)
    }

    @Test
    fun `explicit next heading and gimbal delta from the capture path win`() {
        val explicit = FlightOverlay.derive(inputs(yaw = 350.0, capture = CaptureProgress(nextHeadingDeg = 10.0)))
        assertEquals(10.0, explicit.nextHeadingDeg!!, 1e-9)
        assertEquals("yaw +20°", explicit.deltaLabel)
        val gimbal = FlightOverlay.derive(inputs(yaw = 350.0, capture = CaptureProgress(nextHeadingDeg = 10.0, gimbalDeltaDeg = -15.0)))
        assertEquals(SuggestedDelta(DeltaKind.GIMBAL, -15.0), gimbal.suggestedDelta)
        assertEquals("gimbal −15°", gimbal.deltaLabel)
    }

    @Test
    fun `without yaw the marker points at the first open sector and no delta is suggested`() {
        val state = FlightOverlay.derive(inputs(yaw = null))
        assertNull(state.headingDeg)
        assertEquals(22.5, state.nextHeadingDeg!!, 1e-9)
        assertNull(state.suggestedDelta)
        assertNull(state.deltaLabel)
        val done = CaptureProgress(acceptedHeadingsDeg = (0 until 8).map { it * 45.0 + 1.0 })
        assertNull(FlightOverlay.derive(inputs(capture = done)).nextHeadingDeg)
    }

    @Test
    fun `wrap delta and heading normalisation`() {
        assertEquals(20.0, FlightOverlay.wrapDelta(350.0, 10.0), 1e-9)
        assertEquals(-20.0, FlightOverlay.wrapDelta(10.0, 350.0), 1e-9)
        assertEquals(180.0, FlightOverlay.wrapDelta(0.0, 180.0), 1e-9)
        assertEquals(180.0, FlightOverlay.wrapDelta(180.0, 0.0), 1e-9)
        assertEquals(350.0, FlightOverlay.heading(-10.0), 1e-9)
        assertEquals(10.0, FlightOverlay.heading(730.0), 1e-9)
    }

    @Test
    fun `mode labels and the standing notes`() {
        val state = FlightOverlay.derive(inputs())
        assertEquals(GuidanceMode.VISUAL_ADVISORY, state.guidanceMode)
        assertEquals("visual_advisory", state.guidanceMode.wire)
        assertEquals("aircraft_telemetry", state.poseSource)
        assertEquals("Physical RC remains primary", state.rcPrimaryNote)
        assertEquals("clearance: unverified", state.clearanceLabel)
    }

    @Test
    fun `authority and video labels`() {
        assertEquals("Sweep", FlightOverlay.derive(inputs()).authorityLabel)
        assertEquals("RC (rc_disconnected)", FlightOverlay.derive(inputs(authority = false, reason = "rc_disconnected")).authorityLabel)
        assertEquals("Video live", FlightOverlay.derive(inputs(frameAge = 900)).videoLabel)
        assertEquals("No video for 3 s", FlightOverlay.derive(inputs(frameAge = 3_400)).videoLabel)
        assertEquals("No video yet", FlightOverlay.derive(inputs(frameAge = null)).videoLabel)
    }

    @Test
    fun `degraded sentences are ordered most urgent first`() {
        val state = FlightOverlay.derive(
            inputs(aircraft = false, rc = false, relay = false, watchdog = "failsafe", estop = true, frameAge = 12_000),
        )
        assertEquals(
            listOf(
                "Network stop active: neutral sticks and hover, then land if the stop is held",
                "Watchdog failsafe: land indoors, never return home",
                "Aircraft disconnected",
                "RC disconnected",
                "Relay disconnected",
                "No video for 12 s",
            ),
            state.degraded,
        )
        assertEquals(listOf("Watchdog hold: neutral sticks and hover"), FlightOverlay.derive(inputs(watchdog = "hold")).degraded)
        assertEquals(emptyList<String>(), FlightOverlay.derive(inputs()).degraded)
        assertEquals(listOf("No video yet"), FlightOverlay.derive(inputs(frameAge = null)).degraded)
    }
}
