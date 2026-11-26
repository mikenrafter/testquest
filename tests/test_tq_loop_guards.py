"""CLI checks for assertions that can be skipped by zero loop iterations."""

from test_tq import TqCase


class LoopGuards(TqCase):
    def test_python_loops_that_can_be_empty(self):
        # WHY: any of these bodies can execute zero times.
        # catches: recognizing only if/match or exempting every literal iterable.
        self.check_lint("""
            def test_for():
                for value in []:
                    assert valid(value)

            async def test_async_for():
                async for row in rows:
                    assert row.valid

            def test_while():
                while ready():
                    assert read() == 3

            def test_empty_range():
                for value in range(4, 4):
                    assert valid(value)

            def test_wrong_step():
                for value in range(4, 1, 1):
                    assert valid(value)

            def test_only_unpacking():
                for value in [*rows]:
                    assert valid(value)

            def test_nested():
                for row in rows:
                    for value in (1,):
                        assert row.valid(value)
            """, "guarded-assert", ("def test_for", "def test_async_for", "def test_while",
                                   "def test_empty_range", "def test_wrong_step",
                                   "def test_only_unpacking", "def test_nested"))

    def test_python_guaranteed_iteration_or_outside_check(self):
        # WHY: a non-empty literal enters the body and an outside check runs independently.
        # catches: guarding all loops or ignoring an assertion before or after the loop.
        self.check_lint("""
            def test_list():
                for value in [1]:
                    assert valid(value)

            def test_tuple():
                for value in (1,):
                    assert valid(value)

            def test_set():
                for value in {1}:
                    assert valid(value)

            def test_dict():
                for key in {"row": rows}:
                    assert valid(key)

            def test_range():
                for value in range(3):
                    assert valid(value)

            def test_descending_range():
                for value in range(1, -2, -1):
                    assert valid(value)

            def test_unconditional_while():
                while True:
                    assert valid(read())
                    break

            def test_before():
                assert rows
                for row in rows:
                    assert row.valid

            def test_after(self):
                for row in rows:
                    assert row.valid
                self.assertTrue(rows)

            def test_count():
                for row in rows:
                    assert row.valid
                assert len(rows) == 3

            def test_count_method(self):
                self.assertGreater(len(rows), 0)
                while ready():
                    assert read() == 3

            def test_other_assertion():
                for row in rows:
                    assert row.valid
                assert finished()

            def test_loop_without_outside_check():
                for row in rows:
                    assert row.valid
            """, "guarded-assert", ("def test_loop_without_outside_check",))

    def test_python_all_comprehensions(self):
        # WHY: all() accepts empty input even when its predicate never runs.
        # catches: counting all() as unconditional or ignoring comprehension filters.
        self.check_lint("""
            def test_generator():
                assert all(valid(row) for row in rows)

            def test_list():
                assert all([row.valid for row in rows])

            def test_predicate_helper():
                assert all(check_row(row) for row in rows)

            def test_filtered():
                assert all(valid(value) for value in [3] if keep(value))

            def test_nested():
                assert all(valid(value) for row in [1] for value in values(row))

            def test_nonempty():
                assert all(valid(value) for value in range(3))

            def test_nonempty_list():
                assert all([valid(value) for value in [3]])

            def test_length_before():
                assert len(rows) == 3
                assert all(row.valid for row in rows)

            def test_length_after(self):
                self.assertTrue(all(row.valid for row in rows))
                self.assertGreater(len(rows), 0)

            def test_method_only(self):
                self.assertTrue(all(row.valid for row in rows))

            def test_inside_loop():
                for rows in [first_batch]:
                    assert all(row.valid for row in rows)
            """, "guarded-assert", ("def test_generator", "def test_list", "def test_predicate_helper",
                                   "def test_filtered", "def test_nested", "def test_method_only",
                                   "def test_inside_loop"))

    def test_rust_loops_that_can_be_empty(self):
        # WHY: each loop may skip its body even though it contains a check.
        # catches: missing while let or treating empty, repeated, or descending literals as non-empty.
        self.check_lint("""
            #[test]
            fn test_for() {
                for row in rows() { assert!(row.valid()); }
            }

            #[test]
            fn test_while() {
                while ready() { assert_eq!(read(), 3); }
            }

            #[test]
            fn test_while_let() {
                while let Some(row) = next_row() { assert!(row.valid()); }
            }

            #[test]
            fn test_empty_array() {
                for value in [] { assert!(valid_i32(value)); }
                for value in [0i32; 0] { assert!(valid(value)); }
            }

            #[test]
            fn test_empty_vec() {
                for value in vec![] { assert!(valid_i32(value)); }
            }

            #[test]
            fn test_repeat_zero() {
                for value in vec![0; 0] { assert!(valid(value)); }
            }

            #[test]
            fn test_empty_range() {
                for value in 3..3 { assert!(valid(value)); }
            }

            #[test]
            fn test_descending_range() {
                for value in 3..=1 { assert!(valid(value)); }
            }

            #[test]
            fn test_braced_macro() {
                for value in rows!{} { assert!(valid(value)); }
            }

            #[test]
            fn test_match_iterable() {
                for row in match mode() { Mode::On => rows(), Mode::Off => vec![] } {
                    assert!(row.valid());
                }
            }

            #[test]
            fn test_if_iterable() {
                for row in if enabled() { rows() } else if fallback() { defaults() } else { vec![] } {
                    assert!(row.valid());
                }
            }

            #[test]
            fn test_nested() {
                for row in rows() {
                    loop { assert!(row.valid()); break; }
                }
            }
            """, "guarded-assert", ("fn test_for", "fn test_while()", "fn test_while_let",
                                   "fn test_empty_array", "fn test_empty_vec", "fn test_repeat_zero",
                                   "fn test_empty_range", "fn test_descending_range",
                                   "fn test_braced_macro", "fn test_match_iterable",
                                   "fn test_if_iterable", "fn test_nested"), "loops.rs")

    def test_rust_guaranteed_iteration_or_outside_check(self):
        # WHY: these tests enter the body or make an independent assertion.
        # catches: treating loop, non-empty literals, or an inclusive singleton as skippable.
        self.check_lint("""
            #[test]
            fn test_array() {
                for value in &[read(), 3] { assert!(valid(*value)); }
            }

            #[test]
            fn test_vec() {
                for value in vec![3] { assert!(valid(value)); }
            }

            #[test]
            fn test_repeat() {
                for value in [0; 1] { assert!(valid(value)); }
            }

            #[test]
            fn test_braced_vec() {
                for value in vec!{3; 1} { assert!(valid(value)); }
            }

            #[test]
            fn test_range() {
                for value in 0..3 { assert!(valid(value)); }
            }

            #[test]
            fn test_inclusive_range() {
                for value in (-3..=-3) { assert!(valid(value)); }
            }

            #[test]
            fn test_loop() {
                loop { assert_eq!(read(), 3); break; }
            }

            #[test]
            fn test_before() {
                let rows = read_rows();
                assert!(!rows.is_empty());
                for row in rows { assert!(row.valid()); }
            }

            #[test]
            fn test_after() {
                let rows = read_rows();
                for row in &rows { assert!(row.valid()); }
                assert_eq!(rows.len(), 3);
            }

            #[test]
            fn test_other_assertion() {
                for row in rows() { assert!(row.valid()); }
                assert!(finished());
            }

            #[test]
            fn test_loop_without_outside_check() {
                for row in rows() { assert!(row.valid()); }
            }
            """, "guarded-assert", ("fn test_loop_without_outside_check",), "loops.rs")
