package example.knit

import kotlin.test.assertEquals

fun List<String>.verifyLines(vararg expected: String) {
    assertEquals(expected.size, size)
    for (i in indices) {
        assertEquals(expected[i], get(i))
    }
}
