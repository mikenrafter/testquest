"""The ast-grep engine reports nondistinguishing-literal for Python and Rust tests."""
import importlib.util
import json
import os
import unittest

from test_tq import TqCase

RULE = "nondistinguishing-literal"
ENGINE = os.environ.get("TQ_NDL_ENGINE", "ast-grep")


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py") or ENGINE == "legacy",
                     "ast-grep-py is optional")
class AstGrepLiterals(TqCase):
    def check(self, source, wanted, name="test_cases.py"):
        path = self.write(name, source)
        lines = path.read_text(encoding="utf-8").splitlines()
        expected = [number for number, line in enumerate(lines, 1)
                    if any(marker in line for marker in wanted)]
        self.assertEqual(len(expected), len(wanted), wanted)
        rows = json.loads(self.tq("lint", "--engine", ENGINE, "--json", path.relative_to(self.repo),
                                  code=(0, 1)))
        self.assertEqual([row["line"] for row in rows if row["rule"] == RULE], expected, rows)
        self.assertTrue(all(row["level"] == "advisory" for row in rows if row["rule"] == RULE))

    def test_python_identity_pairs(self):
        # WHY: only the repeated dimensions can conceal a width/height swap.
        # catches: keeping identity pairs or skipping the rest of a call after one.
        self.check("""
            def test_inputs():
                assert object_at("PointStart", "Marker", 0, 0) == point
                assert meta(0, 0, 2, 1) == rectangle
                assert floating(0.0, -0.0) == origin
                assert text("", "") == empty
                assert octets(b"", b"") == empty
                assert tuples((), ()) == empty
                assert lists([], []) == empty

            def test_square():
                assert meta(0, 0, 2, 2) == square
            """, ("meta(0, 0, 2, 2)",))

    def test_rust_identity_pairs(self):
        # WHY: zero/empty pairs alone supply no evidence of a parameter mix-up.
        # catches: discarding a later non-identity pair or keeping signed float zero pairs.
        self.check("""
            #[test]
            fn test_inputs() {
                assert_eq!(SecondBoss::new(0, 0), origin);
                assert_eq!(meta(0, 0, 2, 1), rectangle);
                assert_eq!(floating(0.0, -0.0), origin);
                assert_eq!(text("", ""), empty);
                assert_eq!(octets(b"", b""), empty);
                assert_eq!(tuples((), ()), empty);
                assert_eq!(arrays([], []), empty);
            }

            #[test]
            fn test_retained_pairs() {
                assert_eq!(meta(0, 0, 2, 2), square);
                assert_eq!(art_period(&image(), 6, 2, 2), period);
            }
            """, ("meta(0, 0, 2, 2)", "art_period"), "checks.rs")

    def test_python_symmetry_distinguished_in_same_test(self):
        # WHY: both duplicated slots need known unequal values to prove they are distinguished.
        # catches: accepting an unchecked or unknown witness, or borrowing one from another callee.
        self.check("""
            def test_inputs():
                assert difference(6, 6) == 0
                difference(6, 2)
                assert other.difference(6, 6) == 0
                assert other.difference(6, unknown) == 0
                assert mix(3, 3, 7, 7) == first
                assert mix(1, 2, 7, 7) == second
            """, ("assert difference(6, 6)", "other.difference(6, 6)", "mix(3", "mix(1"))

    def test_rust_symmetry_distinguished_in_same_test(self):
        # WHY: an asymmetric call distinguishes only its own callee and argument pair.
        # catches: accepting an unchecked witness or treating another receiver as the same callee.
        self.check("""
            #[test]
            fn test_inputs() {
                assert_eq!(difference(6, 6), 0);
                difference(6, 2);
                assert_eq!(other.difference(6, 6), 0);
                assert_eq!(other.difference(6, unknown), 0);
                assert_eq!(mix(3, 3, 7, 7), first);
                assert_eq!(mix(1, 2, 7, 7), second);
            }
            """, ("assert_eq!(difference(6, 6)", "other.difference(6, 6)", "mix(3", "mix(1"),
                   "checks.rs")

    def test_python_unvaried_checked_arguments(self):
        # WHY: comparing these instants exercises seconds and leaves nanoseconds at zero.
        # catches: missing bound results, keyword reordering, or multiple reports per callee.
        self.check("""
            def test_instants(self):
                StreamInstant(9, 0)
                first = StreamInstant(2, 0)
                self.assertEqual(first.seconds, 2)
                assert StreamInstant(-2, 0).seconds == -2
                prop_assert(StreamInstant(-2, 0).seconds == -2)

            def test_keywords():
                assert render(width=3, height=1) == short
                assert build(width=3, height=1) == short
                assert render(height=1, width=5) == long
                assert build(height=2, width=5) == tall
            """, ("StreamInstant(9, 0)", "render(width=3"))

    def test_rust_unvaried_checked_arguments(self):
        # WHY: every checked instant has zero nanos despite distinct literal seconds.
        # catches: missing method receivers, signed literals, or splitting a qualified callee.
        self.check("""
            #[test]
            fn test_instants() {
                StreamInstant::new(9, 0);
                let first = StreamInstant::new(2, 0);
                let min = StreamInstant::new(i64::MIN, 0);
                assert_eq!(first.sub(duration), Some(StreamInstant::new(-2, 0)));
                assert_ne!(min.sub(duration), Some(StreamInstant::new(-1, 0)));
                prop_assert!(StreamInstant::new(3, 0).valid());
                assert!(OtherInstant::new(7, 5).valid());
            }

            #[test]
            fn test_bound_results() {
                let first = Clock::new(3, 0);
                assert!(first.ready());
                assert!(Clock::new(5, 0).ready());
            }
            """, ("StreamInstant::new(9, 0)", "let first = Clock"), "checks.rs")

    def test_python_fixed_values_and_text_arguments(self):
        # WHY: neutral settings and equal coordinates can hide argument handling, but labels do not.
        # catches: retaining pinned values or treating text as a fixed candidate.
        self.check("""
            def test_inputs():
                assert origin(3, 0) == first
                assert origin(5, 0) == second
                assert scale(3, 1.0) == first
                assert scale(5, 1.0) == second
                assert collect(3, []) == first
                assert collect(5, []) == second
                assert clip(3, [2, 2, 10, 10]) == first
                assert clip(5, [2, 2, 10, 10]) == second
                assert plain(3, 0.9) == first
                assert plain(5, 0.9) == second
                assert route("up", 0) == first
                assert route("down", 0) == second
                assert pick(3, label="same", name="same") == first
                assert pick(5, label="same", name="same") == second
            """, ("origin(3", "scale(3", "collect(3", "clip(3", 'route("up"'))

    def test_rust_fixed_values_and_text_arguments(self):
        # WHY: signed identities and equal fixed coordinates can hide missing arithmetic.
        # catches: retaining pinned values or conflating integral and float coordinates.
        self.check("""
            #[test]
            fn test_inputs() {
                assert_eq!(origin(3, 0), first);
                assert_eq!(origin(5, 0), second);
                assert_eq!(scale(3, 1.0f32), first);
                assert_eq!(scale(5, 1.0f32), second);
                assert_eq!(index(3, -1), first);
                assert_eq!(index(5, -1), second);
                assert_eq!(clip(3, [2, 2, 10, 10]), first);
                assert_eq!(clip(5, [2, 2, 10, 10]), second);
                assert_eq!(plain(3, 0.9), first);
                assert_eq!(plain(5, 0.9), second);
                assert_eq!(heterogeneous(3, (2u32, 2i32)), first);
                assert_eq!(heterogeneous(5, (2u32, 2i32)), second);
                assert_eq!(homogeneous(3, (2u32, 2u32)), first);
                assert_eq!(homogeneous(5, (2u32, 2u32)), second);
                assert_eq!(route("up", 0), first);
                assert_eq!(route("down", 0), second);
            }
            """, ("origin(3", "scale(3", "index(3", "clip(3", "homogeneous(3", 'route("up"'),
                   "checks.rs")

    def test_python_unused_parameters_and_expected_constructors(self):
        # WHY: a default-like value matters only in a position that the local signature uses,
        # and expected objects describe the oracle rather than the input.
        # catches: borrowing an unrelated signature, or counting right-side constructors as inputs.
        self.check("""
            def build(size, _padding):
                return size

            def test_inputs(self):
                assert build(3, 0) == first
                assert build(5, 0) == second
                assert other.build(3, 0) == first
                assert other.build(5, 0) == second
                self.assertEqual(first, second=Expected(3, 0))
                assert second == [Expected(5, 0)]
                assert Point(3, 0) == Point(30, 9)
                self.assertEqual(Point(5, 0), Point(50, 9))
                self.assertEqual(first, calculate(3, 0))
                assert second == calculate(5, 0)
            """, ("other.build(3", "Point(3", "calculate(3"))

    def test_rust_unused_parameters_and_expected_constructors(self):
        # WHY: only the matching local signature can identify an unused argument,
        # and only constructors on the actual side exercise the input position.
        # catches: ignoring qualified definitions or counting constructors inside expected values.
        self.check("""
            mod fixtures {
                pub fn build(size: i32, _padding: i32) -> i32 { size }
            }

            fn build(size: i32, padding: i32) -> i32 { size + padding }

            #[test]
            fn test_inputs() {
                assert_eq!(fixtures::build(3, 0), first);
                assert_eq!(fixtures::build(5, 0), second);
                assert_eq!(build(3, 0), first);
                assert_eq!(build(5, 0), second);
                assert_eq!(first, Some(Expected::new(3, 0)));
                assert!(second == Some(Expected::new(5, 0)));
                assert_eq!(Point::new(3, 0), Point::new(30, 9));
                assert!(Point::new(5, 0) == Point::new(50, 9));
                assert_eq!(first, calculate(3, 0));
                assert!(second == calculate(5, 0));
            }
            """, ("assert_eq!(build(3", "Point::new(3", "calculate(3"), "checks.rs")

    def test_python_checked_symmetry_witness(self):
        # WHY: an unused call or a changed fixture can leave the duplicate roles untested.
        # catches: accepting an unchecked witness or letting unrelated fixture changes mask the pair.
        self.check("""
            def test_period():
                assert period(textured, 6, 2, 2) == 4
                assert period(flat, 6, 2, 1) is None
                period(textured, 6, 3, 4)
                assert difference(6, 6) == 0
                assert difference(6, 2) == 4
            """, ("period(textured, 6, 2, 2)",))

    def test_rust_checked_symmetry_witness(self):
        # WHY: only a checked call that distinguishes the duplicate slots can suppress a report.
        # catches: accepting a changed fixture or an unchecked asymmetric call.
        self.check("""
            #[test]
            fn test_period() {
                assert_eq!(period(&image(textured), 6, 2, 2), Some(4));
                assert_eq!(period(&image(flat), 6, 2, 1), None);
                period(&image(textured), 6, 3, 4);
                assert_eq!(difference(6, 6), 0);
                assert_eq!(difference(6, 2), 4);
            }
            """, ("period(&image(textured), 6, 2, 2)",), "checks.rs")

    def test_python_named_coverage_and_negative_case_names(self):
        # WHY: an every-input claim must vary flags even when another argument varies,
        # but a name that only mentions an absence does not locate an input case.
        # catches: requiring numeric identities, or reading an output or setup action as a missing input.
        self.check("""
            def testEveryInputChangesTheKey():
                original = key("short", False)
                assert original != key("long", False)

            def test_every_input_changes_the_hash():
                assert digest("short", False) != digest("long", True)

            def test_takes_noisy_input():
                assert handle("content") == expected

            def test_no_chronology():
                assert no_chronology(5) == "no timestamps"

            def message(command, body):
                return f"{command} {body}".strip()

            def test_supports_commandless_and_empty_bodies():
                assert message("/compact", "notes") == "/compact notes"
                assert message("/compact", "") == "/compact"
            """, ("original = key", 'message("/compact", "notes")'))

    def test_rust_named_coverage_and_negative_case_names(self):
        # WHY: an every-input claim requires variation in fixture arguments as well as in the checked call.
        # catches: ignoring unvaried numeric fixture arguments or boolean inputs.
        self.check("""
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

            #[test]
            fn no_chronology() {
                assert_eq!(no_chronology(5), "no timestamps");
            }

            fn message(command: &str, body: &str) -> String { todo!() }
            #[test]
            fn supports_commandless_and_empty_bodies() {
                assert_eq!(message("/compact", "notes"), "/compact notes");
                assert_eq!(message("/compact", ""), "/compact");
            }
            """, ("let base = spec", "let first = key", 'message("/compact", "notes")'), "checks.rs")

    def test_python_zero_origin_and_checked_value(self):
        # WHY: setup builders live outside the comparison while the fixture times
        # can still expose a zero-origin subtraction.
        # catches: flagging nested setup calls or missing output times copied from zero-based input.
        self.check("""
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
            """, ("offset(7", "waits(marks) == [Gap(10, 10)"))

    def test_rust_boundary_and_checked_value(self):
        # WHY: a small repeated fixture is setup, but a fixed coordinate on the
        # checked call's input can hide how the check handles clipping.
        # catches: counting nested setup as checked or ignoring symmetric float coordinates.
        self.check("""
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
            """, ("offset(7", "item(0.0"), "checks.rs")

    def test_python_loose_bound_and_bound_sources(self):
        # WHY: an exact equality anywhere in the test pins the checked function, and
        # an exact tier boundary identifies a branch while a repeated factor does not.
        # catches: skipping unittest inequalities or treating any numeric literal as a returned tier.
        self.check("""
            def confidence(name):
                if name.startswith("db"):
                    return 0.90
                return 0.70

            def score(size):
                factor = 0.80
                return factor * size

            def test_scores(self):
                score_value = compute(5000, 6000, 10000)
                self.assertGreater(score_value, 0.9)
                assert exact(1000, 2000) < 0.9
                assert exact(3000, 4000) == 0.5
                assert dynamic(user_input) > 0.9

            def test_bounds():
                assert 0.5 <= confidence("generic") < 0.90
                assert score(10) > 0.80
            """, ("score_value = compute", "assert score"))

    def test_rust_loose_bound_and_bound_sources(self):
        # WHY: an exact check rules out a bound-only oracle, even on an alias.
        # catches: skipping inequality macros or overlooking equality on a bound result.
        self.check("""
            fn confidence(name: &str) -> f64 {
                if name.starts_with("db") { return 0.90_f64; }
                0.70
            }
            fn score(size: f64) -> f64 {
                fn calibration() -> f64 { return 0.80; }
                let factor = calibration() + 0.1;
                factor * size
            }
            #[test]
            fn test_scores() {
                let value = compute(5000, 6000, 10000);
                prop_assert!(value >= 0.9);
                let exact = area(1000, 2000);
                assert!(exact < 0.9);
                assert_eq!(exact, 0.5);
                assert!(dynamic(user_input) > 0.9);
            }
            #[test]
            fn test_bounds() {
                let tier = confidence("generic");
                assert!(tier >= 0.5 && tier < 0.90);
                assert!(score(10.0) > 0.80);
            }
            """, ("let value = compute", "assert!(score"), "checks.rs")

    def test_python_fixed_output(self):
        # WHY: an output prefix can satisfy an assertion intended to observe a row.
        # catches: counting a conditional row as a fixed prefix or missing an always-present heading.
        self.check("""
            def table(rows):
                out = "GURU estimates"
                for row in rows:
                    out += f"+guru {row.cost}"
                return out

            def test_guru_estimates_in_rows():
                out = table(rows)
                assert "+guru" in out or "GURU" in out

            def test_guru_row():
                out = table(rows)
                assert "+guru" in out or "+GURU" in out

            def test_guru_header():
                assert "GURU" in table(rows)
            """, ('or "GURU"',))

    def test_rust_fixed_output(self):
        # WHY: the fixed heading of a sibling module's table can satisfy a row assertion.
        # catches: inspecting only the test file or treating branch-dependent text as fixed.
        self.write("src/lib.rs", "mod table; mod checks;")
        self.write("src/table/mod.rs", """
            pub(crate) fn table(rows: &[Row]) -> String {
                let mut out = String::new();
                out.push_str(&format!("{:>10}\\n", "GURU (~)"));
                for row in rows {
                    out.push_str(&format!("+guru {:>4}\\n", row.cost));
                }
                out
            }
            """)
        self.check("""
            #[test]
            fn test_guru_estimates_in_rows() {
                let out = crate::table::table(rows);
                assert!(out.contains("+guru") || out.contains("GURU"));
            }
            #[test]
            fn test_guru_row() {
                let out = crate::table::table(rows);
                assert!(out.contains("+guru") || out.contains("+GURU"));
            }
            #[test]
            fn test_guru_header() {
                assert!(crate::table::table(rows).contains("GURU"));
            }
            """, ('|| out.contains("GURU")',), "src/checks.rs")

    def test_python_order_without_cardinality(self):
        # WHY: sorting an empty result always gives the same result. The per-item loop that
        # the legacy engine also reports (the unchecked task count) was judged false in r5 and is not ported.
        # catches: accepting a sort-only oracle or ignoring a separate item count.
        self.check("""
            def test_medians_are_ascending():
                model = fit(dataset)
                medians = [bucket.median for bucket in model.buckets]
                assert medians == sorted(medians)

            def test_order_and_size():
                rows = rank(dataset)
                assert rows == sorted(rows)
                assert len(rows) == 3
            def test_plan_and_task_ranges(self):
                plan = estimate(dataset)
                self.assertLess(plan.p50, plan.p80)
                for task in plan.tasks:
                    self.assertLess(task.p50, task.p80)  # unchecked task count
                    self.assertLess(task.p80, task.p90)

            def test_each_task_has_a_range(self):
                plan = estimate(dataset)
                self.assertLess(plan.p50, plan.p80)
                self.assertEqual(len(plan.tasks), 3)
                for task in plan.tasks:
                    self.assertLess(task.p50, task.p80)

            def test_guarded_by_itself(self):
                plan = estimate(dataset)
                for task in plan.tasks:
                    self.assertLess(task.p50, task.p80)
            """, ("medians == sorted",))

    def test_rust_order_without_cardinality(self):
        # WHY: adjacent pairs cannot witness order in a result shorter than two.
        # catches: accepting a true windows predicate or ignoring a separate item count.
        self.check("""
            #[test]
            fn output_is_ordered() {
                let rows = rank(dataset);
                let ordered = rows.windows(2).all(|w| w[0] <= w[1]);
                assert!(ordered);
            }
            #[test]
            fn output_has_the_expected_rows() {
                let rows = rank(dataset);
                assert!(rows.windows(2).all(|w| w[0] <= w[1]));
                assert_eq!(rows.len(), 3);
            }
            #[test]
            fn output_orders_report_languages() {
                let report = make_report(dataset);
                let langs = &report.languages;
                let mut sorted = langs.clone();
                sorted.sort();
                assert_eq!(*langs, sorted); // empty languages would pass
            }
            #[test]
            fn output_len_agrees_with_set_len() {
                let rows = rank(dataset);
                assert!(rows.windows(2).all(|w| w[0] <= w[1])); // empty rows would pass
                let unique: HashSet<_> = rows.iter().collect();
                assert_eq!(unique.len(), rows.len());
            }
            #[test]
            fn output_has_the_expected_rows_after_projection() {
                let rows = rank(dataset);
                let ids: Vec<_> = rows.iter().map(|r| r.id).collect();
                assert!(ids.windows(2).all(|w| w[0] <= w[1]));
                assert_eq!(rows.len(), 3);
            }
            #[test]
            fn test_spacing_over_fixed_fixture() {
                let rows: Vec<_> = (0..3).map(position).collect();
                assert!(rows.windows(2).all(|w| w[1] - w[0] == 16));
            }
            """, ("assert!(ordered)", "empty languages would pass", "empty rows would pass"),
                   "checks.rs")


if __name__ == "__main__":
    unittest.main()
