import org.junit.Test;
import org.junit.Ignore;
import org.junit.jupiter.params.ParameterizedTest;
import androidx.test.ext.junit.runners.AndroidJUnit4;

@RunWith(AndroidJUnit4.class)
class Instrumented {
    @Test public void hollow() { work(); } // hit
    @Ignore @Test public void skipped() { work(); }
    @ParameterizedTest void check() {
        Object actual = result();
        assertEquals("message", actual, actual); // self
        assertTrue("message", true); // constant
        assertThat(actual).as("context").isEqualTo(actual); // self
        assertThat(actual, equalTo(actual)); // self
        assertThat(actual).hasSize(2);
        assertThrows(IllegalStateException.class, () -> work());
    }
    @Test void guarded() { if (enabled()) return; assertTrue(enabled()); } // hit
    @Test void definite() {
        for (int count : new int[]{4}) { assertEquals(count, read()); }
    }
    @Test void branch() {
        if (enabled()) { assertTrue(enabled()); } else { assertFalse(enabled()); }
    }
    @Test void deferred() { Runnable pending = () -> assertTrue(enabled()); } // hit
    @Test void javaAssertion() { assert ready(); }
}

@Ignore
class Skipped {
    @Test void onIgnoredClass() { work(); }
}
