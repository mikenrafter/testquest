"""CLI checks for which test evidence satisfies no-assert."""

from test_tq import TqCase


class NoAssertRoundOne(TqCase):
    def test_rust_ignored_tests(self):
        # WHY: ignored diagnostics may deliberately only print.
        # catches: treating both forms of ignore as ordinary tests.
        self.check_lint("""
            #[test]
            #[ignore = "timing"]
            fn report_timing() { println!("{}", time_operation()); }

            #[ignore]
            #[test]
            fn dump_table() { write_table(); }

            #[test]
            fn print_result() { println!("{}", compute()); }
            """, "no-assert", ("fn print_result",), "checks.rs")

    def test_rust_rstest_templates(self):
        # WHY: a reusable case list has no test body to check.
        # catches: accepting #[rstest] before looking for #[template].
        self.check_lint("""
            #[template]
            #[rstest]
            #[case(6)]
            fn common_cases(#[case] input: u32) {}

            #[rstest]
            #[rstest_reuse::template]
            #[case(7)]
            fn other_cases(#[case] input: u32) {}

            #[rstest]
            #[case(9)]
            fn reports_case(#[case] input: u32) { compute(input); }
            """, "no-assert", ("fn reports_case",), "checks.rs")

    def test_rust_trait_checks_require_call_and_bound(self):
        # WHY: the compiler verifies the trait bound when the helper is called.
        # catches: dropping turbofish calls or giving an unused declaration credit.
        self.check_lint("""
            #[test]
            fn error_is_send() {
                fn assert_send<T: Send + Sync>() {}
                assert_send::<Box<dyn Worker>>();
            }

            #[test]
            fn error_is_sync() {
                fn check_sync<T>() where T: Sync {}
                check_sync::<Worker>();
            }

            #[test]
            fn unused_bound() {
                fn assert_send<T: Send>() {}
                compute();
            }

            #[test]
            fn unbounded_generic() {
                fn check_type<T>() {}
                check_type::<Worker>();
            }
            #[test]
            fn const_generic() {
                fn check_index<const N: usize>() {}
                check_index::<3>();
            }
            """, "no-assert", ("fn unused_bound", "fn unbounded_generic", "fn const_generic"),
                        "checks.rs")

    def test_rust_function_pointer_type_checks(self):
        # WHY: assigning to a function pointer proves the input and output types.
        # catches: counting every annotated discard as a function signature check.
        self.check_lint("""
            #[test]
            fn conversion_signature() {
                let _: fn(clock::Tick) -> clock::Frame = clock::to_frame;
            }

            #[test]
            fn typed_value_discard() {
                let _: clock::Frame = compute();
            }
            """, "no-assert", ("fn typed_value_discard",), "checks.rs")

    def test_rust_macro_expansions(self):
        # WHY: the invoked expansion provides the check.
        # catches: ignoring local macro bodies or crediting a macro by name or matcher.
        self.check_lint("""
            macro_rules! ensure_value { ($x:expr) => { assert_eq!($x, 3) }; }
            macro_rules! assert_report { ($x:expr) => { println!("{}", $x) }; }
            macro_rules! takes_assertion { (assert!($x:expr)) => { stringify!($x) }; }

            #[test]
            fn macro_verifies() { ensure_value!(compute()); }

            #[test]
            fn macro_reports() { assert_report!(compute()); }

            #[test]
            fn macro_quotes_assertion() { takes_assertion!(assert!(compute())); }
            """, "no-assert", ("fn macro_reports", "fn macro_quotes_assertion"), "checks.rs")

    def test_rust_macro_calls_checking_helper(self):
        # WHY: macro expansions can delegate to ordinary functions.
        # catches: stopping at the macro or treating a delegated call as a check by name.
        self.check_lint("""
            mod tests {
                fn compare_value(value: i32) { assert_eq!(value, 3); }
                fn check_empty(value: i32) { println!("{}", value); }
                macro_rules! one_case { ($x:expr) => { compare_value($x) }; }
                macro_rules! all_cases { () => { one_case!(compute()) }; }
                macro_rules! report_case { () => { check_empty(compute()) }; }

                #[test]
                fn cases_verify() { all_cases!(); }

                #[test]
                fn cases_report() { report_case!(); }
            }
            """, "no-assert", ("fn cases_report",), "checks.rs")

    def test_rust_validator_extraction(self):
        # WHY: extracting a validator's result makes its rejection falsifiable.
        # catches: crediting arbitrary unwraps, bare validation, or unrelated expect calls.
        self.check_lint("""
            #[test]
            fn validates_config() { config.validate().unwrap(); }

            #[test]
            fn verifies_config() { verify(config).expect("valid config"); }

            #[test]
            fn checks_config() { config.check_consistency().unwrap(); }

            #[test]
            fn drops_parse_output() { parse(input).unwrap(); }

            #[test]
            fn drops_validation_result() { config.validate(); }

            #[test]
            fn extracts_unrelated_result() { config.validate(); fixture.unwrap(); }
            """, "no-assert", ("fn drops_parse_output", "fn drops_validation_result",
                               "fn extracts_unrelated_result"), "checks.rs")

    def test_python_module_helpers(self):
        # WHY: local helper calls need executable evidence, including through delegation.
        # catches: accepting names, unchecked comparison returns, or recursive cycles.
        self.check_lint("""
            from elsewhere import check_external

            def check_nothing(value):
                return value

            def check_comparison(value):
                return value == 3

            def relay_nothing(value):
                check_nothing(value)

            def check_cycle(value):
                relay_cycle(value)

            def relay_cycle(value):
                check_cycle(value)

            def verify_exactly(value):
                if value != 3:
                    raise AssertionError(value)

            def relay_check(value):
                verify_exactly(value)

            def test_no_evidence():
                check_nothing(read())
                check_comparison(read())

            def test_delegates_to_noop():
                relay_nothing(read())

            def test_cycle():
                check_cycle(read())

            def test_relay_checks():
                relay_check(read())

            def test_comparison_is_asserted():
                assert check_comparison(read())

            def test_external_check():
                check_external(read())
            """, "no-assert", ("def test_no_evidence", "def test_delegates_to_noop",
                               "def test_cycle"))

    def test_python_method_and_nested_helpers(self):
        # WHY: a same-file method or nested definition overrides a checking name.
        # catches: using self.assert_* by name or counting a nested helper before it is called.
        self.check_lint("""
            class TestMethods:
                def assert_report(self, value):
                    print(value)

                def exact_value(self, value):
                    assert value == 3

                def test_method_reports(self):
                    self.assert_report(read())

                def test_method_checks(self):
                    self.exact_value(read())

                def test_nested_noop(self):
                    def check_value(value):
                        return value
                    check_value(read())

                def test_nested_checks(self):
                    def exact_value(value):
                        assert value == 3
                    exact_value(read())

                def test_nested_unused(self):
                    def exact_value(value):
                        assert value == 3
                    read()
            """, "no-assert", ("def test_method_reports", "def test_nested_noop",
                               "def test_nested_unused"))

    def test_rust_same_file_helpers(self):
        # WHY: calls to visible helper definitions require evidence in their bodies.
        # catches: crediting printed output, unused nested assertions, or a delegated noop.
        self.check_lint("""
            fn check_report(value: i32) { println!("{}", value); }
            fn relay_report(value: i32) { check_report(value); }
            mod tests {
                use super::{check_report as external_name, relay_report};

                fn verify_exactly(value: i32) { if value != 3 { panic!("bad value"); } }
                fn relay_check(value: i32) { verify_exactly(value); }

                #[test]
                fn name_only() { super::check_report(compute()); }

                #[test]
                fn alias_only() { external_name(compute()); }

                #[test]
                fn delegates_without_check() { relay_report(compute()); }

                #[test]
                fn delegates_to_check() { relay_check(compute()); }

                #[test]
                fn nested_helper_unused() {
                    fn compare(value: i32) { assert_eq!(value, 3); }
                    compute();
                }

                #[test]
                fn nested_helper_used() {
                    fn compare(value: i32) { assert_eq!(value, 3); }
                    compare(compute());
                }

                #[test]
                fn external_check() { other_crate::check_result(compute()); }
            }
            """, "no-assert", ("fn name_only", "fn alias_only", "fn delegates_without_check",
                               "fn nested_helper_unused"), "checks.rs")
