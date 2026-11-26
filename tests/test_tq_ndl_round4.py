"""CLI regressions for the nondistinguishing-literal advisory."""

from test_tq import TqCase


class NondistinguishingLiteralsRound4(TqCase):
    def test_python_negative_case_names(self):
        # WHY: a name alone cannot locate an input case.
        # catches: reading an output, encoded literal, setup action, or callee name as a missing input.
        self.check_lint("""
            def test_no_note_reaches_the_cache(self):
                self.assertNotIn("note", collect("history"))

            def test_empty_path_segments():
                assert normalize("src//utils.py") == "src/utils.py"

            def test_runs_without_data(root):
                (root / "inputs").unlink()
                assert status("runs") == 2

            def test_no_chronology():
                assert no_chronology(5) == "no timestamps"

            def message(command, body):
                return f"{command} {body}".strip()

            def test_supports_commandless_and_empty_bodies():
                assert message("/compact", "notes") == "/compact notes"
                assert message("/compact", "") == "/compact"

            def test_supports_commandless_requests():
                assert message("", "notes") == "notes"

            def test_every_input_changes_the_key():
                assert key("short", False) != key("long", False)
            """, "nondistinguishing-literal", ('message("/compact", "notes")', 'key("short"'))

    def test_rust_negative_case_names(self):
        # WHY: an every-input claim still calls for variation in every argument.
        # catches: keeping guesses about negative cases or dropping the every-input check.
        self.check_lint("""
            #[test]
            fn no_note_reaches_the_cache() {
                assert!(!collect("history").contains("note"));
            }
            #[test]
            fn empty_path_segments() {
                assert_eq!(normalize("src//utils.rs"), "src/utils.rs");
            }
            #[test]
            fn runs_without_data() {
                config.size = Some(0);
                assert_eq!(status("runs"), 2);
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
            #[test]
            fn supports_commandless_requests() {
                assert_eq!(message("", "notes"), "notes");
            }
            #[test]
            fn every_input_changes_the_key() {
                assert_ne!(key("short", false), key("long", false));
            }
            """, "nondistinguishing-literal", ('message("/compact", "notes")', 'key("short"'), "checks.rs")

    def test_python_bound_sources(self):
        # WHY: returned tiers and counts of file text make thresholds meaningful.
        # catches: guessing tiers from unrelated literals or losing detection for computed scores.
        self.check_lint("""
            import re
            from pathlib import Path

            def confidence(name):
                if name.startswith("db"):
                    return 0.90
                return 0.70

            def score(size):
                factor = 0.80
                return factor * size

            def test_bounds():
                assert 0.5 <= confidence("generic") < 0.90
                assert score(10) > 0.80

            def test_doc():
                text = Path("README.md").read_text()
                assert len(re.findall("marker", text)) > 3
                assert text.count("marker") > 3

            class SourceChecks:
                @classmethod
                def setUpClass(cls):
                    cls.source = Path("app.py").read_text()

                def test_counts_calls(self):
                    self.assertGreater(self.source.count("throttle()"), 1)
            """, "nondistinguishing-literal", ("assert score",))

    def test_rust_bound_sources(self):
        # WHY: an exact tier boundary identifies a branch while a repeated factor does not.
        # catches: treating any numeric literal in a function body as a returned tier.
        self.check_lint("""
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
            fn test_bounds() {
                let tier = confidence("generic");
                assert!(tier >= 0.5 && tier < 0.90);
                assert!(score(10.0) > 0.80);
            }
            #[test]
            fn test_doc() {
                let text = std::fs::read_to_string("README.md").unwrap();
                assert!(text.matches("marker").count() > 3);
            }
            #[test]
            fn test_process_measurement() {
                let count = |name| std::fs::read_dir(root.join(name)).unwrap().count();
                let threads = count("task");
                assert!(threads > 0);
            }
            """, "nondistinguishing-literal", ("assert!(score",), "checks.rs")

    def test_python_fixed_output(self):
        # WHY: an output prefix can satisfy an assertion intended to observe a row.
        # catches: counting a conditional row as a fixed prefix or missing an always-present heading.
        self.check_lint("""
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
            """, "nondistinguishing-literal", ('or "GURU"',))

    def test_rust_fixed_output(self):
        # WHY: the fixed heading of a sibling module's table can satisfy a row assertion.
        # catches: inspecting only the test file or treating branch-dependent text as fixed.
        self.write("lib.rs", "mod table; mod checks;")
        self.write("table/mod.rs", """
            pub(crate) fn table(rows: &[Row]) -> String {
                let mut out = String::new();
                out.push_str(&format!("{:>10}\\n", "GURU (~)"));
                for row in rows {
                    out.push_str(&format!("+guru {:>4}\\n", row.cost));
                }
                out
            }
            """)
        self.check_lint("""
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
            """, "nondistinguishing-literal", ('|| out.contains("GURU")',), "checks.rs")

    def test_python_order_without_cardinality(self):
        # WHY: sorting an empty result always gives the same result.
        # catches: accepting a sort-only oracle or ignoring a separate item count.
        self.check_lint("""
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
            """, "nondistinguishing-literal", ("medians == sorted", "unchecked task count"))

    def test_rust_order_without_cardinality(self):
        # WHY: adjacent pairs cannot witness order in a result shorter than two.
        # catches: accepting a true windows predicate or ignoring a separate item count.
        self.check_lint("""
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
            """, "nondistinguishing-literal", (
                "assert!(ordered)", "empty languages would pass", "empty rows would pass",
            ), "checks.rs")
