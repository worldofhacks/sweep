package org.worldofhacks.sweep.bridge.core.frames

import java.io.File
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.Test
import org.worldofhacks.sweep.bridge.core.json.Json
import org.worldofhacks.sweep.bridge.core.json.JsonObject

class PythonNavigationContractTest {
    @Test
    fun `parses Python signed route pose and goto`() {
        val path = System.getenv("PYTHON_NAVIGATION_PACKET_PATH") ?: "build/python-navigation-packets.json"
        assumeTrue(File(path).isFile, "run tools/write_android_navigation_packets.py first")
        val packets = Json.parse(File(path).readText()) as JsonObject
        val route = NavigationRouteAuthorization.parse(packets["route"] as JsonObject)
        val pose = NavigationPose.parse(packets["pose"] as JsonObject)
        val command = CommandFrame.parse(packets["command"] as JsonObject)
        val key = "navigation-node-key-at-least-32bytes".encodeToByteArray()
        assertTrue(route.verifies(key))
        assertTrue(pose.verifies(key))
        assertTrue(command.verify(key))
        assertTrue(pose.seq > route.seq)
        assertEquals(route.routeId, (command.args as CommandArgs.Goto).navigationRouteId)
    }
}
