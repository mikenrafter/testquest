package org.example.api;

import static org.assertj.core.api.Assertions.assertThat;

public final class EqualityAssertions {
    public static <T> void assertEqualsAndHashCode(T a, T b, T diff) {
        assertThat(a).isEqualTo(a);
        assertThat(a).isEqualTo(b);
        assertThat(a).isNotEqualTo(diff);
    }
}
