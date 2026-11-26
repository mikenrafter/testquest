"""Fix round 1 for the tq CLI: parse errors, Rust test shapes, advisory rules, baseline guard."""
import json
import re

import test_tq as contract


class ParseErrors(contract.TqCase):
    def test_literal_suffix_and_unparsable_files_do_not_stop_the_run(self):
        # WHY: one bad file must not hide the real finding beside it, and Rust suffixes with an underscore are valid.
        # catches: int() on "1_" aborting the run, or a per-file parse error ending the whole scan with exit 2.
        self.write("a_good.rs", """
            #[test]
            fn suffixes() {
                assert_eq!(1_u8, 1_u8);
                assert_eq!(0xFF_u8, 255);
            }
            """)
        self.write("b_syntax.py", "def test_x(:\n    pass\n")
        self.write("c_unclosed.rs", "#[test]\nfn broken() {\n    assert!(x);\n")
        (self.repo / "d_bytes.py").write_bytes(b"# coding: utf-8\nx = '\xff\xfe'\n")
        (self.repo / "e_bytes.rs").write_bytes(b"fn \xff\xfe() {}\n")
        self.write("f_real_test.py", "def test_real():\n    assert True\n")
        rows = json.loads(self.tq("lint", ".", "--json", code=(1,)))
        pairs = sorted((contract.Path(row["path"]).name, row["rule"]) for row in rows)
        self.assertEqual(pairs, [
            ("a_good.rs", "const-truth"), ("a_good.rs", "const-truth"), ("a_good.rs", "self-compare"),
            ("b_syntax.py", "parse-error"), ("c_unclosed.rs", "parse-error"),
            ("d_bytes.py", "parse-error"), ("e_bytes.rs", "parse-error"),
            ("f_real_test.py", "const-truth"),
        ])
        for row in rows:
            if row["rule"] == "parse-error":
                self.assertTrue(row["message"].strip(), row)


class RustTestShapes(contract.TqCase):
    def no_assert_names(self, source):
        path = self.write("shapes.rs", source)
        lines = path.read_text().splitlines()
        rows = self.findings("lint", path.name)
        return [lines[line - 1].strip() for _, line, rule, _ in rows if rule == "no-assert"]

    def test_attribute_paths_ending_in_test_are_tests(self):
        # WHY: async runtimes spell test attributes with arguments, and silent skips hide every rule.
        # catches: matching only the exact shapes #[test] and #[tokio::test].
        found = self.no_assert_names("""
            #[tokio::test(flavor = "multi_thread")]
            async fn tokio_args() { let x = 1; }
            #[async_std::test]
            async fn async_std_path() { let x = 1; }
            #[rstest]
            fn fixture_case() { let x = 1; }
            #[test_case(1, 2)]
            fn table_case() { let x = 1; }
            """)
        self.assertEqual(found, [
            "async fn tokio_args() { let x = 1; }",
            "async fn async_std_path() { let x = 1; }",
            "fn fixture_case() { let x = 1; }",
            "fn table_case() { let x = 1; }",
        ])

    def test_lookalike_attributes_are_not_tests(self):
        # WHY: a test module and a helper attribute name are not test functions.
        # catches: matching any attribute that contains or starts with the word test.
        self.assertEqual(self.no_assert_names("""
            #[cfg(test)]
            mod tests {
                #[testing_helper]
                fn helper() { let x = 1; }
                #[cfg(test)]
                fn gated() { let x = 1; }
            }
            """), [])

    def test_local_checking_helpers_count_but_empty_ones_do_not(self):
        # WHY: a helper that asserts is the check; a helper that asserts nothing is not.
        # catches: counting only helpers named assert/check/expect, or counting every local helper call.
        found = self.no_assert_names("""
            fn round_trip<T: Copy>(value: T) { assert_eq!(decode(encode(value)), 7); }
            fn outer(value: u8) { round_trip(value); }
            fn lazy(value: u8) { let _ = value; }
            mod inner {
                fn nested_check(value: u8) { assert!(value > 2); }
                #[test]
                fn uses_nested() { nested_check(3); }
            }
            #[test]
            fn direct() { round_trip(3); }
            #[test]
            fn chained() { outer(3); }
            #[test]
            fn delegates_to_nothing() { lazy(3); }
            """)
        self.assertEqual(found, ["fn delegates_to_nothing() { lazy(3); }"])


