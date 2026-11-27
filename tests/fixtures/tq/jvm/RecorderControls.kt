import kotlin.test.Test

class RecorderControls {
    private val kills = mutableListOf<Int>()
    private val search = mockk<Search>()
    private val unrelated = mockk<Search>()

    @Test fun finishedTimer() {
        assertThat(kills).isEmpty()
    }

    @Test fun timeout() {
        assertThat(kills).containsExactly(555)
    }

    @Test fun mobileOff() {
        coVerify(exactly = 0) { search.onAction(any()) }
        coVerify(exactly = 0) { unrelated.onAction(any()) }
    }

    @Test fun mobileOn() {
        coVerify(exactly = 1) { search.onAction(any()) }
    }

    @Test fun renamedRig() {
        val wire = EgressProxyRig()
        assertTrue(wire.directPayloads.isEmpty())
        assertTrue(wire.failures.isEmpty())
    }

    @Test fun positiveRig() {
        val rig = EgressProxyRig()
        assertEquals("payload", rig.directPayloads.poll())
    }

    @Test fun resultEmpty() {
        val again = repository.add("existing")
        assertTrue(again.insertions.isEmpty())
        val invalid = entries.filter { malformed(it) }
        assertTrue(invalid.isEmpty())
    }
}

class EgressProxyRig {
    val directPayloads = Queue<String>()
    val failures = Queue<String>()
}
