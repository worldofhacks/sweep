package org.worldofhacks.sweep.bridge.core.frames

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
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
        assertEquals("map-a", parsed.mapId)

        for (invalid in listOf(
            wire.with("flight_approved", Json.value(false)),
            wire.with("expires_at_ms", Json.value(2_000)),
            wire.with("map_id", Json.value(" map-a")),
            wire.with("target_x_mm", Json.value(0)),
            wire.with("target_x_mm", Json.value(NavigationRouteAuthorization.MAX_ABS_POSITION_MM + 1)),
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

    private fun routeAuthorization() = NavigationRouteAuthorization(
        t = 2_000,
        expiresAtMs = 3_000,
        eventId = "route-event-1",
        session = "session-a",
        droneId = 1,
        connectionEpoch = 2,
        commandId = "command-1",
        routeId = "route-7",
        seq = 1,
        navigationConfigId = "config-4",
        mapId = "map-a",
        geometryId = "geometry-a",
        cameraCalibrationId = "camera-a",
        bodyExtrinsicsId = "body-a",
        startXMm = 0,
        startYMm = 0,
        startZMm = 1_000,
        targetXMm = 1_000,
        targetYMm = 0,
        targetZMm = 1_000,
        maxSpeedMmS = 300,
        horizontalToleranceMm = 100,
        verticalToleranceMm = 100,
        maxPositionUncertaintyMm = 50,
        tubeRadiusMm = 200,
        signature = "0".repeat(64),
    )

    private fun navigationPose(status: NavigationPose.Status = NavigationPose.Status.READY): NavigationPose {
        val ready = status == NavigationPose.Status.READY
        return NavigationPose(
            t = 2_500,
            eventId = "pose-event-1",
            session = "session-a",
            droneId = 1,
            connectionEpoch = 2,
            commandId = "command-1",
            routeId = "route-7",
            seq = 2,
            navigationConfigId = "config-4",
            mapId = "map-a",
            geometryId = "geometry-a",
            cameraCalibrationId = "camera-a",
            bodyExtrinsicsId = "body-a",
            poseTimeMs = if (ready) 2_490 else null,
            fixTimeMs = if (ready) 2_480 else null,
            xMm = if (ready) 100 else null,
            yMm = if (ready) -200 else null,
            zMm = if (ready) 1_000 else null,
            positionUncertaintyMm = if (ready) 25 else null,
            status = status,
            signature = "0".repeat(64),
        )
    }
}
