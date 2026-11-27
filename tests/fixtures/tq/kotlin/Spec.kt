import io.kotest.core.spec.style.FunSpec
import kotlin.test.Test as UnitTest
import org.junit.jupiter.api.RepeatedTest
import org.junit.jupiter.api.Disabled

class Spec : FunSpec({
    context("nested") {
        test("hollow") { work() } // hit
        test("infix") { actual() shouldBe 4 }
        test("exception") { shouldThrow<IllegalStateException> { work() } }
        test("guard") { if (enabled()) actual() shouldBe 4 } // hit
        test("both") { if (enabled()) { actual() shouldBe 4 } else { actual() shouldBe 5 } }
    }
    xtest("disabled") { work() }
    xcontext("disabled group") {
        test("disabled child") { work() }
    }
})

class Annotated {
    @UnitTest fun kotlinTest() { work() } // hit
    @RepeatedTest(3) fun repeat() {
        val same = actual()
        same shouldBe same // self
        assertTrue(true) // constant
        assertEquals(message = "message", actual = same, expected = same) // self
    }
    @Disabled @UnitTest fun ignored() { work() }
    @UnitTest fun literalRange() {
        for (entry in 2..2) { assertEquals(entry, actual()) }
    }
    @UnitTest fun emptyRange() { for (entry in 3 until 3) { assertEquals(entry, actual()) } } // hit
    @UnitTest fun lazyLambda() { val check = { assertTrue(enabled()) } } // hit
    @UnitTest fun expressionBody() = assertEquals(7, actual())
    @UnitTest fun localHelper() { assertResult() }
    @UnitTest fun localHollow() { assertNothing() } // hit
    private fun assertResult() { assertTrue(enabled()) }
    private fun assertNothing() { work() }
}
