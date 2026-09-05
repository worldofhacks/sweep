package org.worldofhacks.sweep.bridge.publish

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Test

class WhipEndpointTest {
    @Test
    fun `the ground host defaults to the relay host on port 8889`() {
        assertEquals("http://10.10.1.60:8889/drone2/whip", WhipEndpoint.whipUrl("ws://10.10.1.60:8000", null, WhipEndpoint.DEFAULT_PORT, 2))
        assertEquals("http://10.10.1.60:8889/drone2/whep", WhipEndpoint.whepUrl("ws://10.10.1.60:8000", "", WhipEndpoint.DEFAULT_PORT, 2))
        assertEquals("http://10.10.1.60:8889/drone2", WhipEndpoint.playerUrl("ws://10.10.1.60:8000/", "  ", WhipEndpoint.DEFAULT_PORT, 2))
        assertEquals("http://127.0.0.1:8889/drone1/whip", WhipEndpoint.whipUrl("ws://127.0.0.1:8000", null, 8889, 1))
        assertEquals("http://relay.local:8889/drone1/whip", WhipEndpoint.whipUrl("wss://relay.local/ws", null, 8889, 1))
    }

    @Test
    fun `an explicit ground host and port override the relay host`() {
        assertEquals("http://ground:9000/drone4/whip", WhipEndpoint.whipUrl("ws://10.10.1.60:8000", " ground ", 9000, 4))
        assertEquals("http://[fe80::1]:8889/drone1/whip", WhipEndpoint.whipUrl("ws://[fe80::2]:8000", "fe80::1", 8889, 1))
        assertEquals("http://[fe80::2]:8889/drone1/whip", WhipEndpoint.whipUrl("ws://[fe80::2]:8000", null, 8889, 1))
    }

    @Test
    fun `hosts are read from any scheme and never include credentials or ports`() {
        assertEquals("10.10.1.60", WhipEndpoint.hostOf("ws://10.10.1.60:8000/ws/demo"))
        assertEquals("relay", WhipEndpoint.hostOf("http://user:pw@relay:1/x?y"))
        assertEquals("fe80::1", WhipEndpoint.hostOf("ws://[fe80::1]:8000"))
        assertEquals("bare", WhipEndpoint.hostOf("bare"))
    }

    @Test
    fun `stream names follow the console mapping and reject bad ids`() {
        assertEquals("drone1", WhipEndpoint.streamName(1))
        assertEquals("drone4", WhipEndpoint.streamName(4))
        assertThrows(IllegalArgumentException::class.java) { WhipEndpoint.streamName(0) }
        assertThrows(IllegalArgumentException::class.java) { WhipEndpoint.whipUrl("ws://h:1", null, 0, 1) }
        assertThrows(IllegalArgumentException::class.java) { WhipEndpoint.whipUrl("ws://", null, 8889, 1) }
    }
}
