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

    def test_python_fixture_cardinality(self):
        # why: agentpace and mstack iterate imported tables and destructured fixture rows.
        # catches: dropping cardinality at an import alias, tuple target or literal default
        self.write("fixtures.py", """
            AXES = ('left', 'right')
            GAPS = ()
        """)
        path = self.write("test_axes.py", """
            import fixtures as cases

            class TestAxes:
                ladder = (('left', (1, 2)), ('right', (3, 4)))

                def test_ladder(self):
                    for axis in cases.AXES:
                        for label, expected in self.ladder:
                            for value, want in zip(expected, (5, 6), strict=True):
                                assert measure(axis, label, value) == want

                def test_unknown(self):
                    for axis in cases.GAPS:
                        assert measure(axis)
        """)
        self.assertEqual(self.lint_rows(path.name), [(12, "guarded-assert")])

    def test_rust_fixture_cardinality(self):
        # why: fixed lengths drive the pairwise hashes in diff-core and Polara menu checks.
        # catches: losing literal return, associated constants or destructured field sizes
        self.write("Cargo.toml", '[package]\nname = "fixture"\nversion = "0.1.0"\n')
        self.write("src/lib.rs", "pub mod tables;")
        self.write("src/tables.rs", """
            pub struct Menu;
            impl Menu { pub const ITEMS: [i32; 2] = [3, 4]; }
            pub fn samples() -> Vec<(&'static str, [i32; 2])> {
                vec![("title", [1, 2]), ("pause", [3, 4])]
            }
        """)
        path = self.write("tests/layout.rs", """
            use fixture::tables::{Menu, samples};
            #[test]
            fn layout() {
                for cursor in 0..Menu::ITEMS.len() {
                    for (name, ranks) in samples() {
                        for rank in ranks {
                            assert_eq!(read(name, rank), cursor);
                        }
                    }
                }
            }

            #[test]
            fn no_pair() {
                let values = vec![9];
                for i in 0..values.len() {
                    for j in i + 1..values.len() { assert_ne!(values[i], values[j]); }
                }
            }
        """)
        self.assertEqual(self.lint_rows("tests/layout.rs"), [(14, "guarded-assert")])

    def test_projected_proptest_fixture(self):
        # why: diff-core maps a generated nonempty input vector to IR before iterating.
        # catches: losing cardinality through a reference or map/collect, or assuming every strategy is nonempty
        path = self.write("graph.rs", """
            proptest! {
                #[test]
                fn positive(files in prop::collection::vec(parse_strategy(), 1..5)) {
                    let ir: Vec<_> = files.iter().map(IrFile::from).collect();
                    for file in &ir { prop_assert!(graph.has(file)); }
                }

                #[test]
                fn maybe_empty(files in prop::collection::vec(parse_strategy(), 0..5)) {
                    let ir: Vec<_> = files.iter().map(IrFile::from).collect();
                    for file in &ir { prop_assert!(graph.has(file)); }
                }
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(9, "guarded-assert")])

    def test_artifact_absence_and_filtered_inventory(self):
        # why: a direct inventory checks a filesystem effect, a filter can silently skip its inputs.
        # catches: flagging artifact globs, or exempting a filtered sweep with no witnessed input
        python = self.write("test_output.py", """
            def test_failed_build(root):
                assert build(root) == 2
                assert list(root.glob('*.apk')) == []

            def test_sweep(root):
                errors = [f for f in root.iterdir() if has_error(f)]
                assert errors == []
        """)
        rust = self.write("output.rs", """
            #[test]
            fn artifact() {
                assert!(build().is_err());
                let remaining: Vec<_> = std::fs::read_dir(out()).unwrap().collect();
                assert!(remaining.is_empty());
            }

            #[test]
            fn sweep() {
                let errors: Vec<_> = std::fs::read_dir(out()).unwrap().filter_map(bad).collect();
                assert!(errors.is_empty());
            }
        """)
        self.assertEqual(self.lint_rows(python.name), [(7, "absence-no-control")])
        self.assertEqual(self.lint_rows(rust.name), [(11, "absence-no-control")])

    def test_checked_projection_and_detector_identity(self):
        # why: paneweft pins an input through its name projection, and qex checks a detector's output.
        # catches: ignoring projected positive controls or accepting a different detector
        path = self.write("checks.rs", """
            fn sessions(input: Input) -> Vec<Session> { find(input) }
            fn unrelated(input: Input) -> Vec<Session> { find(input) }
            #[test]
            fn sessions_with_rows() {
                let cases = fixtures();
                let names: Vec<_> = cases.iter().map(|case| case.name).collect();
                for required in ["first", "second"] { assert!(names.contains(&required)); }
                let mut failures = Vec::new();
                for case in cases {
                    if actual(case) != expected(case) { failures.push(case); }
                }
                assert!(failures.is_empty());
                let rows = sessions(populated());
                let names: Vec<_> = rows.iter().map(|row| row.name).collect();
                assert_eq!(names, ["claude"]);
                assert!(sessions(bare()).is_empty());
                assert!(unrelated(bare()).is_empty());
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(17, "absence-no-control")])

    def test_inline_mutations_preserve_snapshots(self):
        # why: Polara compares before and after a mutable call inside an assertion.
        # catches: comparing an updated operand to its snapshot as if it were an alias
        path = self.write("cursor.rs", """
            #[test]
            fn compare_after_action() {
                let mut cursor = Choice::Next;
                let before = cursor;
                assert_eq!(update(&mut cursor), None);
                assert_eq!(cursor, before);
            }

            #[test]
            fn compare_alias() {
                let cursor = read();
                let before = cursor;
                assert_eq!(cursor, before);
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(13, "self-compare")])

    def test_fixed_range_reaches_checked_branches(self):
        # why: Polara checks specific authored simulation frames inside a fixed range.
        # catches: classifying all match arms as optional or treating an out-of-range arm as reachable
        path = self.write("clock.rs", """
            #[test]
            fn boundary() {
                for tick in 1..=81 {
                    match tick {
                        80 => assert_eq!(clock(tick), 11),
                        81 => assert_eq!(clock(tick), 12),
                        _ => {},
                    }
                }
            }

            #[test]
            fn unreachable_boundary() {
                for tick in 1..81 {
                    if tick >= 81 { assert_eq!(clock(tick), 12); }
                }
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(13, "guarded-assert")])

    def test_retry_loop_and_opt_in_exit(self):
        # why: qex fails if all retries miss, and live integration tests require explicit opt-in.
        # catches: masking a normal early return or ignoring a final failure after checked success
        path = self.write("runtime.rs", """
            #[test]
            fn retry() {
                for _ in 0..100 {
                    if clock_changed() { continue; }
                    assert_eq!(state(), 7);
                    return;
                }
                panic!("the boundary never ran");
            }

            #[test]
            fn opt_in() {
                if std::env::var("RUN_LIVE").is_err() {
                    eprintln!("skipping live test");
                    return;
                }
                assert_eq!(connect(), 7);
            }

            #[test]
            fn guard_in_production_result() {
                if state() == 0 { return; }
                assert_eq!(state(), 7);
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(21, "guarded-assert")])

    def test_join_set_drains_spawned_work(self):
        # why: the diff-core concurrent fixture spawns ten tasks before checking every join.
        # catches: ignoring the spawned tasks or assuming an emptied JoinSet still contains work
        path = self.write("join.rs", """
            #[tokio::test]
            async fn join_work() {
                let mut set = tokio::task::JoinSet::new();
                for _ in 0..10 { set.spawn(async { answer().await }); }
                while let Some(result) = set.join_next().await {
                    assert_eq!(result.unwrap(), 42);
                }
            }

            #[tokio::test]
            async fn empty_after_shutdown() {
                let mut set = tokio::task::JoinSet::new();
                for _ in 0..10 { set.spawn(async { answer().await }); }
                set.shutdown().await;
                while let Some(result) = set.join_next().await {
                    assert_eq!(result.unwrap(), 42);
                }
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(11, "guarded-assert")])

    def test_python_explicit_script_table(self):
        # why: the mstack ledger tests load their fixture axes from an extensionless script.
        # catches: ignoring static script imports or treating an arbitrary runtime value as a table
        self.write("review-ledger", """
            AXES = ("first", "last")
            UNKNOWN = source()
        """)
        self.write("tests/test_ledger.py", """
            import importlib.machinery
            import importlib.util
            from pathlib import Path

            SCRIPT = Path(__file__).resolve().parents[1] / "review-ledger"
            loader = importlib.machinery.SourceFileLoader("review_ledger", str(SCRIPT))
            spec = importlib.util.spec_from_loader("review_ledger", loader)
            ledger = importlib.util.module_from_spec(spec)
            loader.exec_module(ledger)

            def test_axes():
                for axis in ledger.AXES:
                    if axis == "last":
                        continue
                    assert expected(axis)

            def test_unknown():
                for axis in ledger.UNKNOWN:
                    assert expected(axis)
        """)
        self.assertEqual(self.lint_rows("tests/test_ledger.py"), [(17, "guarded-assert")])

    def test_detector_controls_in_helpers_and_literal_vectors(self):
        # why: Polara checks the same drawing detector in a helper, and Vigil compares the waiter vector.
        # catches: ignoring a called positive helper or requiring literal vector elements to be constants
        path = self.write("control.rs", """
            fn underlines(sim: &Sim) -> Vec<Line> { collect(sim) }
            fn waiters(lock: &Lock) -> Vec<Pid> { inspect(lock) }
            fn assert_focus(sim: &Sim) {
                let lines = underlines(sim);
                assert_eq!(lines.len(), 1);
            }

            #[test]
            fn layouts() {
                assert_focus(&menu());
                assert!(underlines(&dialog()).is_empty());
            }

            #[test]
            fn waiting() {
                assert!(waiters(&idle()).is_empty());
                assert_eq!(waiters(&busy()), vec![process::id()]);
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [])

    def test_closure_rows_are_read_by_serialization(self):
        # why: the Polara diagnostic writes closure-recorded frames via a JSON macro.
        # catches: reporting the recorded output as unread when the serializer consumes it
        path = self.write("visuals.rs", """
            #[test]
            #[ignore = "writes diagnostic display lists"]
            fn diagnostic() {
                let mut rows = Vec::new();
                let mut save = |frame| { rows.push(render(frame)); };
                save(42);
                write(serde_json::to_vec(&serde_json::json!({"rows":rows})).unwrap());
            }

            #[test]
            fn never_read() {
                let mut rows = Vec::new();
                let mut save = |frame| { rows.push(render(frame)); };
                save(42);
                assert!(ready());
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(13, "absence-no-control")])

    def test_dictionary_subscript_in_strict_zip(self):
        # why: agentpace compares quantiles with a known nonempty row via strict zip.
        # catches: losing cardinality through a keyed fixture lookup, or treating ordinary zip as strict
        path = self.write("test_quantiles.py", """
            EXPECTED = {"front": (4, 5), "back": (1, 2, 3)}

            def test_strict_rows():
                for name in EXPECTED.keys():
                    for value, desired in zip(fetch(name), EXPECTED[name], strict=True):
                        assert desired == value

            def test_truncated_rows():
                for name in EXPECTED.keys():
                    for value, desired in zip(fetch(name), EXPECTED[name]):
                        assert desired == value
        """)
        self.assertEqual(self.lint_rows(path.name), [(8, "guarded-assert")])

    def test_rust_sliced_and_concatenated_fixture_rows(self):
        # why: the remaining Polara cases iterate borrowed slices and concatenated arrays.
        # catches: losing a literal row across slicing or concat, or assuming a parameter is a fixture
        path = self.write("palettes.rs", """
            fn palette(face: Color) -> [Color; 1] { [face] }
            fn unknown(entries: Vec<Color>) -> Vec<Color> { entries }
            #[test]
            fn faces() {
                let panels = [palette(read_panel()), palette(read_plate())].concat();
                let cases = [(1, &panels[..]), (2, &[Color::Light][..])];
                for (role, faces) in cases {
                    for face in faces { assert!(readable(role, face)); }
                }
            }

            #[test]
            fn unknown_faces() {
                for face in unknown(read_faces()) { assert!(readable(1, face)); }
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(13, "guarded-assert")])

    def test_rust_command_probe_and_diagnostic(self):
        # why: qex probes host-tool availability and Polara explicitly marks the ignored diagnostic.
        # catches: treating a tool availability probe or diagnostic configuration rejection as a test guard
        path = self.write("checks.rs", """
            #[test]
            fn needs_tool() {
                if Command::new("helper").arg("--version").output().is_err() {
                    return;
                }
                assert!(observable());
            }

            #[test]
            #[ignore = "research diagnostic"]
            fn diagnostic() {
                match std::env::var("LEVEL").as_deref() {
                    Ok("high") => render_high(),
                    Ok("low") => render_low(),
                    _ => panic!("Choose a level"),
                }
            }

            #[test]
            fn production_probe() {
                if Command::new("helper").arg("--process").output().is_err() {
                    return;
                }
                assert!(observable());
            }
        """)
        self.assertEqual(self.lint_rows(path.name), [(20, "guarded-assert")])
