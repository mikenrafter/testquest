"""Descriptions, rework recipes, and examples for findings."""

from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class Recipe:
    rework: tuple[str, ...]
    gaming: str
    examples: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class Rule:
    message: str
    allowed_when: str
    recipe: Recipe | None = None
    command: str | None = None
    level: str = "error"


RULES = {
    "no-assert": Rule(
        "Test has no check that verifies the outcome under test.",
        "The stated check is successful execution, such as passing output through a strict parser.",
        Recipe(
            (
                "Assert the result or side effect promised by the test.",
                "Observe it through the production entry point and use the contract to set the expected outcome.",
                "Use an input for which a wrong result would fail the assertion.",
            ),
            "Reject `assert True`, checks on setup data, and helpers that never fail.",
            {
                "py": ("""
                    def test_parse_port():
                        parse_port("8080")
                    """, """
                    def test_parse_port():
                        assert parse_port("8080") == 8080
                    """),
                "rs": ("""
                    #[test]
                    fn parses_port() {
                        let _ = parse_port("8080");
                    }
                    """, """
                    #[test]
                    fn parses_port() {
                        assert_eq!(parse_port("8080"), 8080);
                    }
                    """),
                "ts": ("""
                    test("parses a port", () => {
                      parsePort("8080");
                    });
                    """, """
                    test("parses a port", () => {
                      expect(parsePort("8080")).toBe(8080);
                    });
                    """),
            },
        ),
        command="lint",
    ),
    "guarded-assert": Rule(
        "Every check is conditional and the test can skip them all.",
        "The branch is exercised by a named witness, or zero matching items is explicitly a valid case.",
        Recipe(
            (
                "Arrange an input that must reach the checked case.",
                "Assert that the case was reached, or compare the complete result to the expected result.",
                "Test the empty or alternate case separately if the contract permits it.",
            ),
            "Reject an unrelated assertion outside the guard, or an early return that skips the check.",
            {
                "py": ("""
                    def test_fetch_items():
                        for item in fetch_items("in stock"):
                            assert item > 0
                    """, """
                    def test_fetch_items():
                        assert fetch_items("in stock") == [3, 7]
                    """),
                "rs": ("""
                    #[test]
                    fn fetches_items() {
                        for item in fetch_items("in stock") {
                            assert!(item > 0);
                        }
                    }
                    """, """
                    #[test]
                    fn fetches_items() {
                        assert_eq!(fetch_items("in stock"), vec![3, 7]);
                    }
                    """),
                "ts": ("""
                    test("fetches items", () => {
                      for (const item of fetchItems("in stock")) {
                        expect(item).toBeGreaterThan(0);
                      }
                    });
                    """, """
                    test("fetches items", () => {
                      expect(fetchItems("in stock")).toEqual([3, 7]);
                    });
                    """),
            },
        ),
        command="lint",
    ),
    "self-compare": Rule(
        "Assertion compares the same expression on both sides.",
        "The stated property is reflexivity or repeatability, and both operands are independently evaluated.",
        Recipe(
            (
                "Keep the observed result on one side of the comparison.",
                "Put the outcome required by the contract on the other side.",
                "If the claim is repeatability, run the operation independently and pin a known result too.",
            ),
            "Reject a renamed alias, a copy of the observed value, or a comparison hidden inside a helper.",
            {
                "py": ("""
                    def test_parse_port():
                        port = parse_port("8080")
                        assert port == port
                    """, """
                    def test_parse_port():
                        port = parse_port("8080")
                        assert port == 8080
                    """),
                "rs": ("""
                    #[test]
                    fn parses_port() {
                        let port = parse_port("8080");
                        assert_eq!(port, port);
                    }
                    """, """
                    #[test]
                    fn parses_port() {
                        let port = parse_port("8080");
                        assert_eq!(port, 8080);
                    }
                    """),
                "ts": ("""
                    test("parses a port", () => {
                      const port = parsePort("8080");
                      expect(port).toBe(port);
                    });
                    """, """
                    test("parses a port", () => {
                      const port = parsePort("8080");
                      expect(port).toBe(8080);
                    });
                    """),
            },
        ),
        command="lint",
    ),
    "const-truth": Rule(
        "Assertion contains a predicate that is always true.",
        "A deliberately trivial assertion checks framework plumbing.",
        Recipe(
            (
                "Remove the condition that makes the assertion pass for every outcome.",
                "Check the actual result against a requirement that broken code can violate.",
            ),
            "Reject a constant hidden behind a variable or helper, or a guard that only runs the assertion when it would pass.",
            {
                "py": ("""
                    def test_parse_port():
                        assert parse_port("8080") == 8080 or True
                    """, """
                    def test_parse_port():
                        assert parse_port("8080") == 8080
                    """),
                "rs": ("""
                    #[test]
                    fn parses_port() {
                        assert!(parse_port("8080") == 8080 || true);
                    }
                    """, """
                    #[test]
                    fn parses_port() {
                        assert_eq!(parse_port("8080"), 8080);
                    }
                    """),
                "ts": ("""
                    test("parses a port", () => {
                      assert(parsePort("8080") === 8080 || true);
                    });
                    """, """
                    test("parses a port", () => {
                      assert(parsePort("8080") === 8080);
                    });
                    """),
            },
        ),
        command="lint",
    ),
    "absence-no-control": Rule(
        "No check shows that the recorder or detector can report a result.",
        "An independent observation establishes that the recorder can fire.",
        Recipe(
            (
                "Drive an input that must produce a result through the same recorder in the same test.",
                "Assert what the recorder captured.",
                "Clear the recorder, exercise the absence case, and assert that it stays empty.",
            ),
            "Reject checking a different recorder, filling the recorder by hand, or putting the positive control behind a guard.",
            {
                "py": ("""
                    def test_valid_port_reports_no_errors():
                        errors = []
                        validate_port("8080", errors)
                        assert errors == []
                    """, """
                    def test_valid_port_reports_no_errors():
                        errors = []
                        validate_port("not a port", errors)
                        assert errors == ["port: not a number"]
                        errors.clear()
                        validate_port("8080", errors)
                        assert errors == []
                    """),
                "rs": ("""
                    #[test]
                    fn valid_port_reports_no_errors() {
                        let mut errors = Vec::new();
                        validate_port("8080", &mut errors);
                        assert!(errors.is_empty());
                    }
                    """, """
                    #[test]
                    fn valid_port_reports_no_errors() {
                        let mut errors = Vec::new();
                        validate_port("not a port", &mut errors);
                        assert_eq!(errors, vec!["port: not a number"]);
                        errors.clear();
                        validate_port("8080", &mut errors);
                        assert!(errors.is_empty());
                    }
                    """),
                "ts": ("""
                    test("valid port reports no errors", () => {
                      const report = vi.fn();
                      validatePort("8080", report);
                      expect(report).not.toHaveBeenCalled();
                    });
                    """, """
                    test("valid port reports no errors", () => {
                      const report = vi.fn();
                      validatePort("not a port", report);
                      expect(report).toHaveBeenCalledWith("port: not a number");
                      report.mockClear();
                      validatePort("8080", report);
                      expect(report).not.toHaveBeenCalled();
                    });
                    """),
            },
        ),
        command="lint",
    ),
    "nondistinguishing-literal": Rule(
        "The fixture or assertion can let a wrong implementation pass.",
        "The test's WHY explains the symmetric or fixed input. Examples include equal weights, a zero-write case, or a test that isolates another parameter.",
        Recipe(
            (
                "Name a plausible mistake that gives the same answer on the current input.",
                "Choose inputs that separate the required behavior from that mistake.",
                "Assert the exact outcome from the contract. Keep a symmetric or neutral case if it covers a separate requirement.",
            ),
            "Reject hiding the same literal behind a name, changing only its spelling, or widening the expected range.",
            {
                "py": ("""
                    def test_reverse_order():
                        assert reverse("abba") == "abba"
                    """, """
                    def test_reverse_order():
                        assert reverse("abcd") == "dcba"
                    """),
                "rs": ("""
                    #[test]
                    fn reverses_order() {
                        assert_eq!(reverse(b"abba"), b"abba");
                    }
                    """, """
                    #[test]
                    fn reverses_order() {
                        assert_eq!(reverse(b"abcd"), b"dcba");
                    }
                    """),
                "ts": ("""
                    test("reverses order", () => {
                      expect(reverse("abba")).toBe("abba");
                    });
                    """, """
                    test("reverses order", () => {
                      expect(reverse("abcd")).toBe("dcba");
                    });
                    """),
            },
        ),
        command="lint",
        level="advisory",
    ),
    "self-derived-expected": Rule(
        "The expected value reuses production code from the module under test.",
        "The test checks repeatability, a before-and-after observation, or a declared relation between different inputs or entry points.",
        Recipe(
            (
                "Work out the expected value from the specification, a trusted fixture, or an independent calculation.",
                "Compare the observed result to that value.",
                "If the test checks an inverse or another relation, also pin a known result that shared code could get wrong.",
            ),
            "Reject moving the production calculation into a test helper or copying its formula verbatim.",
            {
                "py": ("""
                    import billing

                    def test_total_cents():
                        assert billing.total_cents(1000, 8) == billing.apply_tax(1000, 8)
                    """, """
                    import billing

                    def test_total_cents():
                        # Eight percent of 1000 cents is 80 cents.
                        assert billing.total_cents(1000, 8) == 1080
                    """),
                "rs": ("""
                    #[test]
                    fn computes_total_cents() {
                        assert_eq!(billing::total_cents(1000, 8), billing::apply_tax(1000, 8));
                    }
                    """, """
                    #[test]
                    fn computes_total_cents() {
                        // Eight percent of 1000 cents is 80 cents.
                        assert_eq!(billing::total_cents(1000, 8), 1080);
                    }
                    """),
                "ts": ("""
                    import * as billing from "./billing";

                    test("computes total cents", () => {
                      expect(billing.totalCents(1000, 8)).toBe(billing.applyTax(1000, 8));
                    });
                    """, """
                    import * as billing from "./billing";

                    test("computes total cents", () => {
                      // Eight percent of 1000 cents is 80 cents.
                      expect(billing.totalCents(1000, 8)).toBe(1080);
                    });
                    """),
            },
        ),
        command="lint",
        level="advisory",
    ),
    "missing-witness": Rule(
        "A conditional test has no evidence that its condition is reached.",
        "Name another scanned test that reaches the condition, or measure coverage in this test.",
        Recipe(
            (
                "Drive the production path with an input that must reach the condition.",
                "Assert the evidence that it reached the condition and check the required outcome.",
                "For generated inputs, count qualifying cases and fail when none reach the condition.",
            ),
            "Reject an unrelated reaches note, a counter incremented before the condition, or fewer qualifying inputs.",
            {
                "py": ("""
                    def test_retry_sends_again():
                        attempts = send_with_retry()
                        if len(attempts) > 1:
                            assert attempts[-1] == "sent"
                    """, """
                    def test_retry_sends_again():
                        attempts = send_with_retry(fail_first=True)
                        assert attempts == ["timeout", "sent"]
                    """),
                "rs": ("""
                    #[test]
                    fn retry_sends_again() {
                        let attempts = send_with_retry(&mut transport());
                        if attempts.len() > 1 {
                            assert_eq!(attempts.last(), Some(&"sent"));
                        }
                    }
                    """, """
                    #[test]
                    fn retry_sends_again() {
                        let attempts = send_with_retry(&mut transport_that_times_out_once());
                        assert_eq!(attempts, vec!["timeout", "sent"]);
                    }
                    """),
                "ts": ("""
                    test("retry sends again", () => {
                      const attempts = sendWithRetry(transport());
                      if (attempts.length > 1) {
                        expect(attempts.at(-1)).toBe("sent");
                      }
                    });
                    """, """
                    test("retry sends again", () => {
                      const attempts = sendWithRetry(transportThatTimesOutOnce());
                      expect(attempts).toEqual(["timeout", "sent"]);
                    });
                    """),
            },
        ),
        command="witness",
    ),
    "invalid-suppression": Rule(
        "A lint suppression names an unknown rule or lacks a meaningful reason.",
        "Name a supported lint rule and explain the exception in at least three words.",
    ),
    "too-deep": Rule(
        "The scan skipped a source file after exceeding parser limits.",
        "This advisory diagnostic does not require a suppression.",
        level="advisory",
    ),
    "parse-error": Rule(
        "A source file could not be parsed.",
        "Fix the source or its encoding before scanning. This diagnostic cannot be suppressed.",
    ),
}

LANGUAGES = {"py": "Python", "rs": "Rust", "ts": "TypeScript"}


def fix_for(name):
    rule = RULES[name]
    step = rule.recipe.rework[0] if rule.recipe else rule.allowed_when
    return f"{step} See `tq rules {name}`."


def summary(name):
    rule = RULES[name]
    return f"{name}  {rule.message}\n  level: {rule.level}\n  allowed-when: {rule.allowed_when}"


def describe(name=None, language=None):
    if name is None:
        return "\n".join(map(summary, sorted(RULES)))
    recipe = RULES[name].recipe
    if recipe is None:
        return summary(name)
    steps = "\n".join(f"  {index}. {step}" for index, step in enumerate(recipe.rework, 1))
    sections = [summary(name), f"Rework\n{steps}", f"Review\n  {recipe.gaming}"]
    for lang in (language,) if language else LANGUAGES:
        bad, good = recipe.examples[lang]
        sections.append(f"{LANGUAGES[lang]}\nBad example\n{dedent(bad).strip()}\n\n"
                        f"Reworked example\n{dedent(good).strip()}")
    return "\n\n".join(sections)
