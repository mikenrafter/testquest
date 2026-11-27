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
    test("whole user agents compared by membership") {
        val acceptable = setOf("Chromium Android", "Chromium Tablet")
        assertTrue(acceptable.contains("Chromium Android"))
        assertTrue(acceptable.contains("Chromium Tablet"))
        assertFalse(acceptable.contains("Safari iOS"))
    }

    test("generated command arguments") {
        val plan = planVideo()
        assertTrue(plan.ffmpegArgs.contains("tonemap="))
        assertTrue(plan.ffmpegArgs.contains("-colorspace bt709"))
        assertTrue(plan.ffmpegArgs.contains("-movflags +faststart"))
    }
})
