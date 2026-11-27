class MicroSnapshot {
    @Test
    void fragments() {
        assertThat(response.text()).contains("alpha");
        assertThat(response.text()).doesNotContain("beta");
        assertTrue("missing gamma", response.text().contains("gamma"));
    }

    @Test
    void whole() {
        assertThat(out).contains("alpha", "beta");
        assertThat(out).contains("end");
        assertThat(out).doesNotContain("gamma");
        assertThat(out).isEqualTo(expected);
    }

    @Test
    void chained() {
        assertThat(response.text()).contains("alpha")
            .doesNotContain("beta")
            .contains("gamma");
    }
}
