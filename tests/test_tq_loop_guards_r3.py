"""CLI regressions for guarded assertions with statically reachable checks."""

from test_tq import TqCase


class LoopGuardsRoundThree(TqCase):
    def test_python_imports(self):
        # WHY: explicit relative imports and module aliases share the same literal proof.
        # catches: trusting any uppercase member, ignoring shadowing or following a symlink out.
        self.write("pkg/__init__.py", "")
        self.write("pkg/data.py", "ROWS = [1]\nEMPTY = []\nDYNAMIC = get_rows()\n")
        outside = self.scratch / "outside.py"
        outside.write_text("ROWS = [2]\n")
        (self.repo / "outside.py").symlink_to(outside)
        path = self.write("pkg/test_cases.py", """
            from .data import ROWS as entries, EMPTY, DYNAMIC
            import pkg.data as tables
            import outside

            def test_imported():
                for row in entries:
                    assert valid(row)

            def test_qualified():
                for row in tables.ROWS:
                    assert valid(row)

            def test_empty():
                for row in EMPTY:
                    assert valid(row)

            def test_dynamic():
                for row in DYNAMIC:
                    assert valid(row)

            def test_shadowed(tables):
                for row in tables.ROWS:
                    assert valid(row)

            def test_local():
                from .data import ROWS
                for row in ROWS:
                    assert valid(row)

            def test_replaced():
                tables.ROWS = get_rows()
                for row in tables.ROWS:
                    assert valid(row)

            def test_external():
                for row in outside.ROWS:
                    assert valid(row)
            """)
        lines = path.read_text().splitlines()
        wanted = [i for i, line in enumerate(lines, 1)
                  if line.startswith(("def test_empty", "def test_dynamic", "def test_shadowed",
                                      "def test_replaced", "def test_external"))]
        self.assertEqual([line for _, line, rule, _ in self.findings("lint", "pkg/test_cases.py")
                          if rule == "guarded-assert"], wanted)

    def test_python_literal_rows(self):
        # WHY: a destructured literal column can supply the inner loop's length.
        # catches: treating a partly empty column or filtered comprehension as nonempty.
        self.check_lint("""
            def test_rows():
                for label, expected in [(label, (1, 2)) for label in ("x", "y")]:
                    for item in expected:
                        assert valid(label, item)

            def test_empty():
                for label, expected in [("x", ())]:
                    for item in expected:
                        assert valid(label, item)

            def test_filtered():
                for row in [i for i in (1, 2) if choose(i)]:
                    assert valid(row)
            """, "guarded-assert", ("def test_empty", "def test_filtered"))

    def test_rust_imports(self):
        # WHY: crate and parent paths identify a specific file and owning type.
        # catches: inferring a table by suffix, skipping empty arrays, or losing super imports.
        self.write("Cargo.toml", '[package]\nname = "sample"\nversion = "0.1.0"\n')
        self.write("src/lib.rs", "mod tables;\n")
        self.write("src/tables.rs", """
            mod tests;
            pub const ROWS: &[i32] = &[1];
            pub struct On;
            impl On { pub const ALL: [i32; 1] = [2]; }
            pub struct Off;
            impl Off { pub const ALL: [i32; 0] = []; }
            """)
        path = self.write("src/tables/tests.rs", """
            use crate::tables::ROWS as TABLE;
            use super::{On, Off};

            #[test]
            fn imported() { for row in TABLE { assert!(valid(row)); } }

            #[test]
            fn associated() { for row in On::ALL { assert!(valid(row)); } }

            #[test]
            fn empty() { for row in Off::ALL { assert!(valid(row)); } }

            #[test]
            fn dynamic() { for row in load_rows() { assert!(valid(row)); } }
            """)
        lines = path.read_text().splitlines()
        wanted = [i for i, line in enumerate(lines, 1)
                  if line.startswith(("fn empty", "fn dynamic"))]
        self.assertEqual([line for _, line, rule, _ in self.findings("lint", "src/tables/tests.rs")
                          if rule == "guarded-assert"], wanted)

    def test_rust_literal_ranges_and_helpers(self):
        # WHY: arithmetic bounds, mapped ranges, and literal helper tails prove iteration.
        # catches: accepting filter_map, early helper returns, or a zero arithmetic span.
        self.check_lint("""
            fn fixed() -> Vec<i32> { let row = load_row(); vec![row] }
            fn variable() -> Vec<i32> { if skip() { return vec![]; } vec![1] }

            #[test]
            fn mapped() {
                let rows: Vec<_> = (0..(6 / 2)).map(load_row).collect();
                for row in rows { assert!(valid(row)); }
            }
            #[test]
            fn helper() { for row in fixed() { assert!(valid(row)); } }

            #[test]
            fn empty_span() { for row in 2..(6 / 3) { assert!(valid(row)); } }

            #[test]
            fn empty_helper() { for row in variable() { assert!(valid(row)); } }

            #[test]
            fn filtered() {
                let rows: Vec<_> = (0..4).filter_map(load_row).collect();
                for row in rows { assert!(valid(row)); }
            }
            """, "guarded-assert", ("fn empty_span", "fn empty_helper", "fn filtered"), "cases.rs")

    def test_rust_counter_guards(self):
        # WHY: a witness from a fixed range must reach the actual checking branch.
        # catches: crediting exclusive endpoints, unreachable match arms or nested unknown guards.
        self.check_lint("""
            #[test]
            fn reached_if() {
                for i in 1..=3 { if i >= 3 { assert!(valid(i)); } }
            }
            #[test]
            fn reached_match() {
                for i in 1..4 { match i { 3 => assert!(valid(i)), _ => (), } }
            }

            #[test]
            fn missed_if() {
                for i in 1..3 { if i >= 3 { assert!(valid(i)); } }
            }

            #[test]
            fn missed_match() {
                for i in 1..3 { match i { 3 => assert!(valid(i)), _ => (), } }
            }

            #[test]
            fn unknown_branch() {
                for i in 1..=3 { if i >= 3 { if may_skip() { assert!(valid(i)); } } }
            }

            #[test]
            fn break_before_witness() {
                for i in 1..=3 { if stop() { break; } if i >= 3 { assert!(valid(i)); } }
            }

            #[test]
            fn nested_incompatible() {
                for i in 1..=3 { if i >= 3 { if i < 3 { assert!(valid(i)); } } }
            }

            #[test]
            fn may_skip_outer() {
                for _ in load_rows() {
                    for i in 1..=3 { if i >= 3 { assert!(valid(i)); } }
                }
            }
            """, "guarded-assert", ("fn missed_if", "fn missed_match", "fn unknown_branch",
                                   "fn break_before_witness", "fn nested_incompatible",
                                   "fn may_skip_outer"), "cases.rs")

    def test_rust_related_literal_bounds(self):
        # WHY: nested bounds must share a reachable outer counter value.
        # catches: choosing incompatible outer values or crediting an empty suffix.
        self.check_lint("""
            #[test]
            fn fixed_pair() {
                let rows: Vec<_> = [10, 20].into_iter().map(load_row).collect();
                for i in 0..rows.len() {
                    for j in (i + 1)..rows.len() { assert!(different(&rows[i], &rows[j])); }
                }
            }

            #[test]
            fn no_pair() {
                let rows: Vec<_> = [10].into_iter().map(load_row).collect();
                for i in 0..rows.len() {
                    for j in (i + 1)..rows.len() { assert!(different(&rows[i], &rows[j])); }
                }
            }

            #[test]
            fn incompatible_indices() {
                for i in 0..2 {
                    for j in (i + 1)..2 {
                        if i == 1 { assert!(valid(j)); }
                    }
                }
            }
            """, "guarded-assert", ("fn no_pair", "fn incompatible_indices"), "cases.rs")

    def test_rust_proptest_bounds_and_ignore(self):
        # WHY: a strategy's lower bound establishes iteration, and ignored dump tools are skipped.
        # catches: treating a zero lower bound as positive or applying #[ignore] to another test.
        self.check_lint("""
            proptest! {
                #[test]
                fn positive(n in 1..8) {
                    for i in 0..n { prop_assert!(valid(i)); }
                }

                #[test]
                fn maybe_empty(n in 0..8) {
                    for i in 0..n { prop_assert!(valid(i)); }
                }

                #[test]
                fn shadowed(n in 1..8) {
                    let n = output_count();
                    for i in 0..n { prop_assert!(valid(i)); }
                }
            }

            #[test]
            #[ignore = "writes snapshots"]
            fn dump() { for row in load_rows() { write(row).expect("write snapshot"); } }

            #[test]
            #[ignore = "requires extracted fixtures"]
            fn parity() { for row in load_rows() { assert!(valid(row)); } }

            #[test]
            fn normal() { for row in load_rows() { assert!(valid(row)); } }
            """, "guarded-assert", ("fn maybe_empty", "fn shadowed", "fn parity", "fn normal"), "cases.rs")

    def test_rust_growth_from_fixed_inputs(self):
        # WHY: a definite insertion can prove a collection is nonempty when it starts empty.
        # catches: crediting insertions inside an unknown loop or an unknown branch.
        self.check_lint("""
            #[test]
            fn extended() {
                let mut rows = Vec::new();
                rows.extend((0..2).map(make_row));
                for row in rows { assert!(valid(row)); }
            }

            #[test]
            fn pushed() {
                let mut rows = Vec::new();
                for i in 0..2 { rows.push(make_row(i)); }
                for row in rows { assert!(valid(row)); }
            }

            #[test]
            fn maybe_pushed() {
                let mut rows = Vec::new();
                for i in dynamic() { rows.push(make_row(i)); }
                for row in rows { assert!(valid(row)); }
            }

            #[test]
            fn conditionally_pushed() {
                let mut rows = Vec::new();
                for i in 0..2 { if condition() { rows.push(make_row(i)); } }
                for row in rows { assert!(valid(row)); }
            }
            """, "guarded-assert", ("fn maybe_pushed", "fn conditionally_pushed"), "cases.rs")

    def test_tokio_join_set_population(self):
        # WHY: a join drains work only if prior code submitted a task.
        # catches: using an unknown range or crediting a join after the set was drained.
        self.check_lint("""
            #[tokio::test]
            async fn joins() {
                let mut tasks = tokio::task::JoinSet::new();
                for i in 0..2 { tasks.spawn(work(i)); }
                while let Some(result) = tasks.join_next().await { assert!(result.is_ok()); }
            }

            #[tokio::test]
            async fn may_never_join() {
                let mut tasks = tokio::task::JoinSet::new();
                for i in jobs() { tasks.spawn(work(i)); }
                while let Some(result) = tasks.join_next().await { assert!(result.is_ok()); }
            }

            #[tokio::test]
            async fn drained() {
                let mut tasks = tokio::task::JoinSet::new();
                for i in 0..2 { tasks.spawn(work(i)); }
                tasks.join_all().await;
                while let Some(result) = tasks.join_next().await { assert!(result.is_ok()); }
            }
            """, "guarded-assert", ("fn may_never_join", "fn drained"), "cases.rs")
