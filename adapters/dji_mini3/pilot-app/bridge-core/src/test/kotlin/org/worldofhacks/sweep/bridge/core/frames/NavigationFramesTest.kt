package org.worldofhacks.sweep.bridge.core.frames

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonArray
import org.worldofhacks.sweep.bridge.core.json.JsonObject
import org.worldofhacks.sweep.bridge.core.signing.Signing

class NavigationFramesTest {
    private val key = "navigation-node-key".toByteArray()

    @Test
    fun `route authorization is signed exact bounded and provenance pinned`() {
        val authorization = routeAuthorization()
        val wire = authorization.unsignedEvent().with("signature", Json.value(Signing.sign(authorization.unsignedEvent(), key)))

        val parsed = NavigationRouteAuthorization.parse(wire)

        assertTrue(parsed.verifies(key))
        assertFalse(parsed.verifies("other-node-key".toByteArray()))
        assertEquals("route-7", parsed.routeId)
        assertEquals("config-4", parsed.navigationConfigId)
        assertEquals("map-v1", parsed.mapVersion)

        for (invalid in listOf(
            wire.with("flight_approved", Json.value(false)),
            wire.with("expires_at_ms", Json.value(2_000)),
            wire.with("map_id", Json.value(" map-a")),
            wire.with("target_x_mm", Json.value(0)),
            wire.with("target_x_mm", Json.value(NavigationSegment.MAX_ABS_POSITION_MM + 1)),
            wire.with("segments", JsonArray(listOf(authorization.segments.single().toJson(), NavigationSegment(1_000, 0, 1_000, 2_000, 0, 1_000, 200).toJson()))),
            wire.with("max_speed_mm_s", Json.value(Double.NaN)),
            JsonObject(wire.fields + ("unexpected" to Json.value(true))),
        )) {
            assertThrows(ContractError::class.java) { NavigationRouteAuthorization.parse(invalid) }
        }
    }

    @Test
    fun `navigation poses bind complete ready observations and withhold hold observations`() {
        val ready = navigationPose().signed()
        val parsedReady = NavigationPose.parse(ready)

        assertTrue(parsedReady.verifies(key))
        assertEquals(NavigationPose.Status.READY, parsedReady.status)
        assertEquals(100, parsedReady.xMm)

        val hold = navigationPose(status = NavigationPose.Status.HOLD).signed()
        val parsedHold = NavigationPose.parse(hold)
        assertEquals(NavigationPose.Status.HOLD, parsedHold.status)
        assertEquals(null, parsedHold.poseTimeMs)

        for (invalid in listOf(
            ready.with("flight_approved", Json.value(false)),
            ready.with("pose_time_ms", Json.value(2_501)),
            ready.with("fix_time_ms", Json.value(2_491)),
            ready.with("x_mm", Json.value(null)),
            ready.with("geometry_id", Json.value("geometry\u200b-a")),
            hold.with("x_mm", Json.value(0)),
        )) {
            assertThrows(ContractError::class.java) { NavigationPose.parse(invalid) }
        }
    }

    @Test
    fun `python route fixture parses verifies and binds its goto to the authorized target`() {
        val fixture = Json.parse(
            requireNotNull(javaClass.classLoader.getResource("navigation/python_route_pose_fixture.json"))
                .readText(),
        ) as JsonObject
        val key = ((fixture["key_utf8"] as org.worldofhacks.sweep.bridge.core.json.JsonString).value).toByteArray()
        val authorization = NavigationRouteAuthorization.parse(fixture["route_authorization"] as JsonObject)
        val pose = NavigationPose.parse(fixture["navigation_pose"] as JsonObject)
        val goto = fixture["goto"] as JsonObject

        assertTrue(authorization.verifies(key))
        assertTrue(pose.verifies(key))
        assertEquals(authorization.routeId, (goto["navigation_route_id"] as org.worldofhacks.sweep.bridge.core.json.JsonString).value)
        assertEquals(authorization.target(), listOf(
            (goto["x_mm"] as org.worldofhacks.sweep.bridge.core.json.JsonInt).value,
            (goto["y_mm"] as org.worldofhacks.sweep.bridge.core.json.JsonInt).value,
            (goto["z_mm"] as org.worldofhacks.sweep.bridge.core.json.JsonInt).value,
        ))
    }

