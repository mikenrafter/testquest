package io.mockk.it

import io.mockk.coVerify
import io.mockk.confirmVerified
import io.mockk.mockk
import io.mockk.verify
import io.mockk.verifySequence
import kotlin.test.Test

class MockkConfirmTest {
    private val mock = mockk<Service>()

    @Test
    fun coVerifyControlsConfirmVerified() = runTest {
        coVerify(exactly = 1) { mock.coOp() }
        verify(exactly = 0) { mock.other() }
        confirmVerified(mock)
    }

    @Test
    fun verifySequenceControlsConfirmVerified() {
        verifySequence {
            mock.op(1)
            mock.op(2)
        }
        confirmVerified(mock)
    }

    @Test
    fun confirmVerifiedWithoutPositiveControlFlags() {
        val unused = mockk<Service>()
        verify(exactly = 0) { unused.other() }
        confirmVerified(unused)
    }
}
