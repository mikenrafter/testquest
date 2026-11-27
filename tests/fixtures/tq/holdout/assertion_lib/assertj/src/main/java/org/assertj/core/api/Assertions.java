package org.assertj.core.api;

public class Assertions {
    public static <T> ObjectAssert<T> assertThat(T actual) { return new ObjectAssert<>(actual); }
}
