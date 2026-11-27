import io.kotest.core.spec.style.BehaviorSpec
import io.kotest.core.spec.style.FreeSpec
import io.kotest.core.spec.style.FunSpec
import io.kotest.core.spec.style.StringSpec

class Behavior : BehaviorSpec({
    given("subject") {
        When("interaction") {
            then("no check") { work() }
        }
    }
})

class Free : FreeSpec({
    "group" - { "no check" { work() } }
})

class Strings : StringSpec({
    "checked" { result() shouldBe 7 }
    "unchecked" { work() }
    "ignored".config(enabled = false) { work() }
})

class Fun : FunSpec() {
    init {
        test("configured").config(enabled = true) { work() }
        test("ignored").config(enabled = false) { work() }
    }

    @Test fun fixedRows() {
        val rows = listOf(7)
        rows.forEach { row -> assertEquals(row, result()) }
    }

    @Test fun mutableRows() {
        val rows = mutableListOf(7)
        rows.clear()
        rows.forEach { row -> assertEquals(row, result()) }
    }
}
