import kotlin.test.Test

enum class Locale { ENGLISH, FRENCH }
private const val GENERATIONS = 500

class FixedInputs {
    @Test fun enumValues() { Locale.values().forEach { assertTrue(ready(it)) } }
    @Test fun enumEntries() { for (locale in Locale.entries) assertTrue(ready(locale)) }
    @Test fun repeated() { repeat(GENERATIONS) { assertTrue(ready(it)) } }
    @Test fun generatedNames() { names().forEach { assertTrue(valid(it)) } }
    @Test fun flatCurve() { curve().forEach { assertTrue(valid(it)) } }

    private fun names(n: Int = 500, seed: Int = 328) =
        Random(seed).let { random -> List(n) { generate(random) } }

    private fun curve() = (0..23).map { multiplier(it) }

    @Test fun productOutput() {
        weights().values.forEach { assertTrue(valid(it)) }
    }
}
