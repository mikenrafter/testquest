"""Exercise the optional parser through lint and changed-test inventory."""
import importlib.util
import json
import unittest

from test_tq import TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class AstGrepLint(TqCase):
    def lint_rows(self, *paths):
        rows = self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1))
        return [(row["line"], row["rule"]) for row in json.loads(rows)]

    def test_rust_operand_reparse_and_template_locations(self):
        # WHY: the commas in turbofish and closures belong to the first operand.
        # catches: splitting macro arguments at a generic parameter or closure comma
        path = self.write("checks.rs", '''
            #[cfg(test)]
            mod tests {
                use proptest::prelude::*;
                proptest! {
                    #[test]
                    fn two_strategies(a in 0..4, b in 1..5) {
                        let _emoji = "🧪";
                        prop_assert_eq!(project::<A, B>(a, |x, y| x + y), project::<A, B>(a, |x, y| x + y));
                        prop_assert_eq!(project::<A, B>(a, |x, y| x + y), project::<A, B>(b, |x, y| x + y));
                    }
                }
            }
        ''')
        self.assertEqual(self.lint_rows(path.name), [(8, "self-compare")])

    def test_common_rules_and_suppression(self):
        # WHY: an empty iterator and an independently demonstrated recorder differ.
        # catches: treating an unknown iterator as nonempty or sharing controls by verb alone
        python = self.write("test_cases.py", '''
            @mark.parametrize("seed", (1, 2))
            def test_no_assertion(seed):
                consume(seed)

            def test_may_skip():
                for x in fetch():
                    assert valid(x)

            def test_known_nonempty():
                for x in range(2, -1, -1):
                    assert valid(x)

            def test_constant():
                assert ready() or True
                assert ready() or True  # tq: allow const-truth tests the exception parser

            def test_uncontrolled():
                errors = []
                accept(errors.append)
                unrelated = []
                report(unrelated.append)
                assert unrelated
                assert not errors

            def test_controlled():
                errors = []
                accept(errors.append)
                positive = []
                accept(positive.append)
                assert positive
                assert not errors
        ''')
        rust = self.write("checks.rs", '''
            #[rstest]
            #[case(1)]
            fn no_assertion(#[case] seed: i32) {
                consume(seed);
            }

            #[test]
            fn may_skip() {
                for x in fetch() {
                    assert!(valid(x));
                }
            }

            #[test]
            fn known_nonempty() {
                for x in [1, 2].into_iter().map(transform) {
                    assert!(valid(x));
                }
            }

            #[test]
            fn constant() {
                assert!(ready() || true);
                assert!(ready() || true); // tq: allow const-truth tests the exception parser
            }
        ''')
        self.assertEqual(self.lint_rows(python.name), [
            (2, "no-assert"), (5, "guarded-assert"), (14, "const-truth"), (23, "absence-no-control"),
        ])
        self.assertEqual(self.lint_rows(rust.name), [
            (3, "no-assert"), (8, "guarded-assert"), (23, "const-truth"),
        ])

    def test_inherited_checks_and_loop_exits(self):
        # WHY: a base-class helper verifies the result, but break can bypass a loop check.
        # catches: ignoring inherited checks or proving execution from a nonempty input alone
        python = self.write("test_inherited.py", """
            class Case:
                def verify(self, received):
                    self.assertEqual(received, 42)

            class Test(Case):
                def test_reads(self):
                    self.verify(read())
        """)
        rust = self.write("checks.rs", """
            #[test]
            fn skip_after_break() {
                for item in [1] {
                    if skip(item) { break; }
                    assert!(valid(item));
                }
            }
        """)
        self.assertEqual(self.lint_rows(python.name), [])
        self.assertEqual(self.lint_rows(rust.name), [(2, "guarded-assert")])

    def test_fixture_loading_is_not_an_outcome_check(self):
        # WHY: an unreadable fixture can abort setup without checking the outcome.
        # catches: counting a loader's fallback panic as evidence for its callers
        path = self.write("checks.rs", """
            fn fixture() -> String {
                std::fs::read_to_string("input").unwrap_or_else(|_| panic!("missing input"))
            }

            #[test]
            fn output_is_only_printed() {
                println!("{}", layout_of(fixture()));
            }

            #[test]
            fn all_reported_rows_are_valid() {
                for row in parse(fixture()) {
                    assert!(valid(row));
                }
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(6, "no-assert"), (11, "guarded-assert")])

    def test_since_inventories_the_deletion_side(self):
        # WHY: deleting the only check leaves the function line outside the new hunk.
        # catches: selecting only tests that intersect new lines
        self.git("init", "-q")
        filename = "checks.rs"
        self.write(filename, """
            mod scoped {
                #[test]
                fn former_check() {
                    let state = prepare();
                    assert!(state.is_valid());
                }
                #[test]
                fn other() { prepare(); }
            }
        """)
        self.commit("Fixture with a check")
        self.write(filename, """
            mod scoped {
                #[test]
                fn former_check() {
                    let state = prepare();
                }
                #[test]
                fn other() { prepare(); }
            }
        """)
        self.assertEqual(self.lint_rows("--since", "HEAD"), [(3, "no-assert")])
