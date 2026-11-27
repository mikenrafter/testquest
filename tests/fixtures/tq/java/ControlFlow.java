import org.junit.Test;

class ControlFlow {
    @Test(expected = IllegalArgumentException.class)
    public void checksException() { parse(invalid()); }

    @Test public void checksExists() { assertThat(file()).exists(); }
    @Test public void transformingIsNotACheck() { assertThat(entries()).extracting(Entry::name); }
    @Test public void checksAfterTransform() {
        assertThat(entries()).extracting(Entry::name).contains("expected");
    }

    @Test public void nonemptyLoop() {
        for (int i = 0; i < 1; i++) assertEquals(i, read());
    }

    @Test public void emptyLoop() {
        for (int i = 1; i < 1; i++) assertEquals(i, read());
    }

    @Test public void throwInsideUnknownCallback() {
        register(() -> { throw new IllegalStateException(); });
        assertTrue(ready());
    }

    @Test public void transformedSubjectDiffersFromReceiver() {
        Object source = read();
        assertThat(source).extracting(Entry::name).isEqualTo(source);
    }

    @Test public void checksFluentException() {
        assertThatExceptionOfType(IllegalStateException.class).isThrownBy(() -> work());
    }

    @Test public void ownsHelperFinding() { assertFixedValue(); }
    private void assertFixedValue() { assertTrue(true); }
}
