import kotlin.test.Test

class Spans {
    @Test
    fun `a custom DNS change starts the checks over`() = runTest {
        watching(rig) {
            assertFalse("the old probe result is dropped too", rig.blocked)
        }
    }

    @Test
    fun `a VPN change starts the checks over`() = runTest {
        work()
    }

    private suspend fun TestScope.watching(rig: Rig, body: suspend () -> Unit) {
        try { body() } finally { cleanup(rig) }
    }
}
