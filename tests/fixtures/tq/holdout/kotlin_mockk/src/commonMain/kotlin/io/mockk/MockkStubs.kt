package io.mockk

fun verify(exactly: Int = -1, verifyBlock: () -> Unit) {}
fun coVerify(exactly: Int = -1, verifyBlock: suspend () -> Unit) {}
fun verifySequence(verifyBlock: () -> Unit) {}
fun confirmVerified(vararg mocks: Any) {}
