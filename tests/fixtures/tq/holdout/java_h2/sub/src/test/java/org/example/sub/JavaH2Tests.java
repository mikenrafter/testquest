package org.example.sub;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.DynamicContainer.dynamicContainer;
import static org.junit.jupiter.api.DynamicTest.dynamicTest;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;

import com.google.common.truth.Expect;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TimeZone;
import java.util.function.BiConsumer;
import java.util.stream.Stream;
import org.assertj.core.api.AbstractSoftAssertions;
import org.assertj.core.api.StandardSoftAssertionsProvider;
import org.example.api.ClassMode;
import org.example.api.Config;
import org.example.api.EqualityAssertions;
import org.example.api.Mode;
import org.junit.Assert;
import org.junit.Rule;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EmptySource;
import org.skyscreamer.jsonassert.JSONAssert;
import org.skyscreamer.jsonassert.JSONCompareMode;

class JavaH2Tests extends BaseAssertionTests {
    @Rule
    public final Expect expect = Expect.create();

    Conn conn1 = mock();
    Conn conn2 = mock();

    interface Conn { void close(); }
    interface Service { List<String> run(int id); }
    static class ServiceImpl implements Service {
        private int count = 0;
        public List<String> run(int id) {
            if (count > 0) {
                throw new AssertionError("Called too many times");
            }
            count++;
            return List.of();
        }
    }

    static class CustomSoft extends AbstractSoftAssertions implements StandardSoftAssertionsProvider {}
    private final CustomSoft softly = new CustomSoft();

    static final String[] EMPTY_ARR = new String[0];
    private static final Map<String, String> MODES = Map.of("a", "1", "b", "2");

    @Test
    void truthExpectRuleIsCheck() {
        expect.that(compute(1)).isEqualTo(2);
    }

    @Test
    void customAssertJExtractingTransform() {
        assertThat(wrapModel("v")).extractingBindingResult("item").hasSize(1);
    }

    @Test
    void oneArgLocalAssertEqualsHelper() throws Exception {
        writeOutput("{\"k\":1}");
        assertEquals("{\"k\":1}");
    }

    private static void assertEquals(String expected) throws Exception {
        String actual = readOutput();
        JSONAssert.assertEquals(expected, actual, JSONCompareMode.LENIENT);
    }

    @Test
    void inheritedHelperFromSuperclass() {
        compareFails("a", "b");
    }

    @Test
    void assertAllOverMappedStream() {
        assertOptionsThrow("-a", "-b");
    }

    private void assertOptionsThrow(String... opts) {
        assertAll(Stream.of(opts).map(opt -> () -> assertThrows(IllegalArgumentException.class, () -> parseOpt(opt))));
    }

    @Test
    void compileHelperRunsCallback() {
        compile((actual, compiled) -> assertThat(actual).isNotNull());
    }

    private void compile(BiConsumer<String, Object> consumer) {
        TestCompiler.forSystem().compile(compiled -> consumer.accept("ok", compiled));
    }

    @Test
    void serviceImplThrowsOnSecondCall() {
        Service svc = getService();
        svc.run(1);
        svc.run(1);
    }

    @Test
    void mapKeySetLoopIsNonEmpty() {
        for (String key : MODES.keySet()) {
            assertNotNull(key);
        }
    }

    @TestFactory
    Stream<DynamicNode> mapEntrySetFactoryIsNonEmpty() {
        var entries = Map.of("k1", "v1", "k2", "v2");
        return entries.entrySet().stream().map(entry ->
                dynamicContainer(entry.getKey(),
                        Stream.of("x", "y").map(rep -> dynamicTest(rep, () -> assertNotNull(entry.getValue())))));
    }

    @Test
    void timeZoneIdsLoopIsNonEmpty() {
        for (String tz : TimeZone.getAvailableIDs()) {
            assertNotNull(tz);
        }
    }

    @Test
    void crossModuleEnumValuesIsNonEmpty() {
        for (Mode mode : Mode.values()) {
            assertNotNull(mode);
        }
    }

    @Test
    void populatedMapLoopInHelper() {
        checkCapture("a", "1", "b", "2");
    }

