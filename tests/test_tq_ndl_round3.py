"""CLI regressions for round 3 of the nondistinguishing-literal advisory."""

from test_tq import TqCase


class NondistinguishingLiteralsRound3(TqCase):
    def test_python_checked_symmetry_witness(self):
        # WHY: an unused call or a changed fixture can leave the duplicate roles untested.
        # catches: accepting an unchecked witness or letting unrelated fixture changes mask the pair.
        self.check_lint("""
            def test_period():
                assert period(textured, 6, 2, 2) == 4
                assert period(flat, 6, 2, 1) is None
                period(textured, 6, 3, 4)
                assert difference(6, 6) == 0
                assert difference(6, 2) == 4
            """, "nondistinguishing-literal", ("period(textured, 6, 2, 2)",))

    def test_rust_checked_symmetry_witness(self):
        # WHY: only a checked call that distinguishes the duplicate slots can suppress a report.
        # catches: accepting a changed fixture or an unchecked asymmetric call.
        self.check_lint("""
            #[test]
            fn test_period() {
                assert_eq!(period(&image(textured), 6, 2, 2), Some(4));
                assert_eq!(period(&image(flat), 6, 2, 1), None);
                period(&image(textured), 6, 3, 4);
                assert_eq!(difference(6, 6), 0);
                assert_eq!(difference(6, 2), 4);
            }
            """, "nondistinguishing-literal", ("period(&image(textured), 6, 2, 2)",), "checks.rs")

    def test_python_named_coverage(self):
        # WHY: an every-input claim must vary flags, and an empty case must supply an empty input.
        # catches: requiring numeric identities, or treating names with no relevant claim as coverage claims.
        self.check_lint("""
            def testEveryInputChangesTheKey():
                original = key("short", False)
                assert original != key("long", False)

            def test_every_input_changes_the_hash():
                assert digest("short", False) != digest("long", True)

            def test_supports_empty_body():
                assert message("content") == expected

            def test_accepts_empty_body():
                assert render("") == expected
                assert render("content") == other

            def test_takes_noisy_input():
                assert handle("content") == expected
            """, "nondistinguishing-literal", ('original = key', 'message("content")'))

    def test_rust_named_coverage(self):
        # WHY: the empty body does not cover a missing command or variation in an input fixture.
        # catches: using an empty argument to cover unrelated named cases or excluding string and bool literals.
        self.check_lint("""
            fn message(command: &str, instructions: &str) -> String { todo!() }

            #[test]
            fn supports_commandless_requests_and_empty_bodies() {
                assert_eq!(message("/compact", "instructions"), combined);
                assert_eq!(message("/compact", ""), command);
            }

            #[test]
            fn handles_commandless_requests() {
                assert_eq!(message("", "instructions"), instructions);
            }

            #[test]
            fn every_argument_changes_the_key() {
                let base = spec(100, 50);
                let first = key(base, false);
                assert_ne!(first, key(spec(101, 50), false));
            }

            #[test]
            fn all_inputs_vary() {
                assert_ne!(digest(3, false), digest(5, true));
            }
            """, "nondistinguishing-literal", (
                'message("/compact", "instructions")', 'let base = spec', 'let first = key',
            ), "checks.rs")

    def test_python_checked_value_and_zero_origin(self):
        # WHY: setup builders live outside the comparison while the fixture times
        # can still expose a zero-origin subtraction.
        # catches: flagging nested setup calls or missing output times copied from zero-based input.
        self.check_lint("""
            def test_inputs():
                unused = disk("unused", "0", "0")
                assert read(meta(0, 0, 1, 1)) == expected
                assert charge(box(7, 0)) == low
                assert charge(box(8, 0)) == high
                assert offset(7, 0) == low
                assert offset(8, 0) == high

            def test_waits():
                marks = [Mark(0, "assistant"), Mark(10, "human"), Mark(30, "human")]
                assert waits(marks) == [Gap(10, 10), Gap(30, 30)]

            def test_shifted_waits():
                marks = [Mark(3, "assistant"), Mark(10, "human"), Mark(30, "human")]
                assert waits(marks) == [Gap(10, 7), Gap(30, 27)]
            """, "nondistinguishing-literal", ("offset(7", "waits(marks) == [Gap(10, 10)"))

    def test_rust_checked_value_and_boundary(self):
        # WHY: a small repeated fixture is setup, but a fixed coordinate on the
        # checked call's input can hide how the check handles clipping.
        # catches: counting nested setup as checked or ignoring symmetric float coordinates.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                let unused = disk("unused", "0", "0");
                assert_eq!(read(meta(0, 0, 1, 1)), expected);
                assert_eq!(charge(shade(7, 0)), low);
                assert_eq!(charge(shade(8, 0)), high);
                assert_eq!(offset(7, 0), low);
                assert_eq!(offset(8, 0), high);
            }

            #[test]
            fn clips_lines() {
                assert_eq!(hidden_share(item(0.0, 100.0, 150.0, 150.0), LOOK, VIEW), 1.0);
                assert_eq!(hidden_share(item(-100.0, 100.0, 50.0, 50.0), LOOK, VIEW), 0.0);
            }
            """, "nondistinguishing-literal", (
                "offset(7", "item(0.0",
            ), "checks.rs")

    def test_python_loose_bound(self):
        # WHY: an exact equality anywhere in the test pins the checked function.
        # catches: skipping unittest inequalities or flagging a function also checked for equality.
        self.check_lint("""
            def test_scores(self):
                score = compute(5000, 6000, 10000)
                self.assertGreater(score, 0.9)
                assert exact(1000, 2000) < 0.9
                assert exact(3000, 4000) == 0.5
                assert dynamic(user_input) > 0.9
            """, "nondistinguishing-literal", ("score = compute",))

    def test_rust_loose_bound(self):
        # WHY: an exact check rules out a bound-only oracle, even on an alias.
        # catches: skipping inequality macros or overlooking equality on a bound result.
        self.check_lint("""
            #[test]
            fn test_scores() {
                let score = compute(5000, 6000, 10000);
                prop_assert!(score >= 0.9);
                let exact = area(1000, 2000);
                assert!(exact < 0.9);
                assert_eq!(exact, 0.5);
                assert!(dynamic(user_input) > 0.9);
            }
            """, "nondistinguishing-literal", ("score = compute",), "checks.rs")