class AdvisoryRules(contract.TqCase):
    ADVISORY = {"nondistinguishing-literal", "self-derived-expected"}

    def test_equal_falsy_positionals_fire_but_keyword_pairs_do_not(self):
        # WHY: equal zeros hide a swap of interchangeable parameters; named arguments fix which is which.
        # catches: skipping every falsy duplicate, or still flagging duplicate keyword arguments.
        self.check_lint("""
            from m import f, g

            def test_positional():
                assert f(0, 0) == 3  # positional pair

            def test_keywords():
                assert g(keep=(), no_defer=()) == 3
            """, "nondistinguishing-literal", ("# positional pair",))

    def test_rules_list_the_advisory_level(self):
        # WHY: users must know which rules block and which only advise.
        # catches: listing every rule as gating, or marking a gating rule advisory.
        output = self.tq("rules")
        starts = list(re.finditer(rf"(?m)^({'|'.join(sorted(contract.RULES))})\b", output))
        ends = [start.start() for start in starts[1:]] + [len(output)]
        levels = {start[1]: "level: advisory" in output[start.start():end]
                  for start, end in zip(starts, ends)}
        self.assertEqual({rule for rule, advisory in levels.items() if advisory}, self.ADVISORY)

    def test_advisory_findings_show_but_only_gating_findings_fail(self):
        # WHY: an advisory-only file must not break a build, and a gating finding still must.
        # catches: exit 1 for advisory findings, hiding them, or letting them mask a gating exit.
        self.write("advice_test.py", """
            from m import f

            def test_advice():
                assert f(0, 0) == 3
            """)
        output = self.tq("lint", "advice_test.py")
        self.assertIn("nondistinguishing-literal", output)
        self.assertIn("advisory", output)
        rows = json.loads(self.tq("lint", "advice_test.py", "--json"))
        self.assertEqual([(row["rule"], row["level"]) for row in rows],
                         [("nondistinguishing-literal", "advisory")])
        self.write("advice_test.py", """
            from m import f

            def test_advice():
                assert f(0, 0) == 3
                assert True
            """)
        rows = json.loads(self.tq("lint", "advice_test.py", "--json", code=(1,)))
        self.assertEqual(sorted((row["rule"], row["level"]) for row in rows),
                         [("const-truth", "error"), ("nondistinguishing-literal", "advisory")])

    def test_self_derived_expected_is_advisory_too(self):
        # WHY: dogfood hits were mostly repeatability checks the rule itself allows.
        # catches: marking only one of the two advisory rules.
        self.write("derived_test.py", """
            import codec

            def test_same_function():
                actual = codec.encode(1)
                expected = codec.encode(2)
                assert actual == expected
            """)
        rows = json.loads(self.tq("lint", "derived_test.py", "--json"))
        self.assertEqual([(row["rule"], row["level"]) for row in rows],
                         [("self-derived-expected", "advisory")])


class BaselineGuard(contract.TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")

    def replay(self, *, code):
        return contract.CatchesTests.replay(self, code=code)

    def check_replay(self, rows, description, status):
        contract.CatchesTests.check_replay(self, rows, description, status)

    def test_failing_baseline_is_an_error_not_a_caught_fault(self):
        # WHY: a suite already red at HEAD would make every fault look caught.
        # catches: running the mutant without proving the unpatched copy passes first.
        label = contract.CatchesTests.fixture_with_fault(self)
        self.write("test_payload.py", """
            import unittest
            from payload import encode

            class Payload(unittest.TestCase):
                def test_value(self):
                    # catches: drop the high word [patch: byte.patch]
                    self.assertEqual(encode(), 0x4321)
            """)
        self.commit("Make the selected test fail before the patch")
        rows = self.replay(code=(2,))
        self.check_replay(rows, label, "error")
        self.assertIn("Baseline did not pass", rows[0]["detail"])