    private void checkCapture(String... kv) {
        Map<String, String> expected = new HashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            expected.put(kv[i], kv[i + 1]);
        }
        for (Map.Entry<String, String> entry : expected.entrySet()) {
            assertNotNull(entry.getValue());
        }
    }

    @Test
    void catchRethrowsViaHelper() {
        loadOrThrow("x");
    }

    private static void loadOrThrow(String s) {
        try {
            assertThat(parseOpt(s)).isEqualTo(s);
        } catch (Exception ex) {
            ExceptionUtils.throwAsUncheckedException(ex);
        }
    }

    @Test
    void earlyReturnSearchWithTrailingFail() {
        assertContains(decodeItems(), "ok");
    }

    private static <T> void assertContains(List<T> items, T target) {
        for (T item : items) {
            if (item.equals(target)) {
                return;
            }
        }
        Assert.fail("missing");
    }

    @Test
    void reflectionConstantStreamIsNonEmpty() {
        Arrays.stream(Config.class.getFields())
                .filter(f -> Modifier.isStatic(f.getModifiers()))
                .filter(f -> f.getName().startsWith("MODE_"))
                .forEach(f -> assertNotNull(f.getName()));
    }

    @Test
    void helperListBuiltInNestedLoops() {
        for (Integer x : boundaryValues()) {
            assertNotNull(x);
        }
    }

    private static List<Integer> boundaryValues() {
        int[] xs = {1, 2};
        int[] ys = {3, 4};
        List<Integer> out = new ArrayList<>();
        for (int x : xs) {
            for (int y : ys) {
                out.add(x + y);
            }
        }
        return out;
    }

    @Test
    void mockitoFieldMocksSameTypeWitness() {
        conn1.close();
        verify(conn1).close();
        verifyNoInteractions(conn2);
    }

    @Test
    void seededWithAddPropertyThenCleared() {
        JsonObject o = new JsonObject();
        o.addProperty("a", 1);
        Map<String, Object> m = o.asMap();
        m.clear();
        assertThat(m).hasSize(0);
        assertThat(o.size()).isEqualTo(0);
    }

    @Test
    void helperPassedProductResultNotRecorder() {
        Diff diff = computeDiff("a", "a");
        assertNoDiff(diff);
    }

    private static void assertNoDiff(Diff d) {
        assertThat(d.missing).isEmpty();
    }

    @Test
    void helperDrainsLocalMapBeforeZeroCheck() {
        checkLocalMap("a");
    }

    private static void checkLocalMap(String k) {
        Map<String, String> m = new HashMap<>();
        m.put(k, "1");
        m.remove(k);
        assertThat(m.size()).isEqualTo(0);
    }

    @Test
    void castAndStaticEmptyArrayNotRecorders() {
        String[] arr = (String[]) EMPTY_ARR;
        assertEquals(0, arr.length);

        JsonObject obj = (JsonObject) toJsonTree("empty");
        assertThat(obj.entrySet()).isEmpty();
    }

    @ParameterizedTest
    @EmptySource
    void emptySourceInputIsExact(String arg) {
        assertThat(arg).isEmpty();
    }

    @Test
    void freshObjectAndEmptyArrayNotRecorders() {
        StrBuilder sb = new StrBuilder();
        assertEquals(0, sb.length());

        String[] a = new String[0];
        swap(a);
        assertEquals(0, a.length);
    }

    @Test
    void softAssertionsInputNotChecked() {
        softly.assertThat("String").isEqualTo("String");
        assertThat(softly.errorsCollected()).isEmpty();
    }

    @Test
    void classBackedValuesLoopIsNonEmpty() {
        for (ClassMode m : ClassMode.values()) {
            assertNotNull(m);
        }
        EqualityAssertions.assertEqualsAndHashCode("a", "a", "b");
    }

    @Test
    void realNoAssert() {
        compute(1);
    }

    @Test
    void realProductLoop() {
        for (String item : decodeItems()) {
            assertNotNull(item);
        }
    }

    @Test
    void realAbsenceNoControl() {
        List<String> found = new ArrayList<>();
        walkTree(found);
        assertTrue(found.isEmpty());
    }

    @Test
    void realVarMethodRefAbsenceNoControl() {
        var found = new ArrayList<String>();
        decodeItems().stream().forEach(found::add);
        assertTrue(found.isEmpty());
    }
}
