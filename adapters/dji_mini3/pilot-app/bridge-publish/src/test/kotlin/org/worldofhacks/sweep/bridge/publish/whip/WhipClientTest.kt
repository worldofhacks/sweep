package org.worldofhacks.sweep.bridge.publish.whip

import java.util.concurrent.TimeUnit
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class WhipClientTest {
    private val offer = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n"
    private val answer = "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n"

    private fun server(block: MockWebServer.() -> Unit) {
        MockWebServer().use { server ->
            server.start()
            server.block()
        }
    }

    @Test
    fun `201 with a path location yields the answer and an absolute resource url`() = server {
        enqueue(MockResponse().setResponseCode(201).setHeader("Content-Type", "application/sdp").setHeader("Location", "/drone1/whip/abc123").setBody(answer))
        val whipUrl = url("/drone1/whip").toString()
        val session = WhipClient(timeoutMs = 2_000).use { it.publish(whipUrl, offer) }
        assertEquals(answer, session.answerSdp)
        assertEquals(url("/drone1/whip/abc123").toString(), session.resourceUrl)
        val request = takeRequest(1, TimeUnit.SECONDS)!!
        assertEquals("POST", request.method)
        assertEquals("/drone1/whip", request.path)
        assertEquals("application/sdp", request.getHeader("Content-Type"))
        assertEquals(offer, request.body.readUtf8())
    }

    @Test
    fun `absolute and bare locations resolve too`() = server {
        enqueue(MockResponse().setResponseCode(201).setHeader("Location", url("/drone2/whip/xyz")).setBody(answer))
        enqueue(MockResponse().setResponseCode(201).setHeader("Location", "bare-id").setBody(answer))
        enqueue(MockResponse().setResponseCode(200).setBody(answer))
        WhipClient(timeoutMs = 2_000).use { client ->
            assertEquals(url("/drone2/whip/xyz").toString(), client.publish(url("/drone2/whip").toString(), offer).resourceUrl)
            assertEquals(url("/drone2/whip/bare-id").toString(), client.publish(url("/drone2/whip").toString(), offer).resourceUrl)
            assertNull(client.publish(url("/drone2/whip").toString(), offer).resourceUrl)
        }
    }

    @Test
    fun `4xx and 5xx raise a WhipException carrying the status and body`() = server {
        enqueue(MockResponse().setResponseCode(404).setBody("path not found"))
        enqueue(MockResponse().setResponseCode(400).setBody("SetRemoteDescription called with no ice-ufrag"))
        enqueue(MockResponse().setResponseCode(500))
        WhipClient(timeoutMs = 2_000).use { client ->
            val notFound = assertThrows(WhipException::class.java) { client.publish(url("/drone9/whip").toString(), offer) }
            assertEquals(404, notFound.status)
            assertTrue(notFound.message!!.contains("HTTP 404 path not found"), notFound.message)
            val bad = assertThrows(WhipException::class.java) { client.publish(url("/drone1/whip").toString(), offer) }
            assertEquals(400, bad.status)
            assertTrue(bad.message!!.contains("ice-ufrag"))
            val error = assertThrows(WhipException::class.java) { client.publish(url("/drone1/whip").toString(), offer) }
            assertEquals(500, error.status)
        }
    }

    @Test
    fun `an empty 2xx body is an error not a session`() = server {
        enqueue(MockResponse().setResponseCode(201).setHeader("Location", "/drone1/whip/x"))
        val error = WhipClient(timeoutMs = 2_000).use { client ->
            assertThrows(WhipException::class.java) { client.publish(url("/drone1/whip").toString(), offer) }
        }
        assertEquals(201, error.status)
        assertTrue(error.message!!.contains("without an SDP answer"))
    }

    @Test
    fun `a dropped connection surfaces as a WhipException without a status`() = server {
        enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        val error = WhipClient(timeoutMs = 2_000).use { client ->
            assertThrows(WhipException::class.java) { client.publish(url("/drone1/whip").toString(), offer) }
        }
        assertNull(error.status)
        val refused = WhipClient(timeoutMs = 1_000).use { client ->
            assertThrows(WhipException::class.java) { client.publish("http://127.0.0.1:1/drone1/whip", offer) }
        }
        assertNull(refused.status)
        WhipClient(timeoutMs = 1_000).use { client ->
            assertThrows(WhipException::class.java) { client.publish("not a url", offer) }
        }
    }

    @Test
    fun `stop deletes the resource and tolerates failures`() = server {
        enqueue(MockResponse().setResponseCode(201).setHeader("Location", "/drone1/whip/abc").setBody(answer))
        enqueue(MockResponse().setResponseCode(200))
        enqueue(MockResponse().setResponseCode(404))
        val credential = MediaCredential.publisher(1, "adapter-key".toByteArray())
        assertEquals("drone1", credential.username)
        assertEquals("dde9291ecc6873df7c86e7597f085d9f88ccd75ab32b1613ca04c889032870d1", credential.password)
        assertTrue(!credential.toString().contains(credential.password))
        val client = WhipClient(timeoutMs = 2_000, credential = credential)
        client.use {
            val session = it.publish(url("/drone1/whip").toString(), offer)
            val publish = takeRequest(1, TimeUnit.SECONDS)!!
            val expectedAuthorization = "Basic ZHJvbmUxOmRkZTkyOTFlY2M2ODczZGY3Yzg2ZTc1OTdmMDg1ZDlmODhjY2Q3NWFiMzJiMTYxM2NhMDRjODg5MDMyODcwZDE="
            assertEquals(expectedAuthorization, publish.getHeader("Authorization"))
            val resource = requireNotNull(session.resourceUrl)
            assertEquals(200, it.delete(resource))
            val delete = takeRequest(1, TimeUnit.SECONDS)!!
            assertEquals("DELETE", delete.method)
            assertEquals("/drone1/whip/abc", delete.path)
            assertEquals(expectedAuthorization, delete.getHeader("Authorization"))
            assertEquals(404, it.delete(resource))
            assertNull(it.delete("http://127.0.0.1:1/drone1/whip/abc"))
            assertNull(it.delete("garbage"))
        }
    }

    @Test
    fun `missing or failed network providers surface as network failures`() {
        val unavailable = assertThrows(WhipException::class.java) {
            WhipClient(clientProvider = { null }).publish("http://127.0.0.1:8889/drone1/whip", offer)
        }
        assertNull(unavailable.status)
        assertTrue(unavailable.message!!.contains("bound media network unavailable"))

        val failed = assertThrows(WhipException::class.java) {
            WhipClient(clientProvider = { error("binding replaced") }).publish("http://127.0.0.1:8889/drone1/whip", offer)
        }
        assertNull(failed.status)
        assertTrue(failed.message!!.contains("binding replaced"))
    }

    @Test
    fun `resource resolution rules`() {
        val whip = "http://10.10.1.60:8889/drone1/whip".toHttpUrl()
        assertEquals("http://10.10.1.60:8889/drone1/whip/abc", WhipClient.resolveResource(whip, "/drone1/whip/abc"))
        assertEquals("http://10.10.1.60:8889/drone1/whip/abc", WhipClient.resolveResource(whip, "abc"))
        assertNull(WhipClient.resolveResource(whip, "http://other:1/x"), "a Location origin cannot select where credentials are sent")
        assertNull(WhipClient.resolveResource(whip, "  "))
    }
}
