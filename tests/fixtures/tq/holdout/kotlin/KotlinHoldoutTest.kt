package example.holdout

import app.cash.paparazzi.Paparazzi
import assertk.assertThat
import assertk.assertions.containsExactly
import assertk.assertions.isEqualTo
import assertk.assertions.isFalse
import assertk.assertions.isNotNull
import assertk.assertions.isTrue
import io.mockk.coVerify
import io.mockk.confirmVerified
import io.mockk.every
import io.mockk.mockk
import io.mockk.util.assertArrayEquals
import io.mockk.verify
import io.mockk.verifySequence
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.testing.TestBase
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.yield
import org.junit.Rule
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.extension.AfterAllCallback
import org.junit.jupiter.api.extension.ExtendWith
import org.junit.jupiter.api.extension.ExtensionContext
import kotlin.test.BeforeTest
import kotlin.test.assertContains
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertTrue
import kotlin.test.fail

class KotlinHoldoutTest : TestBase() {
    private val mock = mockk<Service>()
    private val iterations = 2000 * stressMultiplier
    private lateinit var ops: List<() -> Unit>

    @BeforeTest
    fun setUp() {
        ops = listOf(
            { state.call() },
            { state.done() },
        )
    }

    @Test
    fun expressionBodyRunTestAsync() = runTestAsync {
        val result = fetchResult()
        assertIs<String>(result)
        assertEquals(3, result.length)
    }

    private fun withTestScope(block: TestScope.() -> Unit) {
        val job = Job()
        TestScope(job).block()
        job.cancel()
    }

    @Test
    fun expressionBodyWithTestScope() = withTestScope {
        val actual = assertFailsWith<IllegalStateException> { emptyChannel().takeItem() }
        assertEquals("Closed", actual.message)
    }

    private fun runTestTurbine(validate: suspend TurbineTestScope.() -> Unit) = runTest {
        turbineScope {
            val scope = object : TurbineTestScope {}
            scope.validate()
        }
    }

    @Test
    fun expressionBodyRunTestTurbine() = runTestTurbine {
        val actual = assertFailsWith<AssertionError> { awaitItem() }
        assertEquals("Timed out", actual.message)
    }

    @Test
    fun assertkChainsAndApply() {
        val list = listOrNull("dir")
        assertThat(list).isNotNull().containsExactly("nested.txt")
        metadata().apply {
            assertThat(isRegularFile).isTrue()
            assertThat(isDirectory).isFalse()
            assertThat(size).isEqualTo(7L)
        }
    }

    @Test
    fun importedAssertArrayEquals() {
        assertArrayEquals(LongArray(0), makeEmptyLongArray())
    }

    private suspend fun nestedMethod(traces: List<String>) {
        oneMoreNestedMethod(traces)
        assertTrue(true) // Prevent tail-call optimization
    }

    private suspend fun oneMoreNestedMethod(traces: List<String>) {
        try {
            awaitWork()
            expectUnreached()
        } catch (e: RuntimeException) {
            verifyStackTrace(e, traces)
        }
    }

    @Test
    fun tailCallGuardInHelper() = runTest {
        nestedMethod(listOf("frame1"))
    }

    @Test
    fun tailCallGuardInAsync() = runTest {
        val d = async {
            suspendingMethod()
            assertTrue(true)
        }
        yield()
        verifyDump()
        d.cancelAndJoin()
    }

    private fun verifyDump() {
        assertEquals(1, activeCount())
    }

    @Test
    fun testBaseExpectFinishAndObject() {
        expect(1)
        flowOf(42).subscribe(object : Subscriber<Int> {
            override fun onNext(t: Int) {
                expect(2)
                assertEquals(42, t)
            }
            override fun onError(t: Throwable?) {
                expectUnreached()
            }
        })
        finish(3)
    }

    @Test
    fun expectUnreachedInCallback() = runTest {
        val ch = makeChannel(onUndelivered = {
            expectUnreached()
        })
        ch.close()
        ch.trySend(1)
    }

    @Test
    fun expectUnreachedInTryCatch() = runTest {
        try {
            awaitResult()
            expectUnreached()
        } catch (e: TestException) {
            assertIs<TestException2>(e.cause)
        }
    }

