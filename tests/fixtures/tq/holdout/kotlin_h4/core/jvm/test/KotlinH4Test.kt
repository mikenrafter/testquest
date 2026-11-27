package example.h4

import app.cash.turbine.Turbine
import assertk.assertThat
import assertk.assertions.containsExactlyInAnyOrder
import assertk.assertions.isEmpty
import example.coroutines.CoroutineStart
import example.knit.*
import example.testing.stressTestMultiplierSqrt
import io.mockk.coVerifyAny
import io.mockk.every
import io.mockk.mockk
import io.mockk.verify
import io.mockk.bdd.coThen
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import okio.fakefilesystem.FakeFileSystem
import org.junit.Assert
import org.junit.jupiter.api.Disabled
import org.junit.jupiter.api.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class KotlinH4Test : TestBase() {
    private val createdMocks = mutableListOf<Any>()

    private class Snapshot {
        fun read(): String = "ABC"
    }

    private fun Snapshot.assertValue(expected: String) {
        assertEquals(expected, read())
    }

    @Test
    fun memberExtensionHelper() {
        val snapshot = Snapshot()
        snapshot.assertValue("ABC")
    }

    @Test
    fun importedVarargExtensionHelper() {
        runGuide("Ex01").verifyLines("line1", "line2")
    }

    @Test
    fun callableReferencePassedToHelper() {
        checkMaybeValue(null, Assert::assertNull)
    }

    private suspend fun decodeBitmap(name: String, size: Size, scale: Int = 1): String =
        assertIs<String>(decode(name, size, scale))

    private suspend fun decodeBitmap(name: String, options: Options, scale: Int = 1): String =
        assertIs<String>(decode(name, options, scale))

    @Test
    fun overloadedDefaultParamAssertIs() = runTest {
        decodeBitmap("large.webp", Size(10, 10))
    }

    private class MethodChecker(private val expected: Boolean) {
        private var count = 0
        fun call() { count++ }
        fun complete(name: String) {
            require(count in 0..1) { "$name called $count" }
            if (expected) require(count == 1) else require(count == 0)
        }
    }

    private class TestEventListener(val onStart: MethodChecker = MethodChecker(true)) {
        fun complete() { onStart.complete("onStart") }
    }

    @Test
    fun localClassHelperWithRequire() {
        val listener = TestEventListener()
        runImageLoad(listener)
        listener.complete()
    }

    @Test
    fun fakeFileSystemCheckNoOpenFiles() {
        val fs = FakeFileSystem()
        val sink = fs.sink("/a")
        sink.close()
        fs.checkNoOpenFiles()
    }

    @Test
    fun mockkCoVerifyAnyAndCoThen() = runBlocking {
        val mock = mockk<Service>()
        coVerifyAny {
            mock.op(1)
            mock.op(2)
        }
        coThen { mock.op(3) }
    }

    @Test
    fun turbineAwaitComplete() = runTest {
        val ch = Turbine<Int>()
        ch.close()
        ch.awaitComplete()
    }

    private fun runTestTurbine(validate: suspend TurbineTestScope.() -> Unit) = runTest {
        turbineScope {
            val turbineTestScope =
                object : TurbineTestScope, TurbineContext by this {
                    override val backgroundScope = this@runTest.backgroundScope
                }
            turbineTestScope.validate()
        }
    }

    @Test
    fun turbineScopeWithDelegationByThis() = runTestTurbine {
        assertTrue(true)
        assertEquals(1, compute())
    }

    @Test
    fun flowFirstWithPredicate() = runTest {
        val ready = MutableStateFlow(false)
        trigger(ready)
        ready.first { it }
    }

    private suspend fun activeMethod() {
        nestedMethod()
        assertTrue(true) // tail-call
    }

    @Test
    fun tailCallGuardWithErrorGuard() = runBlocking {
        val d = async { activeMethod() }
        val result = readDump()
        if (!result.startsWith("expected")) {
            error("Unexpected dump")
        }
        d.cancel()
    }

    @Test
    fun tailCallGuardWithVarargVerifyDump() = runBlocking {
        val d = async { activeMethod() }
        d.cancel()
        verifyDump(ignoredCoroutine = "Blocking")
    }

    @Test
    fun enumEntriesFromMultiplatformCommonSrc() = runTest {
        CoroutineStart.entries.forEach { testCancelledParent(it) }
    }

    private suspend fun testCancelledParent(start: CoroutineStart) {
        try {
            expect(1)
            startWork(start)
            expectUnreached()
        } catch (_: CancellationException) {
        }
    }

    @Test
    fun joinedLaunchWithDeferredDispatch() = runTest {
        withContext(Dispatchers.Default) {
            launchWithDeferredDispatch {
                assertEquals(1, dispatchCount())
            }.join()
        }
    }

    @Test
    fun stressMultiplierAndWithContextLaunch() = runTest {
        repeat(100 * stressTestMultiplierSqrt) {
            withContext(Dispatchers.Default) {
                repeat(100) {
                    launch {
                        assertEquals(1, checkThread())
                    }
                }
            }
        }
    }

    @Test
    fun resumeModeSuspendingWithOverloadedWithContext() = runTest {
        testResumeMode(Dispatchers.Unconfined)
    }

    private suspend fun testResumeMode(ctx: Any) {
        try {
            val ch = Channel<Int>()
            val latch = Channel<Int>()
            GlobalScope.launch {
                latch.receive()
                expect(3)
                ch.close()
            }
            doSuspendingPath(ctx, ch, latch)
        } catch (e: Throwable) {
            finish(4)
            verifyDump("trace")
        }
    }

    private suspend fun doSuspendingPath(ctx: Any, ch: Channel<Int>, latch: Channel<Int>) {
        withContext(ctx, ch, latch)
    }

    private suspend fun withContext(ctx: Any, ch: Channel<Int>) {
        ch.receive()
    }

    private suspend fun withContext(ctx: Any, ch: Channel<Int>, latch: Channel<Int>) {
        withContext(ctx) {
            expect(1)
            latch.send(1)
            expect(2)
            ch.receive()
        }
    }

    @Test
    fun assertGarbageCollectedOnFixedMocks() {
        val refs = List(100) { mockk<Service>() }
        assertGarbageCollected(refs, "never GCd")
    }

    @Test
    fun longLiteralRangeLoop() {
        for (seed in 0L until 100L) {
            assertEquals(seed, echo(seed))
        }
    }

    @Test
    fun absenceWithLaterContainsExactlyInAnyOrder() {
        val processed = mutableListOf<String>()
        readFile(processed)
        assertThat(processed).isEmpty()
        listFiles(processed)
        assertThat(processed).containsExactlyInAnyOrder("a", "b")
    }

    @Test
    fun absenceWithFixtureSuccessAssertion() {
        val result = runNestedTest("createsMocks")
        assertSucceeded(result)
        val refs = createdMocks.toList()
        createdMocks.clear()
        assertGarbageCollected(refs, "released")
    }

    @Disabled("fixture")
    class ReleaseMocksFixture {
        @Test
        fun createsMocks() {
            createdMocks.add(mockk<Service>())
        }
    }

    private fun assertSucceeded(result: Any) {
        assertEquals("OK", result.toString())
    }

    // --- Negative controls (must STILL flag) ---
    private fun checkCancelling(schedule: (Runnable) -> Unit) {
        schedule {
            throw IllegalStateException("late")
        }
    }

    @Test
    fun checkCancellingWithoutSyncCheckFlagsNoAssert() {
        val s = makeScheduler()
        checkCancelling(s::scheduleDirect)
    }

    @Test
    fun stubWithoutCallFlagsNoAssert() {
        val m = mockk<Service>()
        every { m.op(1) } returns 2
    }

    @Test
    fun standaloneConstTruthStillFlags() {
        assertTrue(true)
    }

    @Test
    fun whileLoopMaySkipFlagsGuardedAssert() {
        while (dispatcher.isIdle()) {
            assertEquals(1, readCount())
        }
    }

    @Test
    fun tryCatchWithoutTryCheckFlagsGuardedAssert() {
        try {
            doWork()
        } catch (e: Throwable) {
            assertTrue(e.message != null)
        }
    }

    @Test
    fun discardedRunTestsResultFlagsAbsenceNoControl() {
        runTests()
        val refs = createdMocks.toList()
        createdMocks.clear()
        assertGarbageCollected(refs, "released")
    }

    @Test
    fun verifyZeroWithoutPositiveMockControlFlagsAbsence() {
        val spy = mockk<Service>()
        assertTrue(spy.compares > 0)
        verify(exactly = 0) { spy.compareTo(any()) }
    }
}