    @Test
    fun `route evidence cannot be downgraded into a legacy goto`() {
        val route = routeAuthorization().signed()
        assertThrows(ContractError::class.java) { CommandFrame.parse(route) }

        val legacy = CommandFrame(
            t = 2_000,
            eventId = "command-event-1",
            session = "session-a",
            commandId = "command-1",
            intentId = "intent-1",
            rosterVersion = 1,
            droneId = 1,
            connectionEpoch = 2,
            seq = 1,
            issuedAt = 2_000,
            ttlMs = 1_000,
            operation = CommandOperation.GOTO,
            args = CommandArgs.Goto(1_000, 0, 1_000, 300),
        )
        val routeArgs = CommandArgs.Goto(1_000, 0, 1_000, 300, "route-7")
        val routeGoto = legacy.copy(args = routeArgs, rawArgs = routeArgs.toJson()).signed(key).toJson()
        assertEquals("route-7", (CommandFrame.parse(routeGoto).args as CommandArgs.Goto).navigationRouteId)

        val unboundRouteId = legacy.copy(
            rawArgs = legacy.rawArgs.with("route_id", Json.value("route-7")),
        ).signed(key).toJson()

        assertThrows(ContractError::class.java) { CommandFrame.parse(unboundRouteId) }
    }

    private fun NavigationRouteAuthorization.signed(): JsonObject =
        unsignedEvent().with("signature", Json.value(Signing.sign(unsignedEvent(), key)))

    private fun NavigationPose.signed(): JsonObject =
        unsignedEvent().with("signature", Json.value(Signing.sign(unsignedEvent(), key)))

    private val hash = "a".repeat(64)

    private fun routeAuthorization() = NavigationRouteAuthorization(
        t = 2_000, expiresAtMs = 3_000, eventId = "route-event-1", session = "session-a", deviceId = 1,
        connectionEpoch = 2, commandId = "command-1", routeId = "route-7", seq = 1,
        positionFrame = NavigationRouteAuthorization.POSITION_FRAME, clockLeaseId = "lease-1", maxClockErrorMs = 25,
        navigationConfigId = "config-4", navigationConfigSha256 = hash, mapVersion = "map-v1", mapSha256 = hash,
        geometrySha256 = hash, cameraCalibrationSha256 = hash, bodyExtrinsicsSha256 = hash, worldTransformSha256 = hash,
        controlSourceIds = listOf("tag-source", "telemetry-source"),
        segments = listOf(NavigationSegment(0, 0, 1_000, 1_000, 0, 1_000, 200)),
        maxSpeedMmS = 300, maxAccelerationMmS2 = 200, maxDecelerationMmS2 = 200, maxPositionUncertaintyMm = 50,
        maxCrossTrackMm = 200, arrivalHorizontalToleranceMm = 100, arrivalVerticalToleranceMm = 100,
        poseFreshnessMs = 500, trackingTimeoutMs = 1_000, flightApproved = true, signature = "0".repeat(64),
    )

    private fun navigationPose(status: NavigationPose.Status = NavigationPose.Status.READY): NavigationPose {
        val ready = status == NavigationPose.Status.READY
        return NavigationPose(
            t = 2_500, eventId = "pose-event-1", session = "session-a", deviceId = 1, connectionEpoch = 2,
            commandId = "command-1", routeId = "route-7", seq = 2, positionFrame = NavigationRouteAuthorization.POSITION_FRAME,
            clockLeaseId = "lease-1", navigationConfigId = "config-4", navigationConfigSha256 = hash, mapVersion = "map-v1",
            mapSha256 = hash, geometrySha256 = hash, cameraCalibrationSha256 = hash, bodyExtrinsicsSha256 = hash,
            worldTransformSha256 = hash, controlSourceIds = listOf("tag-source", "telemetry-source"),
            poseTimeMs = if (ready) 2_490 else null, fixTimeMs = if (ready) 2_480 else null,
            xMm = if (ready) 100 else null, yMm = if (ready) -200 else null, zMm = if (ready) 1_000 else null,
            positionUncertaintyMm = if (ready) 25 else null, status = status, flightApproved = true, signature = "0".repeat(64),
        )
    }
}
