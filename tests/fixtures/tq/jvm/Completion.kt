import kotlin.test.Test

class Completion {
    @Test fun keepsTheKey() {
        val error = failure<IOException> { open() }
        assertEquals("not open", error.message)
    }

    private inline fun <reified T : Exception> failure(block: () -> Unit): T = try {
        block()
        fail("expected failure")
        throw AssertionError()
    } catch (error: Exception) {
        if (error !is T) throw AssertionError("unexpected failure")
        error
    }

    @Test fun keepsTheOldKey() {
        try {
            move()
            throw AssertionError("expected an IOException")
        } catch (error: IOException) {
            assertEquals("cannot move", error.message)
        }
        assertTrue(oldKey().exists())
    }

    @Test fun rejectsTypedProblem() {
        assertEquals(listOf<SeedProblem>(SeedProblem.BadName), problems())
    }

    @Test fun completesUi() {
        composeRule.waitUntil(5_000) { finished }
    }

    @Test fun completesRepository() {
        withTimeout(5_000) {
            while (repo.searches() != listOf("Wetter morgen") || repo.sites() != listOf("example.de")) {
                delay(20)
            }
        }
    }

    @Test fun waitsForRows() {
        awaitUntil(what = "rows deleted") { dao.snapshot.isEmpty() }
    }

    private fun awaitUntil(what: String, condition: () -> Boolean) {
        val end = deadline()
        while (!condition()) {
            if (System.currentTimeMillis() >= end) fail("timed out waiting for $what")
            Thread.sleep(5)
        }
    }
}
