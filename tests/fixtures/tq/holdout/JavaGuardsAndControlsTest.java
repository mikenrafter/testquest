package org.example.guards;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.guava.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;
import static org.junit.jupiter.api.DynamicTest.dynamicTest;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.lang.reflect.Parameter;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.IntStream;
import java.util.stream.Stream;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.opentest4j.AssertionFailedError;

class JavaGuardsAndControlsTest {
    enum Kind { FIRST, SECOND }

    interface LocalApi {
        void ping(String value);
    }

    static class Target {
        public Target(String name) {}
    }

    static void expectAssertionFailedError() {
        throw new AssertionFailedError("expected AssertionFailedError");
    }

    @Test
    void tryFailAfterTryWithReturnInCatch() {
        try {
            assertThat(multimap).containsValues("a", "b");
        } catch (AssertionError e) {
            assertThat(e).hasMessage("missing b");
            return;
        }
        fail("Assertion error expected");
    }

    @Test
    void tryFailHelperInsideTry() {
        try {
            assertEquals("a", "b");
            expectAssertionFailedError();
        } catch (AssertionFailedError ex) {
            assertThat(ex.getMessage()).contains("expected");
        }
    }

    @Test
    void guardFailureConditionOnly() {
        String expected = "abc";
        String actual = render();
        if (!expected.equals(actual)) {
            assertEquals(expected, actual);
        }
    }

    @Test
    void intStreamRangeForEach() {
        IntStream.range(0, 10).parallel().forEach(i -> assertThat(actual).isEqualTo("ok"));
    }

    @Test
    void loopOverSystemPropertyDefaultCount() {
        for (int i = 0; i < Integer.getInteger("iters", 100); i++) {
            assertTrue(check(i));
        }
    }

    @Test
    void loopOverAddedExpectedListSize() {
        List<String> expected = new ArrayList<>();
        expected.add("a");
        expected.add("b");
        List<String> actual = decode();
        for (int i = 0; i < expected.size(); ++i) {
            assertEquals(expected.get(i), actual.get(i));
        }
    }

    private void assertClosed(Stub... stubs) {
        for (Stub s : stubs) {
            assertTrue(s.closed);
        }
    }

    @Test
    void helperLoopOverVarargs() {
        Stub stub = new Stub();
        closeAll(stub);
        assertClosed(stub);
    }

    @Test
    void loopOverFixedConstructorParameters() throws Exception {
        Parameter[] params = Target.class.getConstructor(String.class).getParameters();
        for (Parameter p : params) {
            assertNotNull(p);
        }
    }

    @Test
    void loopOverFixedDeclaredMethods() {
        for (Method m : LocalApi.class.getDeclaredMethods()) {
            assertNotNull(m);
        }
    }

    @Test
    void platformBranchesCoverAll() {
        if (new File("C:/myfile.txt").isAbsolute()) {
            assertEquals("C:/myfile.txt", parsePath("C:/myfile.txt"));
        }
        if (new File("/myfile.txt").isAbsolute()) {
            assertEquals("/myfile.txt", parsePath("/myfile.txt"));
        }
    }

    @Test
    void switchAllBranchesCheck() {
        Kind k = readKind();
        switch (k) {
            case FIRST:
                assertEquals(1, readValue());
                break;
            default:
                assertEquals(2, readValue());
        }
    }

    @Test
    void seededThenDrainedListAndStream() {
        List<String> items = new ArrayList<>();
        items.add("seed");
        items.clear();
        assertThat(items).isEmpty();

        InputStream stream = new ByteArrayInputStream(payload);
        consume(stream);
        assertThat(stream).isEmpty();
    }

    @Test
    void realProductOutputLoop() {
        String[][] rows = ProductTable.escapeRows();
        for (int i = 0; i < rows.length; i++) {
            assertNotNull(rows[i][0]);
        }
    }

    @Test
    void realAbsenceWithoutControl() {
        List<String> found = new ArrayList<>();
        walkJar(found);
        assertTrue(found.isEmpty());
    }
}

abstract class AbstractFactorySourceTest {
    abstract Stream<String> createItems();

    @TestFactory
    Stream<DynamicTest> serializableItems() {
        return createItems().map(item -> dynamicTest(item, () -> assertNotNull(item)));
    }
}
