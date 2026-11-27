package example

import kotlin.test.Test

class Wrappers {
    @Test fun adoptsInOrder() = layerTest {
        assertEquals("rotated", currentPersona())
    }

    private fun layerTest(body: suspend TestScope.() -> Unit) = runTest {
        try {
            body()
        } finally {
            close()
        }
    }

    @Test fun capturesRestore() = CapturingTree.capture { tree ->
        assertTrue(tree.restored())
    }

    @Test fun unresolved() = unknownHarness {
        assertEquals(result(), result())
    }

    @Test fun deferredOnly() = retain {
        assertEquals(result(), result())
    }

    private fun retain(body: () -> Unit) { pending = body }
}
