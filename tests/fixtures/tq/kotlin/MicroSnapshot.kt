class MicroSnapshot : FunSpec({
    test("fragments") {
        response.text shouldContain "alpha"
        assertThat(response.text).doesNotContain("beta")
        assertTrue(response.text.contains("""gamma"""))
        response.text shouldContain "$dynamic"
    }

    test("whole") {
        out shouldContain "alpha"
        out shouldContain "beta"
        out shouldContain "gamma"
        out shouldBe expected
    }
})
