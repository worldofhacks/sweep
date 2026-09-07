package org.worldofhacks.sweep.bridge.node

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertInstanceOf
import org.junit.jupiter.api.Test

class GimbalPitchCompletionTest {
    @Test
    fun `action callback alone never completes a gimbal request`() {
        val completion = GimbalPitchCompletion()
        val request = assertInstanceOf(GimbalPitchCompletion.Start.Begun::class.java, completion.begin(-75.0, 100)).request

        assertInstanceOf(GimbalPitchCompletion.Outcome.Accepted::class.java, completion.accepted(request.id, 150))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Ignored::class.java, completion.observe(-75.0, 149))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Ignored::class.java, completion.observe(-71.0, 151))
        val completed = assertInstanceOf(GimbalPitchCompletion.Outcome.Completed::class.java, completion.observe(-74.0, 152))

        assertEquals(request.id, completed.request.id)
        assertEquals(-74.0, completed.observedDegrees)
    }

    @Test
    fun `failed action and deadline both fail the active request`() {
        val completion = GimbalPitchCompletion(timeoutMs = 500)
        val rejected = assertInstanceOf(GimbalPitchCompletion.Start.Begun::class.java, completion.begin(-45.0, 100)).request
        assertInstanceOf(GimbalPitchCompletion.Outcome.Failed::class.java, completion.rejected(rejected.id, "SDK denied rotation"))

        val timedOut = assertInstanceOf(GimbalPitchCompletion.Start.Begun::class.java, completion.begin(-75.0, 1_000)).request
        val failure = assertInstanceOf(GimbalPitchCompletion.Outcome.Failed::class.java, completion.expire(timedOut.id, 1_500))
        assertEquals("gimbal attitude confirmation timed out", failure.reason)
    }

    @Test
    fun `busy and stale callbacks cannot change the current request`() {
        val completion = GimbalPitchCompletion()
        val first = assertInstanceOf(GimbalPitchCompletion.Start.Begun::class.java, completion.begin(-45.0, 100)).request
        assertInstanceOf(GimbalPitchCompletion.Start.Busy::class.java, completion.begin(-75.0, 101))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Ignored::class.java, completion.accepted(first.id + 1, 110))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Accepted::class.java, completion.accepted(first.id, 110))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Ignored::class.java, completion.accepted(first.id, 112))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Ignored::class.java, completion.observe(-45.0, 109))
        assertInstanceOf(GimbalPitchCompletion.Outcome.Completed::class.java, completion.observe(-45.0, 111))
    }
}

class GimbalPitchOperationCoordinatorTest {
    @Test
    fun `terminal operation stays attached when the next request begins`() {
        val coordinator = GimbalPitchOperationCoordinator<String>()
        val first = assertInstanceOf(
            GimbalPitchOperationCoordinator.Start.Begun::class.java,
            coordinator.begin(-45.0, 100, "first"),
        ).active
        assertInstanceOf(GimbalPitchOperationCoordinator.Start.Busy::class.java, coordinator.begin(-75.0, 101, "second"))
        assertInstanceOf(GimbalPitchOperationCoordinator.Update.Accepted::class.java, coordinator.accepted(first.request.id, 110))

        val complete = assertInstanceOf(
            GimbalPitchOperationCoordinator.Update.Completed::class.java,
            coordinator.observed(-45.0, 111),
        )
        val second = assertInstanceOf(
            GimbalPitchOperationCoordinator.Start.Begun::class.java,
            coordinator.begin(-75.0, 112, "second"),
        ).active

        assertEquals("first", complete.active.operation)
        assertEquals("second", second.operation)
    }

    @Test
    fun `deadline boundaries fail and leave no active operation`() {
        val coordinator = GimbalPitchOperationCoordinator<String>(GimbalPitchCompletion(timeoutMs = 100))
        val first = assertInstanceOf(
            GimbalPitchOperationCoordinator.Start.Begun::class.java,
            coordinator.begin(-45.0, 100, "first"),
        ).active
        assertInstanceOf(GimbalPitchOperationCoordinator.Update.Failed::class.java, coordinator.accepted(first.request.id, 200))
        val second = assertInstanceOf(
            GimbalPitchOperationCoordinator.Start.Begun::class.java,
            coordinator.begin(-75.0, 300, "second"),
        ).active
        assertInstanceOf(GimbalPitchOperationCoordinator.Update.Accepted::class.java, coordinator.accepted(second.request.id, 301))
        assertInstanceOf(GimbalPitchOperationCoordinator.Update.Failed::class.java, coordinator.observed(-75.0, 400))
        assertInstanceOf(
            GimbalPitchOperationCoordinator.Start.Begun::class.java,
            coordinator.begin(-45.0, 401, "third"),
        )
    }
}
