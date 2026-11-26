"""Regression cases for observed outcomes, absence controls, and equal operands."""

from test_tq import TqCase


class GatingRoundTwo(TqCase):
    def test_python_only_setup_and_status_are_checked(self):
        # WHY: a check of the fixture or call status can leave the promised result untested.
        # catches: crediting a setup-only check, printing, or assignment to a discard.
        self.check_lint("""
            def test_setup_only():
                source = sample()
                assert source
                convert(source)

            def test_discard():
                source = sample()
                assert source
                _ = convert(source)

            def test_print_only():
                source = sample()
                assert source
                print(convert(source))

            def test_missing_returns_none():
                assert fetch("missing").is_ok()

            def test_missing_returns_none_checked():
                assert fetch("missing") is None

            def test_convert_and_check_fixture():
                source = sample()
                convert(source)
                assert source == "converted"

            def test_check_then_clean_up():
                assert convert(sample()) == "converted"
                cleanup()
            """, "no-assert", ("def test_setup_only", "def test_discard",
                               "def test_print_only", "def test_missing_returns_none():"))

    def test_rust_only_setup_and_status_are_checked(self):
        # WHY: a successful Result can still contain the wrong value.
        # catches: crediting is_ok for an empty return claim or a fixture check for later work.
        self.check_lint("""
            #[test]
            fn setup_only() {
                let input = fixture();
                assert!(!input.is_empty());
                convert(input);
            }

            #[test]
            fn discard() {
                let input = fixture();
                assert!(!input.is_empty());
                let _ = convert(input);
            }

            #[test]
            fn print_only() {
                let input = fixture();
                assert!(!input.is_empty());
                eprintln!("{:?}", convert(input));
            }

            #[test]
            fn missing_returns_none() { assert!(fetch("missing").is_ok()); }

            #[test]
            fn truncates_before_writing() {
                let result = write_text(large_text());
                assert!(result.is_ok(), "writing failed: {:?}", result.err());
            }

            #[test]
            fn outcome_logged_after_check() {
                let actual = convert(fixture());
                assert_eq!(actual, "converted");
                eprintln!("{:?}", actual);
            }

            #[test]
            fn missing_returns_none_checked() { assert!(fetch("missing").unwrap().is_none()); }

            #[test]
            fn convert_and_observe() {
                let mut input = fixture();
                convert(&mut input);
                assert_eq!(input, "converted");
            }

            #[test]
            fn check_then_clean_up() {
                assert_eq!(convert(fixture()), "converted");
                cleanup();
            }
            """, "no-assert", ("fn setup_only", "fn discard", "fn print_only",
                               "fn missing_returns_none()", "fn truncates_before_writing"), "checks.rs")

    def test_rust_method_and_macro_evidence(self):
        # WHY: a waiter fails on timeout and a macro template is not a handwritten test.
        # catches: crediting a same-name method on the wrong type or linting a template body.
        self.check_lint("""
            struct Harness;
            struct Recorder;
            impl Harness {
                fn until(&self, poll: impl Fn() -> bool) {
                    if !poll() { panic!("timeout"); }
                }
                fn finishes(&self, poll: impl Fn() -> bool) { self.until(poll); }
                fn waits(&self) { channel().recv_timeout(delay()).expect("timed out"); }
            }
            impl Recorder {
                fn until(&self, poll: impl Fn() -> bool) { println!("poll"); }
                fn waits(&self) { self.channel.recv_timeout(delay()).ok(); }
            }

            #[test]
            fn clap_schema() { external::schema().debug_assert(); }

            #[test]
            fn method_check() { let h = Harness::new(); h.finishes(poll); }

            #[test]
            fn timeout_check() { let h: Harness = fixture(); h.waits(); }

            #[test]
            fn method_report() { let r = Recorder::new(); r.until(poll); }

            #[test]
            fn timeout_ignored() { let r: Recorder = fixture(); r.waits(); }

            macro_rules! adapter {
                () => {
                    #[test]
                    fn export_hook() { export_if_requested(); }
                };
            }

            #[test]
            fn ordinary_test() { export_if_requested(); }
            """, "no-assert", ("fn method_report", "fn timeout_ignored", "fn ordinary_test"), "checks.rs")

    def test_rust_imported_waiter(self):
        # WHY: a waiter can be defined in a different module of the same crate.
        # catches: checking by method spelling or ignoring an explicitly imported type.
        self.write("lib.rs", "mod support; mod cases;")
        self.write("support.rs", """
            pub struct Harness;
            impl Harness {
                fn check_condition(&self, ready: bool) { assert!(ready); }
                pub fn wait_for(&self, ready: bool) { self.check_condition(ready); }
            }
        """)
        self.check_lint("""
            use crate::support::Harness;

            #[test]
            fn polls_until_ready() {
                let h: Harness = fixture();
                h.wait_for(ready());
            }

            #[test]
            fn same_method_on_another_type() {
                let r = Recorder::new();
                r.wait_for(ready());
            }
            """, "no-assert", ("fn same_method_on_another_type",), "cases.rs")

    def test_python_absence_controls(self):
        # WHY: an empty producer can satisfy every negative case even when broken.
        # catches: accepting a positive control for a different producer or ignoring a mock write.
        self.check_lint("""
            def scan(path) -> list:
                return process(path)

            def report(path) -> list:
                return process(path)

            def test_no_scan_entries():
                assert scan("missing") == []  # empty producer

            def test_no_reports():
                output = report("missing")
                assert not output

            def test_report_can_find_one():
                assert report("known") == ["warning"]

            def test_forward_flag():
                import unittest.mock
                forwarded = unittest.mock.Mock()
                assert reject(forwarded)  # unchecked mock

            def test_forward_flag_checked():
                import unittest.mock
                forwarded = unittest.mock.Mock()
                assert reject(forwarded)
                forwarded.assert_not_called()
            """, "absence-no-control", ("# empty producer", "# unchecked mock"))

    def test_rust_absence_controls(self):
        # WHY: a detector that always returns an empty Vec passes an absence-only test.
        # catches: crediting a sibling control for a different producer or missing a loop detector.
        self.check_lint("""
            fn scan(path: &str) -> Vec<Warning> { process(path) }
            fn report(path: &str) -> Vec<Warning> { process(path) }

            #[test]
            fn no_scan_entries() { assert!(scan("missing").is_empty()); } // flag direct

            #[test]
            fn no_reports() { assert!(report("missing").is_empty()); }

            #[test]
            fn report_finds_one() { assert_eq!(report("known").len(), 1); }

            fn index(path: &str) -> Vec<Warning> { process(path) }

            #[test]
            fn index_may_be_empty() { assert!(index("missing").is_empty()); }

            #[test]
            fn projected_index_contains_a_warning() {
                let names: Vec<_> = index("known").iter().map(|warning| warning.name).collect();
                assert!(names.contains(&"warning"));
            }

            fn warning_names(rows: &[Warning]) -> Vec<&str> {
                rows.iter().map(|warning| warning.name).collect()
            }

            #[test]
            fn empty_warning_names() {
                assert!(warning_names(&index("missing")).is_empty());
            }

            fn assert_warning_names() {
                assert_eq!(warning_names(&index("known")), ["warning"]);
            }

            #[test]
            fn known_warning_names() { assert_warning_names(); }

            #[test]
            fn no_collected_warnings() {
                let mut failures = Vec::new();
                for warning in scan("missing") { failures.push(warning); }
                assert!(failures.is_empty()); // flag collector
            }

            #[test]
            fn counts_reports() {
                let called = Cell::new(0);
                accept(|| called.set(called.get() + 1));
                assert_eq!(called.get(), 1);
            }
            """, "absence-no-control", ("// flag direct", "// flag collector"), "checks.rs")

    def test_python_same_operands(self):
        # WHY: two calls with one input do not compare the two promised inputs.
        # catches: accepting a stability cue or missing a field alias or assertEqual typo.
        self.check_lint("""
            def test_two_inputs_have_the_same_key(self):
                self.assertEqual(key("left"), key("left"))  # flag here
                self.assertEqual(key("left"), "known-key")

            def test_borrowed_field(self):
                actual = read()
                alias = actual
                self.assertEqual(alias.value, actual.value)  # flag here
                self.assertEqual(alias.value, actual.label)

            def test_expected_typo(self):
                actual = read()
                expected = "label"
                self.assertEqual(actual, actual)  # flag here
                self.assertEqual(actual, expected)

            def test_repeatable(self):
                self.assertEqual(key("left"), key("left"))
            """, "self-compare", ("key(\"left\"))  # flag here", "actual.value)  # flag here",
                                  "actual, actual)  # flag here"))

    def test_rust_same_operands(self):
        # WHY: a shared borrow preserves identity and does not provide an independent expected value.
        # catches: ignoring references, missing an explicit two-input claim, or collapsing mutated clones.
        self.check_lint("""
            #[test]
            fn clone_eq() {
                let actual = read();
                let copy = actual.clone();
                assert_eq!(&actual, &copy); // flag clone
                assert_eq!(&actual, &actual.clone()); // flag inline
                let alias = &actual;
                assert_eq!(alias.name, actual.name); // flag field
            }

            #[test]
            fn changed_clone() {
                let actual = read();
                let mut copy = actual.clone();
                copy.name = "different";
                assert_ne!(&actual, &copy);
            }

            #[test]
            fn same_key() {
                // Both inputs map to the same key.
                assert_eq!(key("left"), key("left")); // flag call
                assert_eq!(key("left"), "known-key");
            }

            #[test]
            fn repeatable() {
                assert_eq!(key("left"), key("left"));
            }
            """, "self-compare", ("// flag clone", "// flag inline", "// flag field", "// flag call"), "checks.rs")
