package org.example.support;

import static org.junit.jupiter.api.Assertions.assertEquals;

public class SupportHelper {
    public static void checkPair(String input, String expected) {
        assertEquals(expected, input);
    }
}
