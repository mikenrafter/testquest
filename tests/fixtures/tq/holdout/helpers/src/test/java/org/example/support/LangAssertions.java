package org.example.support;

import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.function.Executable;

public class LangAssertions {
    public static void assertNullPointerException(Executable exec) {
        assertThrows(NullPointerException.class, exec);
    }

    public static void assertIllegalArgumentException(Executable exec) {
        assertThrows(IllegalArgumentException.class, exec);
    }
}
