"""Spec-gate and white-box checks for the tq CLI."""
import json
import shlex

import test_tq as contract


class LintRefinements(contract.TqCase):
    def check_sources(self, python, rust, expected=()):
        self.write("cases.py", python)
        self.write("cases.rs", rust)
        rows = self.findings("lint", "cases.py", "cases.rs")
        self.assertEqual([row[:3] for row in rows], list(expected))

    def test_absence_on_values_and_fixture_inputs(self):
        # WHY: the parser return value is the oracle subject; the list supplies input.
        # catches: treating any empty result or any list populated by append as a recorder.
        self.check_sources("""
            def test_empty():
                result = parse("")
                ok = validate("bad")
                assert result == []
                assert not ok
                items = []
                items.append(13)
                assert total(items) == 13
            """, """
            #[test]
            fn test_empty() {
                let parts = split("");
                assert_eq!(parts.len(), 0);
                let mut input = Vec::new();
                input.push(13);
                assert_eq!(total(&input), 13);
            }
            """)

    def test_code_resumes_after_nested_comment(self):
        # WHY: the fake closing brace and check are outside the inner comment.
        # catches: ending a block comment at its first closing delimiter.
        self.check_lint("""
            #[test]
            fn nested() {
                /* outer /* inner */ assert!(true); } */
                assert!(true); // real check
            }
            """, "const-truth", ("// real check",), "nested.rs")

    def test_rust_quote_states_keep_following_code_visible(self):
        # WHY: a real assertion after each delimiter tests where the lexer resumes.
        # catches: treating char quotes, escaped backslashes, hashless raw strings, or lifetimes as string delimiters.
        self.check_sources("", r'''
            #[test]
            fn literals() {
                let quote = '"'; assert!(true); let byte_quote = b'"';
                let slashes = "\\"; assert!(true); let empty = "";
                let raw = r"\"; assert!(true); let decoy = r"assert!(true)";
                let left: &'static str = "x"; assert!(true); let right: &'static str = "y";
            }
            ''', [("cases.rs", line, "const-truth") for line in range(3, 7)])

    def test_literal_size_name_and_type_gates(self):
        # WHY: trivial sequences and non-ordering operations have independent checks.
        # catches: reporting every palindrome or conflating literal 0 and False.
        self.check_sources("""
            def test_inputs():
                assert reverse("") == ""
                assert encode(b"x") == b"x"
                assert rotate([3]) == [3]
                assert parse("level") == 1
                assert retry(0, False) is None
            """, """
            #[test]
            fn inputs() {
                assert_eq!(reverse(""), "");
                assert_eq!(encode(b"x"), b"x");
                assert_eq!(rotate([3]), [3]);
                assert_eq!(parse("level"), 1);
            }
            """)

    def test_exhaustive_branches_and_expected_failure(self):
        # WHY: each variant is checked, including a Rust wrong variant that fails the test.
        # catches: treating exhaustive conditional checks like one-sided guards.
        self.check_sources("""
            def test_both():
                if flag:
                    assert x == 1
                else:
                    assert x == 2

            def test_unittest(self):
                self.assertIsNotNone(msg)
                if msg:
                    assert msg.ok
            """, """
            #[test]
            fn exhaustive() {
                match out {
                    Ok(v) => assert_eq!(v, 7),
                    Err(e) => panic!("{e}"),
                }
            }
            """)

    def test_async_test_and_nested_helpers(self):
        # WHY: an async test needs an oracle and its sync helpers are fixture code.
        # catches: inspecting every sync function while overlooking async test entry points.
        self.check_sources("""
            def make_fixture():
                return 1

            class TestCases:
                def setUp(self):
                    self.item = make_fixture()

                def test_record(self):
                    calls = []
                    def on_message(message):
                        calls.append(message)
                    run(on_message)
                    assert calls == [self.item]

                async def test_async_bare(self):
                    await run()
            """, "", [("cases.py", 15, "no-assert")])

    def test_python_suppressions_use_matching_comment(self):
        # WHY: suppression is consumed only by the named rule at its source line.
        # catches: suppressing all findings on a line or reading directives inside strings.
        self.check_sources('''
            def test_directives():
                assert True  # tq: allow self-compare a different rule
                assert 1 == 1  # tq: allow const-truth only this rule
                assert True, "# tq: allow const-truth this is a string"
                assert True  # tq: allow const-truth only this assertion
                assert True
            ''', "", [("cases.py", line, rule) for line, rule in
                      [(2, "const-truth"), (3, "self-compare"), (4, "const-truth"), (6, "const-truth")]])

    def test_provenance_follows_imports_and_excludes_independent_measures(self):
        # WHY: imported aliases couple production oracles; independent len and date calls measure output.
        # catches: matching only dotted call sites or reporting neutral observation functions as circular.
        self.check_sources("""
            from codec import encode as forward, expected_bytes
            import datetime

            def test_coupled():
                assert forward(data) == expected_bytes(data)

            def test_independent():
                assert len(a) == len(b)
                assert parse_day("2026-01-02") == datetime.date(2026, 1, 2)
            """, """
            use crate::codec::{encode as forward, expected_bytes};
            #[test]
            fn coupled() {
                assert_eq!(forward(data), expected_bytes(data));
            }
            """, [("cases.py", 5, "self-derived-expected"), ("cases.rs", 4, "self-derived-expected")])

    def test_constant_false_and_partial_truth(self):
        # WHY: these predicates can fail and do not trigger the forced-true rule.
        # catches: flagging any literal predicate, including false or True inside a conjunction.
        self.check_sources("""
            def test_predicates():
                if bad:
                    assert False, "unreachable"
                assert 0
                assert ""
                assert 1 == 2
                assert ready and True
                assert [] != ()
            """, """
            #[test]
            fn predicates() {
                assert!(false);
                assert_eq!(1, 2);
                assert!(ready && true);
            }
            """, [("cases.py", 8, "const-truth")])


