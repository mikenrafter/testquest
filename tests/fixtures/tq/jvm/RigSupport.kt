package example

import kotlin.test.Test

class ProxyRig {
    val payloads = Queue<String>()
    val failures = Queue<String>()
}

class RigSupport {
    @Test fun receivesPayload() = ProxyRig().use { rig ->
        assertEquals("payload", rig.payloads.poll())
    }
}
