package org.assertj.core.api;

import static java.lang.Boolean.FALSE;
import static java.lang.Boolean.TRUE;
import static org.assertj.core.api.Assertions.assertAny;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.BDDAssertions.thenExceptionOfType;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.function.Executable;

class BoolAssertionsTest {
    private WithAssertions withAssertions = new WithAssertions();

    @Test
    void booleansNotEqual() {
        assertThat(true).isNotEqualTo(FALSE);
        assertThat(TRUE).isEqualTo(TRUE);
    }

    @Test
    void softAutoCloseablePasses() {
        try (AutoCloseableSoftAssertions softly = new AutoCloseableSoftAssertions()) {
            softly.assertThat(1).isEqualTo(1);
        }
    }

    @Test
    void bddSoftAutoCloseablePasses() {
        try (AutoCloseableBDDSoftAssertions softly = new AutoCloseableBDDSoftAssertions()) {
            softly.then(1).isEqualTo(1);
        }
    }

    @Test
    void assertionHelpersInLibrary() {
        Executable[] items = { () -> {} };
        assertAny(items);
        withAssertions.assertThatThrownBy(() -> { throw new IllegalStateException("boom"); })
            .isInstanceOf(IllegalStateException.class);
        thenExceptionOfType(IllegalArgumentException.class)
            .isThrownBy(() -> { throw new IllegalArgumentException("bad"); })
            .withMessage("bad");
    }

    @Test
    void softAutoCloseableEarlyReturn() {
        try (AutoCloseableSoftAssertions softly = new AutoCloseableSoftAssertions()) {
            softly.assertThat(1).isEqualTo(1);
            return;
        }
    }
}
