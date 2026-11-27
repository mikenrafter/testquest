// tq: allow no-assert registration scheduled by the runner
@org.junit.jupiter.api.Test fun allowed() { work() }

@kotlin.test.Test fun withNotes() {
    /**
     * reaches: enabled branch
     * catches: the wrong error type
     */
    assertTrue(ready())
}

@kotlin.test.Test fun guardedWithWitness() {
    // reaches: withNotes
    if (enabled()) assertTrue(ready())
}

@kotlin.test.Test fun guardedAlone() {
    // reaches: unknown test
    if (enabled()) assertTrue(ready())
}
