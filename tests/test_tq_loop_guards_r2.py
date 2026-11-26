"""CLI checks for non-empty iterables in the guarded-assert rule."""

from test_tq import TqCase


class LoopGuardsRoundTwo(TqCase):
    def test_python_bindings_and_wrappers(self):
        # WHY: a literal survives iteration adapters only while its binding is valid.
        # catches: ignoring wrappers, trusting a conditional binding, or missing reassignment.
        self.check_lint("""
            def test_bound():
                cases = {"a": 1}
                for label, value in cases.items():
                    assert valid(label, value)

            def test_wrapped():
                for index, pair in enumerate(zip(sorted([2, 1]), (3, 4))):
                    assert valid(index, pair)

            def test_all():
                cases = {1, 2}
                assert all(valid(case) for case in cases)

            def test_zip_empty():
                for pair in zip([1], []):
                    assert valid(pair)

            def test_reassigned():
                cases = [1]
                cases = load_cases()
                for case in cases:
                    assert valid(case)

            def test_conditional_binding():
                cases = []
                if enabled():
                    cases = [1]
                for case in cases:
                    assert valid(case)

            def test_cleared():
                cases = [1]
                cases.clear()
                for case in cases:
                    assert valid(case)
            """, "guarded-assert", ("def test_zip_empty", "def test_reassigned",
                                   "def test_conditional_binding", "def test_cleared"))

    def test_python_constants_and_helpers(self):
        # WHY: only a visible, non-empty table or a single literal return proves iteration.
        # catches: trusting upper-case names, collisions across classes, or arbitrary calls.
        self.check_lint("""
            MODULE_ROWS = (1, 2)
            EMPTY_ROWS = ()

            def fixed_rows():
                return [1]

            def sorted(rows):
                return load_rows()

            def loaded_rows():
                if enabled():
                    return [1]
                return load_rows()

            class TestTables:
                ROWS = [3]

                def test_tables(self):
                    for row in MODULE_ROWS:
                        for other in self.ROWS:
                            assert valid(row, other)

                def test_helper(self):
                    for row in fixed_rows():
                        assert valid(row)

                def test_member_call(self):
                    for row in factory().fixed_rows():
                        assert valid(row)

                def test_shadowed_adapter(self):
                    for row in sorted([1]):
                        assert valid(row)

                def test_loaded(self):
                    for row in loaded_rows():
                        assert valid(row)

                def test_empty(self):
                    for row in EMPTY_ROWS:
                        assert valid(row)

                def test_shadow(self):
                    MODULE_ROWS = load_rows()
                    for row in MODULE_ROWS:
                        assert valid(row)

                def test_fixture(self):
                    for row in fixture_path.rglob("*.json"):
                        assert valid(row)

            class TestOther:
                ROWS = []

                def test_other(self):
                    for row in self.ROWS:
                        assert valid(row)
            """, "guarded-assert", ("def test_member_call", "def test_shadowed_adapter",
                                   "def test_loaded", "def test_empty", "def test_shadow(self)",
                                   "def test_fixture", "def test_other"))

    def test_python_nested_loops_and_size_checks(self):
        # WHY: a size check runs independently only if its enclosing path runs.
        # catches: losing an inner guard or counting a check inside a skipped loop.
        self.check_lint("""
            def test_count_before(self):
                tables = [load_rows()]
                for rows in tables:
                    self.assertEqual(len(rows), 2)
                    for row in rows:
                        assert valid(row)

            def test_count_after():
                tables = [load_rows()]
                for rows in tables:
                    for row in rows:
                        assert valid(row)
                    assert rows

            def test_inner_output():
                for case in [1]:
                    for row in load_rows(case):
                        assert valid(row)

            def test_count_inside_output():
                for rows in load_tables():
                    assert len(rows) == 2
                    for row in rows:
                        assert valid(row)
            """, "guarded-assert", ("def test_inner_output", "def test_count_inside_output"))

    def test_rust_bindings_and_adapters(self):
        # WHY: adapters preserve a literal's entries only while the underlying binding does.
        # catches: ignoring typed slices, trusting a zero repeat, or missing a reassignment.
        self.check_lint("""
            #[test]
            fn test_bound() {
                let pairs: &[(i32, i32)] = &[(1, 2)];
                for (i, pair) in pairs.iter().enumerate() { assert!(valid(i, pair)); }
            }

            #[test]
            fn test_wrapped() {
                for value in [0; 2].into_iter() { assert!(valid(value)); }
            }

            #[test]
            fn test_empty() {
                let cases = vec![0; 0];
                for case in cases.into_iter() { assert!(valid(case)); }
            }

            #[test]
            fn test_reassigned() {
                let mut cases = vec![1];
                cases = load_rows();
                for case in cases { assert!(valid(case)); }
            }

            #[test]
            fn test_cleared() {
                let mut cases = vec![1];
                cases.clear();
                for case in cases { assert!(valid(case)); }
            }

            #[test]
            fn test_shadowed() {
                let cases = vec![1];
                let cases = load_rows();
                for case in cases { assert!(valid(case)); }
            }
            """, "guarded-assert", ("fn test_empty", "fn test_reassigned",
                                   "fn test_cleared", "fn test_shadowed"), "loops.rs")

    def test_rust_constants_and_helpers(self):
        # WHY: constants and literal helpers resolve to their declaration in the current scope.
        # catches: trusting a table name from a different type or a helper with an early return.
        self.check_lint("""
            const COUNT: usize = 1;
            static TABLE: [i32; COUNT] = [1; COUNT];
            struct On;
            struct Off;
            impl On { const ALL: &'static [i32] = &[2]; }
            impl Off { const ALL: [i32; 0] = []; }
            fn fixed_rows() -> Vec<i32> { return vec![1]; }
            fn conditional_rows() -> Vec<i32> {
                if enabled() { return vec![]; }
                vec![1]
            }

            mod tests {
                use super::{TABLE, On, Off, fixed_rows, conditional_rows};

                #[test]
                fn test_tables() {
                    for first in TABLE.iter() {
                        for second in On::ALL { assert!(valid(first, second)); }
                    }
                }

                #[test]
                fn test_helper() {
                    for row in fixed_rows() { assert!(valid(row)); }
                }

                #[test]
                fn test_empty() {
                    for row in Off::ALL { assert!(valid(row)); }
                }

                #[test]
                fn test_conditional_helper() {
                    for row in conditional_rows() { assert!(valid(row)); }
                }

                #[test]
                fn test_fixture() {
                    for entry in std::fs::read_dir("fixtures").unwrap() { assert!(valid(entry)); }
                }
            }
            """, "guarded-assert", ("fn test_empty", "fn test_conditional_helper",
                                   "fn test_fixture"), "loops.rs")

    def test_rust_nested_loops_and_size_checks(self):
        # WHY: an inner count check must have a path that executes independently of the inner loop.
        # catches: losing an inner guard or counting a check inside an output-dependent outer loop.
        self.check_lint("""
            #[test]
            fn test_count_before() {
                let tables = [load_rows()];
                for rows in tables {
                    assert!(!rows.is_empty());
                    for row in rows { assert!(valid(row)); }
                }
            }

            #[test]
            fn test_count_after() {
                let tables = [load_rows()];
                for rows in tables {
                    for row in &rows { assert!(valid(row)); }
                    assert_eq!(rows.len(), 2);
                }
            }

            #[test]
            fn test_inner_output() {
                for case in [1] {
                    for row in load_rows(case) { assert!(valid(row)); }
                }
            }

            #[test]
            fn test_count_inside_output() {
                for rows in load_tables() {
                    assert_eq!(rows.len(), 2);
                    for row in rows { assert!(valid(row)); }
                }
            }
            """, "guarded-assert", ("fn test_inner_output", "fn test_count_inside_output"), "loops.rs")
