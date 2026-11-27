package example.holdout

import kotlin.coroutines.CoroutineContext
import kotlin.coroutines.EmptyCoroutineContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.withContext
import kotlin.test.assertEquals

fun runTestAsync(
    context: CoroutineContext = EmptyCoroutineContext,
    block: suspend CoroutineScope.() -> Unit,
) = runTest(context) {
    withContext(Dispatchers.Default, block)
}

enum class ChannelKind(
    val capacity: Int,
    val viaBroadcast: Boolean = false,
) {
    RENDEZVOUS(0),
    BUFFERED_1(1),
    CONFLATED_BROADCAST(-1, viaBroadcast = true),
}

val stressMultiplierSqrt: Int = if (isStressTest) 5 else 1
val stressMultiplier: Int = stressMultiplierSqrt * stressMultiplierSqrt

fun verifyStackTrace(e: Throwable, traces: List<String>) {
    assertEquals(traces.size, e.stackTrace.size)
}
