package example.h4

import kotlin.test.assertEquals

fun <T> checkMaybeValue(value: T?, checker: (T?) -> Unit) {
    checker(value)
}

fun verifyDump(vararg expectedTraces: String, ignoredCoroutine: String? = null) {
    val dumps = collectDumps(ignoredCoroutine)
    assertEquals(expectedTraces.size, dumps.size)
}

fun assertGarbageCollected(refs: List<Any>, message: String) {
    repeat(10) {
        if (refs.all { it == null }) return
        System.gc()
    }
    assertEquals(0, refs.count { it != null }, message)
}

fun launchWithDeferredDispatch(block: suspend () -> Unit): Job = launch { block() }
