package org.example.sub;

import static org.assertj.core.api.Assertions.assertThat;

public class BaseAssertionTests {
    protected void compareFails(Object actual, Object expected) {
        assertThat(actual).isNotEqualTo(expected);
    }
}
