package org.example.app;

import static com.google.common.truth.Truth.assertThat;
import static org.assertj.core.api.Assertions.assertThat;
import static org.example.support.LangAssertions.assertIllegalArgumentException;
import static org.example.support.LangAssertions.assertNullPointerException;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verifyNoMoreInteractions;

import org.assertj.core.internal.Throwables;
import org.example.support.SupportHelper;
import org.junit.jupiter.api.Test;
import reactor.test.StepVerifier;

class HelpersAndFrameworksTest {
    private Throwables throwables = new Throwables();

    @Test
    void validateIsProductNotAssertion() {
        Validate.isTrue(true);
    }

    @Test
    void staticAssertHelper() {
        assertNullPointerException(() -> service.call(null));
        assertIllegalArgumentException(() -> service.call(-1));
    }

    @Test
    void instanceAssertHelper() {
        throwables.assertHasCauseInstanceOf(err, IllegalStateException.class);
    }

    @Test
    void mockMvcAndExpect() throws Exception {
        mockMvc.perform(get("/items/1"))
            .andExpect(status().isOk())
            .andExpect(header().stringValues("X-Item"));
    }

    @Test
    void noExceptionInNameWithAndExpect() throws Exception {
        mockMvc.perform(get("/items/1"))
            .andExpect(status().isOk());
    }

    @Test
    void stepVerifierChecks() {
        StepVerifier.create(service.stream())
            .expectNext("a")
            .verifyComplete();
        StepVerifier.create(service.failing())
            .expectError(IllegalStateException.class)
            .verify();
    }

    @Test
    void stepVerifierInLambda() {
        checkMono(step -> step.expectNext("x").verifyComplete());
    }

    @Test
    void mockitoInOrderChecksAndControls() {
        Listener a = mock(Listener.class);
        Listener b = mock(Listener.class);
        runListeners(a, b);
        var order = inOrder(a, b);
        order.verify(a).opened();
        order.verify(b).opened();
        verifyNoMoreInteractions(a, b);
        order.verifyNoMoreInteractions();
    }

    @Test
    void fluentTransformsAndTerminals() {
        assertThat(array).asList().containsExactly("x", "y");
        assertThat(path.getFileName()).asString().startsWith("pre");
        assertThat(predicate).accepts("good");
        assertThat(predicate).rejects("bad");
        assertThat(invocations).match(hints);
    }

    @Test
    void helperInOtherClass() {
        localCheck("hello");
    }

    private void localCheck(String s) {
        SupportHelper.checkPair(s, "hello");
    }

    @Test
    void realEmptyBody() {
    }
}
