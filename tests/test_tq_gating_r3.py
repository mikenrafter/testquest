"""Regression cases for collection controls and effect claims."""

from test_tq import TqCase


class GatingRoundThree(TqCase):
    def check_directory(self, rule, wanted):
        rows = self.findings("lint", self.repo)
        self.assertEqual([(path, line) for path, line, code, _ in rows if code == rule], wanted)

    def test_rust_twin_requires_same_body_and_crate(self):
        # WHY: a positive check in another file can establish that an exact copy fires.
        # catches: keeping the twin finding or matching on name alone or across crates.
        for crate in ("a", "b"):
            self.write(f"{crate}/Cargo.toml", f'[package]\nname = "{crate}"')
        self.write("a/src/control.rs", """
            fn scan(dir: &Path) -> Vec<PathBuf> { collect(dir, ".tmp") }
            #[test]
            fn finds_one() { assert_eq!(scan(known()).len(), 1); }
            """)
        self.write("a/src/twin.rs", """
            fn scan(dir: &Path) -> Vec<PathBuf> { collect(dir, ".tmp") }
            #[test]
            fn finds_none() { assert!(scan(clean()).is_empty()); }
            """)
        self.write("a/src/different.rs", """
            fn scan(dir: &Path) -> Vec<PathBuf> { collect(dir, ".bak") }
            #[test]
            fn finds_none() { assert!(scan(clean()).is_empty()); }
            """)
        self.write("b/src/unrelated.rs", """
            fn scan(dir: &Path) -> Vec<PathBuf> { collect(dir, ".tmp") }
            #[test]
            fn finds_none() { assert!(scan(clean()).is_empty()); }
            """)
        self.check_directory("absence-no-control", [("different.rs", 3), ("unrelated.rs", 3)])

    def test_python_twin_requires_same_body(self):
        # WHY: a positive check of a copied detector is transferable across files.
        # catches: matching detectors with different filters by function name.
        self.write("test_control.py", """
            def scan(path) -> list:
                return collect(path, ".tmp")

            def test_finds_one():
                assert len(scan(known())) == 1
            """)
        self.write("test_twin.py", """
            def scan(path) -> list:
                return collect(path, ".tmp")

            def test_finds_none():
                assert not scan(clean())
            """)
        self.write("test_different.py", """
            def scan(path) -> list:
                return collect(path, ".bak")

            def test_finds_none():
                assert not scan(clean())
            """)
        self.check_directory("absence-no-control", [("test_different.py", 5)])

    def test_rust_sibling_recorder_uses_the_same_producer_and_slot(self):
        # WHY: a sibling can prove a producer writes to the same recorder argument.
        # catches: considering unrelated writes or a different producer argument evidence.
        self.check_lint("""
            fn scan(input: Input, details: &mut Vec<Row>, report: &mut Vec<Row>) {
                if input.invalid { report.push(input.row); }
            }
            fn validate(input: Input, details: &mut Vec<Row>, report: &mut Vec<Row>) {}

            #[test]
            fn reports_fault() {
                let mut details = Vec::new();
                let mut report = Vec::new();
                scan(faulty(), &mut details, &mut report);
                assert_eq!(report.len(), 1);
            }

            #[test]
            fn no_report_for_valid_input() {
                let mut details = Vec::new();
                let mut report = Vec::new();
                scan(valid(), &mut details, &mut report);
                assert!(report.is_empty());
                assert!(details.is_empty()); // different argument
            }

            #[test]
            fn no_validation_report() {
                let mut details = Vec::new();
                let mut report = Vec::new();
                validate(valid(), &mut details, &mut report);
                assert!(report.is_empty()); // different producer
            }
            struct Scanner;
            impl Scanner {
                fn new() -> Self { Self }
                fn collect(&self, input: Input, report: &mut Vec<Row>) {
                    if input.invalid { report.push(input.row); }
                }
            }

            mod positive {
                use super::*;
                #[test]
                fn scanner_reports_fault() {
                    let scanner = Scanner::new();
                    let mut report = Vec::new();
                    scanner.collect(faulty(), &mut report);
                    assert_eq!(report.len(), 1);
                }
            }

            mod negative {
                use super::*;
                #[test]
                fn scanner_ignores_valid_input() {
                    let scanner = Scanner::new();
                    let mut report = Vec::new();
                    scanner.collect(valid(), &mut report);
                    assert!(report.is_empty());
                }
            }

            mod shadowed {
                use super::*;
                struct Scanner;
                impl Scanner {
                    fn new() -> Self { Self }
                    fn collect(&self, _: Input, _: &mut Vec<Row>) {}
                }

                #[test]
                fn shadowed_scanner_has_no_control() {
                    let scanner = Scanner::new();
                    let mut report = Vec::new();
                    scanner.collect(valid(), &mut report);
                    assert!(report.is_empty()); // different receiver type
                }
            }
            """, "absence-no-control", ("// different argument", "// different producer",
                                       "// different receiver type"), "checks.rs")

    def test_rust_runtime_sweep_requires_a_nonempty_input(self):
        # WHY: filtering zero directory entries produces no violations regardless of the predicate.
        # catches: ignoring inline filtered collections or disregarding the source length check.
        self.check_lint("""
            #[test]
            fn files_have_no_errors() {
                let entries: Vec<_> = std::fs::read_dir("fixtures").unwrap().flatten().collect();
                let errors = entries.iter().filter(|entry| invalid(entry)).collect::<Vec<_>>();
                assert!(errors.is_empty()); // unchecked inventory
            }

            #[test]
            fn three_files_have_no_errors() {
                let entries: Vec<_> = std::fs::read_dir("fixtures").unwrap().flatten().collect();
                assert_eq!(entries.len(), 3);
                let errors: Vec<_> = entries.iter().filter(|entry| invalid(entry)).collect();
                assert!(errors.is_empty());
            }

            #[test]
            fn literal_fixture_has_no_errors() {
                let entries = ["a", "b"];
                let errors: Vec<_> = entries.iter().filter(|entry| invalid(entry)).collect();
                assert!(errors.is_empty());
            }
            """, "absence-no-control", ("// unchecked inventory",), "checks.rs")

    def test_python_runtime_sweep_requires_a_nonempty_input(self):
        # WHY: a comprehension over a missing asset directory can pass vacuously.
        # catches: ignoring comprehensions or treating a count of a separate input as coverage.
        self.check_lint("""
            def test_files_have_no_errors():
                entries = list(Path("fixtures").glob("*.json"))
                errors = [entry for entry in entries if invalid(entry)]
                assert not errors  # unchecked inventory

            def test_three_files_have_no_errors():
                entries = list(Path("fixtures").glob("*.json"))
                assert len(entries) == 3
                errors = [entry for entry in entries if invalid(entry)]
                assert not errors

            def test_three_other_files_do_not_cover_this_sweep():
                entries = list(Path("fixtures").glob("*.json"))
                other = list(Path("other").glob("*.json"))
                assert len(other) == 3
                errors = [entry for entry in entries if invalid(entry)]
                assert not errors  # wrong inventory
            """, "absence-no-control", ("# unchecked inventory", "# wrong inventory"))

    def test_rust_status_checks_do_not_verify_effects(self):
        # WHY: exit status or a successful extraction can coexist with missing output.
        # catches: accepting a zero exit code for output or flagging a checked effect.
        self.check_lint("""
            #[test]
            fn writes_manifest() {
                assert!(generate().is_ok());
            }

            #[test]
            fn emits_warning() {
                let output = command().output().unwrap();
                assert!(output.status.success());
            }

            #[test]
            fn deletes_cache() {
                assert_eq!(command().status().unwrap().code(), Some(0));
            }

            #[test]
            fn writes_checked_manifest() {
                let output = command().output().unwrap();
                assert!(output.status.success());
                assert!(manifest_path().is_file());
            }

            #[test]
            fn command_runs_successfully() {
                assert!(command().status().unwrap().success());
            }

            #[test]
            fn rejects_invalid_manifest() {
                assert!(generate_invalid().is_err());
            }

            fn init() { assert!(command().status().unwrap().success()); }

            #[test]
            fn init_is_idempotent() {
                init();
                init();
            }

            fn assert_manifest() { assert!(manifest_path().is_file()); }

            #[test]
            fn writes_with_helper_check() {
                assert!(generate().is_ok());
                assert_manifest();
            }

            #[test]
            fn validates_after_direct_manifest_writes_without_using_a_cli() {
                assert!(validate_manifest().is_ok());
            }
            """, "no-assert", ("fn writes_manifest", "fn emits_warning", "fn deletes_cache",
                               "fn init_is_idempotent"), "checks.rs")

    def test_python_status_checks_do_not_verify_effects(self):
        # WHY: a subprocess can exit successfully without printing or writing anything.
        # catches: accepting status assertions or flagging a checked effect.
        self.check_lint("""
            def test_prints_summary():
                run = subprocess.run(["report"], capture_output=True)
                self.assertEqual(run.returncode, 0)

            def test_writes_manifest():
                run = subprocess.run(["generate"], capture_output=True)
                assert run.returncode == 0

            def test_emits_a_warning():
                assert run_report().is_ok()

            def test_prints_checked_summary():
                run = subprocess.run(["report"], capture_output=True)
                assert run.returncode == 0
                assert run.stdout == b"summary"

            def test_command_runs_successfully():
                assert subprocess.run(["report"]).returncode == 0
            """, "no-assert", ("def test_prints_summary", "def test_writes_manifest", "def test_emits_a_warning"))
