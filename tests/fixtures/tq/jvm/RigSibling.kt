package example

import kotlin.test.Test

class RigSibling {
    @Test fun expectsNoPayload() = ProxyRig().use { wire ->
        assertTrue(wire.payloads.isEmpty())
        assertTrue(wire.failures.isEmpty())
    }
}
