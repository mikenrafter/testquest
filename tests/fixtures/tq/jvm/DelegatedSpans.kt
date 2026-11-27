import kotlin.test.Test

class DelegatedSpans {
    @Test
    fun cancelledWipe() = runBlocking {
        val store = object : Store by real.store {
            override suspend fun update() { work() }
        }
        try { example.Rig(store).use { rig ->
            try {
                assertTrue(rig.closed())
            } finally { acknowledge.complete(Unit); wipe.cancelAndJoin() }
        } } finally { owner.close() }
    }

    @Test
    fun nextTest() = runBlocking {
        work()
    }
}