    @Test
    fun mockkVerifyAndConfirmVerified() = runTest {
        coVerify(exactly = 1) { mock.coOp() }
        verify(exactly = 0) { mock.other() }
        confirmVerified(mock)
    }

    @Test
    fun mockkVerifySequenceAndConfirmVerified() {
        doCalls()
        verifySequence {
            mock.op(1)
            mock.op(2)
        }
        confirmVerified(mock)
    }

    private suspend fun runAllKinds(block: suspend (ChannelKind) -> Unit) {
        for (kind in ChannelKind.values()) {
            if (kind.viaBroadcast) continue
            try {
                withContext(Job()) {
                    block(kind)
                }
            } catch (e: Throwable) {
                error("$kind: $e", e)
            }
        }
    }

    @Test
    fun loopOverFilteredEnum() = runTest {
        runAllKinds { kind ->
            assertEquals(0, kind.capacity)
        }
    }

    @Test
    fun repeatWithStressMultiplier() = runTest {
        repeat(iterations) {
            assertEquals(20, computeSum())
        }
    }

    @Test
    fun loopOverBeforeTestList() {
        for (op in ops) {
            assertFailsWith<IllegalStateException> { op() }
        }
    }

    private fun expectVerificationError(vararg messages: String, block: () -> Unit) {
        try {
            clearMocks(mock)
            block()
            fail("Block should throw verification failure")
        } catch (ex: AssertionError) {
            if (messages.any { !ex.message!!.contains(it) }) {
                fail("Bad message: " + ex.message)
            }
        }
    }

    @Test
    fun helperFailInTryChecksMessage() {
        expectVerificationError("was not called") {
            verify { mock.op(1) }
        }
    }

    @Test
    fun inlineIfFailInTryCatch() {
        try {
            verify(exactly = 2) { mock.op(1) }
            fail("Block should throw verification failure")
        } catch (ex: AssertionError) {
            val message = ex.message!!
            if (!message.contains("One matching call found")) {
                fail("Bad message: " + message)
            }
        }
    }

    @Test
    fun throwingFakeSourceIsCheck() {
        val source = object : ForwardingSource() {
            override fun read(): Long {
                throw AssertionError()
            }
        }
        sink.write(source, 0)
        assertEquals(0, data.size)
    }

    @Test
    fun paparazziSnapshotCheck() {
        val paparazzi = Paparazzi()
        paparazzi.snapshot(renderView())
    }

    // --- Negative controls (must STILL flag) ---
    @Test
    fun tryCatchWithNoFailOnNormalReturn() = runTest {
        try {
            withTimeout(1_000) { receiveItem() }
        } catch (e: TimeoutCancellationException) {
            assertContains(e.message!!, "virtual")
        }
    }

    @Test
    fun stubWithoutCall() {
        val myMock: Service = mockk()
        every { myMock.foo() } returns "bar"
    }

    @Test
    fun sameParseBothSides() {
        assertEquals("C:x\\..\\z".toPath(), "C:x\\..\\z".toPath())
    }

    @Test
    fun bareConstTruth() {
        assertTrue(true)
    }

    @Test
    fun unrecordedMockAbsence() {
        val unused = mockk<Service>()
        verify(exactly = 0) { unused.op(1) }
    }
}

class RuleValidatedTest {
    @Rule
    @JvmField
    val validation = TestFailureValidation(
        TestResultSpec("successfulTest"),
    )

    @Test
    fun successfulTest() = runBlocking {
        val job = launch { yield() }
        job.join()
    }
}

abstract class BaseExtensionTest {
    @Test
    open fun stubAndCallMocks() {
        val m = mockk<Service>()
        every { m.foo() } returns "x"
        m.foo()
    }

    class AssertMocksCollected : AfterAllCallback {
        override fun afterAll(context: ExtensionContext) {
            assertTrue(collected())
        }
    }

    @ExtendWith(AssertMocksCollected::class)
    class PerClassTest : BaseExtensionTest() {
        @Test
        override fun stubAndCallMocks() = super.stubAndCallMocks()
    }
}
