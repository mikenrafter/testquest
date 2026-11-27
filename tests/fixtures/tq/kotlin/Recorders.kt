import kotlin.test.Test

class Recorders {
    @Test fun absence() {
        verify(exactly = 0) { sender.send(msg) } // absence
        verify { other.send(msg) }
    }
    @Test fun witnessed() {
        verify { sender.send(probe) }
        verify(exactly = 0) { sender.send(msg) }
    }
    @Test fun guarded() {
        if (enabled()) verify { sender.send(probe) }
        verify(exactly = 0) { sender.send(msg) } // absence
    }
    @Test fun shadowed() {
        val sender = mockk<Sender>()
        run {
            val sender = mockk<Sender>()
            verify { sender.send(probe) }
        }
        verify(exactly = 0) { sender.send(msg) } // absence
    }
    @Test fun blockedControl() {
        verify(exactly = 0) { sender.send(msg) } // absence
        if (enabled()) return
        verify { sender.send(probe) }
    }
    @Test fun empty() {
        assertTrue(events.isEmpty()) // absence
    }
    @Test fun collectionControl() {
        assertTrue(events.isNotEmpty())
        assertEquals(0, events.size)
    }
}