class WitnessRefinements(contract.TqCase):
    def test_only_distinct_scanned_test_is_a_witness(self):
        # WHY: setup branching is harmless and a Rust witness is a real test on another path.
        # catches: flagging every if, accepting helper/self witnesses, or ignoring Rust witness comments.
        self.write("cases.py", """
            def test_setup_if():
                x = 1 if flag else 2
                assert compute(x) == 3

            def test_if_then_assert():
                if flag:
                    setup()
                assert ready()

            def test_reaches_helper():
                # reaches: helper_only
                if x:
                    assert x.ok

            def test_self():
                # reaches: test_self
                if x:
                    assert x.ok

            def helper_only():
                pass
        """)
        self.write("cases.rs", """
            #[test]
            fn property() {
                // reaches: example
                if let Some(v) = x { assert_eq!(v, 1); }
            }

            #[test]
            fn example() { assert!(x.is_some()); }
        """)
        rows = self.findings("witness", "cases.py", "cases.rs")
        self.assertEqual([row[:3] for row in rows], [
            ("cases.py", 10, "missing-witness"), ("cases.py", 15, "missing-witness"),
        ])


class ReplayRefinements(contract.TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.temporary_copies = self.scratch / "replay-copies"
        self.temporary_copies.mkdir()

    def replay(self, *, command=None, extra=(), code):
        self.replay_env["TMPDIR"] = str(self.temporary_copies)
        return contract.CatchesTests.replay(self, command=command, extra=extra, code=code)

    def test_scratch_is_removed_on_every_early_exit(self):
        # WHY: failure to apply or run a replay must still remove both copies.
        # catches: early returns before scratch cleanup on patch failure, launch error, or dirty replay.
        contract.CatchesTests.fixture_with_fault(self, dirty=True)
        missing = shlex.quote(str(self.scratch / "command-does-not-exist"))
        for command, extra, code, status in (
                (None, (), (2,), "patch-failed"),
                (missing, ("--worktree",), (2,), "error"),
                (None, ("--worktree",), (0,), "red")):
            with self.subTest(status=status):
                self.assertEqual([row["status"] for row in self.replay(
                    command=command, extra=extra, code=code)], [status])
                self.assertEqual(list(self.temporary_copies.iterdir()), [])

    def test_faults_use_fresh_baselines_and_copies(self):
        # WHY: both patches change the same line, and only the second fault should survive.
        # catches: applying multiple faults cumulatively or inheriting test-created files from another replay.
        contract.CatchesTests.fixture_with_fault(self)
        self.write("other.patch", (self.repo / "byte.patch").read_text().replace("0x0034", "0x0234"))
        self.write("test_survives.py", """
            import unittest
            from pathlib import Path
            from payload import encode

            class Second(unittest.TestCase):
                def test_low_byte(self):
                    # catches: replace the high byte [patch: other.patch]
                    self.assertFalse(Path("test-side-effect").exists())
                    self.assertEqual(encode() & 0xff, 0x34)
        """)
        self.commit("Add independent fault against the same line")
        rows = self.replay(code=(1,))
        self.assertEqual([(row["description"], row["status"]) for row in rows], [
            ("drop the high word", "red"), ("replace the high byte", "green"),
        ])


class RiskRefinements(contract.TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")

    def snapshot(self, *options):
        return contract.RiskTests.snapshot(self, *options)

    def test_risk_rank_responds_to_imports_churn_and_test_references(self):
        # WHY: equal fix counts expose whether the remaining measures change rank and gaps.
        # catches: sorting by fixes alone or ignoring evidence of test references on a tie.
        for part in ("core", "leaf", "churned", "unchanged", "a_tested", "z_untested"):
            self.write(f"{part}.py", "VALUE = 13\n")
        for number in range(4):
            self.write(f"consumer{number}.py", "import core\n")
        self.write("tests/test_exists.py", "import leaf\nimport a_tested\n")
        self.write("tests/test_still_exists.py", "import leaf\n")
        self.commit("Create example modules")
        self.write("churned.py", "VALUE = 27\n")
        self.commit("Revise the value")
        rows = {row["path"]: row for row in self.snapshot()["files"]}
        for higher, lower in (("core", "leaf"), ("churned", "unchanged"), ("z_untested", "a_tested")):
            self.assertGreater(rows[f"{higher}.py"]["score"], rows[f"{lower}.py"]["score"])
        self.assertTrue(rows["core.py"]["gap"])
        self.assertFalse(rows["leaf.py"]["gap"])

    def test_int_budget_rounding_funds_multiple_areas(self):
        # WHY: nearby risks and a budget below the top-three floor expose different rounding bugs.
        # catches: giving every extra slot to the first row, or losing and overallocating rounded slots.
        for name in ("first", "second", "third", "fourth", "fifth"):
            self.write(f"{name}.py", "VALUE = 0\n")
        self.commit("Create five example modules")
        for rank, changes in (("first", 6), ("second", 5), ("third", 4), ("fourth", 1)):
            for number in range(changes):
                self.write(f"{rank}.py", f"VALUE = {number + 1}\n")
                self.commit(f"Fix {rank} revision {number}")
        for budget in (12, 10, 2):
            with self.subTest(budget=budget):
                rows = self.snapshot("--budget", str(budget))["files"]
                counts = [row["tests"] for row in rows]
                self.assertEqual(sum(counts), budget)
                if budget == 2:
                    self.assertEqual(counts, [1, 1, 0, 0, 0])
                else:
                    self.assertGreaterEqual(counts[1], 2)
                    self.assertGreaterEqual(counts[2], 1)


class PropsRefinements(contract.TqCase):
    def test_search_returns_independent_cobs_answer_and_crc_faults(self):
        # WHY: retrieving by fault and boundary should surface distinguishing evidence.
        # catches: searching only variants or returning a circular or misframed COBS oracle.
        cobs = json.loads(self.tq("props", "--search", "interior zero", "--json"))
        interior = next(row for row in cobs if row["id"] == "cobs-interior-zero")
        self.assertEqual(bytes.fromhex(interior["input_strategy"]["raw_hex"]), b"\x11\x00\x29")
        # Table I: code 02 precedes each one-byte run; the implicit terminal zero is discarded.
        self.assertEqual(bytes.fromhex(interior["oracle"]["encoded_hex"]), b"\x02\x11\x02\x29")
        self.assertIn("COBSforToN", interior["evidence"][0]["source"])
        answers = json.loads(self.tq("props", "--search", "final xor", "--json"))
        known = next(row for row in answers if row["id"] == "crc-iso-hdlc-check")
        faults = " ".join(known["known_wrong_implementations"])
        self.assertIn("0x01DD", faults)
        self.assertIn("0x340BC6D9", faults)
