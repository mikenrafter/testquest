package org.junit.jupiter.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.assertj.core.api.Assertions;
import org.junit.jupiter.api.condition.EnabledIf;

class JupiterAssertionsTest {
    @Test
    void assertionsUnderTest() {
        Object token = new Object();
        assertTrue(true);
        assertFalse(false);
        assertEquals(0.42d, 0.24d, 0.19d);
        assertEquals(1.0d, 1.0d);
        assertSame(token, token);
    }

    @Test
    @EnabledIf("enabled")
    void realEmptyEnabledTest() {
    }

    @Test
    void realAbsenceUsesForeignAssertJ() {
        Assertions.assertThat(TrackingWatcher.events).isEmpty();
    }
}
