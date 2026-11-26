"""Black-box tests for the tq CLI. Lint and witness source is parsed only."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tq"
RULES = {
    "absence-no-control",
    "const-truth",
    "guarded-assert",
    "invalid-suppression",
    "missing-witness",
    "no-assert",
    "nondistinguishing-literal",
    "parse-error",
    "self-compare",
    "self-derived-expected",
}


class TqCase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.repo = self.scratch / "repo"
        self.repo.mkdir()

    def write(self, name, source):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return path

    def copy(self, source, name=None):
        path = self.repo / (name or source.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        return path

    def tq(self, *args, code=(0,), env=None):
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "bin/tq"), *map(str, args)],
            cwd=self.scratch if "--repo" in args else self.repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertIn(result.returncode, code, result.stderr or result.stdout)
        if "--json" in args:
            self.assertTrue(result.stdout.strip(), result.stderr)
        return result.stdout

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=Tq Tests", "-c", "user.email=tq@example.test",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout

    def commit(self, subject):
        self.git("add", "-A")
        self.git("commit", "-qm", subject)

    def findings(self, subcommand, *paths):
        rows = json.loads(self.tq(subcommand, *paths, "--json", code=(0, 1)))
        return [
            (Path(row["path"]).name, row["line"], row["rule"], row["message"])
            for row in rows
        ]

    def check_lint(self, source, rule, wanted, name="test_cases.py"):
        path = self.write(name, source)
        self.check_lint_file(path, rule, wanted)

    def check_lint_file(self, path, rule, wanted):
        source = path.read_text(encoding="utf-8").splitlines()
        wanted_lines = [
            number for number, line in enumerate(source, 1)
            if any(marker in line for marker in wanted)
        ]
        self.assertEqual(len(wanted_lines), len(wanted), wanted)
        found = self.findings("lint", path.name)
        self.assertEqual(
            [(name, line) for name, line, found_rule, _ in found if found_rule == rule],
            [(path.name, line) for line in wanted_lines],
            found,
        )


class LintTests(TqCase):
    def test_python_constant_truths(self):
        # WHY: a literal or forced-true operand can hide a broken predicate.
        # catches: recognizing only the boolean literal True
        self.check_lint("""
            def test_expression():
                assert True
                assert 1
                assert ready or True
                assert "ready" == "ready"
                assert ready or False
                assert got == 1
                assert left != right
            """, "const-truth", ("assert True", "assert 1", "or True", '"ready" =='))

    def test_rust_constant_truths(self):
        # WHY: Rust's equality macro can compare two equal literals.
        # catches: checking Rust assert! but ignoring assert_eq!
        self.check_lint("""
            #[test]
            fn numeric_checks() {
                assert!(true);
                assert_eq!(73, 73);
                assert!(score > 12);
                assert_eq!(score, 73);
                assert_ne!(score, 61);
            }
            """, "const-truth", ("assert!(true)", "assert_eq!(73"), "checks.rs")

    def test_python_self_compare(self):
        # WHY: distinct variable names matter even when their accessors match.
        # catches: comparing operand shapes without comparing their bindings
        self.check_lint("""
            def test_result():
                assert result.name == result.name
                assert result.name == expected.name
                assert read_register(0) == read_register(1)
            """, "self-compare", ("result.name == result.name",))

    def test_rust_self_compare_in_nested_macro(self):
        # WHY: commas inside nested delimiters and strings do not split assertion arguments.
        # catches: splitting assert_eq! on the first comma instead of at balanced delimiters
        self.check_lint("""
            #[test]
            fn compare_outputs() {
                assert_eq!(a.len(), a.len());
                assert_eq!(a.len(), b.len());
                assert_eq!(join!(a, [3, 5]), join!(a, [3, 5]), "expected a, b");
                assert_eq!(join!(a, [3, 5]), join!(a, [5, 3]));
            }
            """, "self-compare", ("assert_eq!(a.len(), a.len())", '"expected a, b"'), "checks.rs")

    def test_python_exception_and_helper_checks(self):
        # WHY: raises and assertion helpers can observe outcomes without an assert statement.
        # catches: treating framework exception checks and delegated checks as empty tests
        self.check_lint("""
            def test_bare():
                convert("2")

            def test_raises():
                with pytest.raises(ValueError):
                    convert("bad")

            def test_assert_raises(self):
                with self.assertRaises(OverflowError):
                    scale(500)

            def test_helpers():
                assert_output(convert("2"), 2)

            def test_check():
                check_size("é".encode(), 2)

            def test_expect():
                rules.expect_positive(4)
            """, "no-assert", ("def test_bare",))

    def test_rust_explicit_panic_expectation(self):
        # WHY: should_panic is an assertion, while a similar bare call has no oracle.
        # catches: losing should_panic when it comes after the test attribute
        self.check_lint("""
            #[test]
            fn test_bare() {
                parse("2");
            }

            #[test]
            #[should_panic(expected = "invalid input")]
            fn test_invalid() {
                parse("bad");
            }

            #[tokio::test]
            async fn test_helpers() {
                check_output(parse("2"), 2);
            }
            """, "no-assert", ("fn test_bare",), "checks.rs")

    def test_rust_ignores_assertions_in_literals_and_comments(self):
        # WHY: Rust test bodies contain strings and characters that mimic Rust syntax.
        # catches: recognizing assertion tokens outside actual Rust test code
        path = self.copy(FIXTURES / "rust_tokens.rs")
        found = self.findings("lint", path.name)
        self.assertEqual([(line, rule) for _, line, rule, _ in found], [
            (2, "no-assert"),
            (18, "self-compare"),
        ])

    def test_python_guarded_assertions(self):
        # WHY: a conditional check can be skipped on every run.
        # catches: treating a guarded oracle as unconditional or missing a prior condition check
        self.check_lint("""
            def test_conditional():
                message = parse(raw)
                if message is not None:
                    assert message.topic == "alerts"
                    assert message.priority == 3

            def test_witnessed():
                message = parse(raw)
                assert message is not None
                if message is not None:
                    assert message.priority == 3

            def test_following_assertion():
                if enabled:
                    assert parse(raw).priority == 3
                assert audit.topic == "alerts"
            """, "guarded-assert", ("def test_conditional",))

    def test_rust_if_let_and_match_arms(self):
        # WHY: Rust's if let and match can skip a property while the test still passes.
        # catches: tracking plain if blocks but missing if let and match arms
        self.check_lint("""
            #[test]
            fn test_optional() {
                if let Some(value) = output {
                    assert_eq!(value, 7);
                }
            }

            #[test]
            fn test_branch() {
                match output {
                    Ok(value) => prop_assert_eq!(value, 7),
                    Err(_) => (),
                }
            }

            #[test]
            fn test_output_required() {
                assert!(output.is_some());
                if let Some(value) = output {
                    assert_eq!(value, 7);
                }
            }
            """, "guarded-assert", ("fn test_optional", "fn test_branch"), "checks.rs")

    def test_spy_recorder_without_a_check(self):
        # WHY: the denial test can pass without observing whether its recorder fired.
        # catches: inspecting only negative assertions and missing a write-only recorder
        path = self.copy(FIXTURES / "spy_recorder.py", "test_proxy.py")
        found = [line for _, line, rule, _ in self.findings("lint", path.name)
                 if rule == "absence-no-control"]
        control_line = next(number for number, line in enumerate(path.read_text().splitlines(), 1)
                            if line.startswith("def test_upload_forwarding_control"))
        self.assertEqual(len(found), 1, found)
        self.assertIn(found[0], range(1, control_line))

    def test_python_absence_needs_positive_control_on_same_recorder(self):
        # WHY: an unrelated recorder can fire even if the target is disconnected.
        # catches: accepting any positive list check as control for a negative recorder check
        self.check_lint("""
            def test_wrong_control():
                called = []
                rejected = []
                try_request(called.append, rejected.append)
                assert not called
                assert rejected == ["invalid"]

            def test_controlled():
                called = []
                try_request(called.append, payload="invalid")
                assert called == []
                try_request(called.append, payload="valid")
                assert called == ["accepted"]
            """, "absence-no-control", ("assert not called",))

    def test_rust_recorder_length(self):
        # WHY: zero recorder length proves little until a successful call records something.
        # catches: missing zero-length comparisons or ignoring later positive checks in Rust
        self.check_lint("""
            #[test]
            fn test_unguarded_recorder() {
                let mut calls = Vec::new();
                send(&mut calls, "invalid");
                assert_eq!(calls.len(), 0); // missing control
            }

            #[test]
            fn test_controlled_recorder() {
                let mut calls = Vec::new();
                send(&mut calls, "invalid");
                assert_eq!(calls.len(), 0);
                send(&mut calls, "valid");
                assert_eq!(calls, vec!["accepted"]);
            }
            """, "absence-no-control", ("// missing control",), "checks.rs")

    def test_python_expected_value_provenance(self):
        # WHY: tracking the source of each operand through locals exposes circular evidence.
        # catches: detecting repeated calls only when both appear directly in the assertion
        self.check_lint("""
            import codec
            import reference

            def test_identical_source():
                actual = codec.encode(frame)
                expected = codec.encode(reference_frame)
                assert actual == expected  # same function

            def test_shared_source():
                actual = codec.encode(frame)
                expected = codec.expected_bytes(frame)
                assert actual == expected  # same module

            def test_independent():
                actual = codec.encode(frame)
                expected = reference.encode(reference_frame)
                assert actual == expected

            def test_literal():
                actual = codec.encode(frame)
                assert actual == b"\\x03\\x14"
            """, "self-derived-expected", ("# same function", "# same module"))

    def test_rust_expected_value_provenance(self):
        # WHY: a different module named reference supplies an independent result.
        # catches: treating Rust expected values from the same production module as independent
        self.check_lint("""
            #[test]
            fn test_shared() {
                let actual = codec::encode(input);
                let expected = codec::reference_bytes(input);
                prop_assert_eq!(actual, expected); // coupled oracle
            }

            #[test]
            fn test_independent() {
                let actual = codec::encode(input);
                let expected = reference::encode(input);
                prop_assert_eq!(actual, expected);
            }
            """, "self-derived-expected", ("// coupled oracle",), "checks.rs")

    def test_python_nondistinguishing_literals(self):
        # WHY: symmetry lets a reversed result or swapped argument pass the expected check.
        # catches: missing homogeneous lists or duplicate arguments outside named ordering operations
        self.check_lint("""
            def test_palindrome():
                assert reverse(b"aba") == b"aba"
                assert order("level") == "level"
                assert rotate([3, 5, 3]) == [5, 3, 3]
                assert median([4, 4, 4]) == 4
                assert subtract(6, 6) == 0

            def test_asymmetric():
                assert reverse(b"abc") == b"cba"
                assert order("cba") == "abc"
                assert rotate([3, 5, 7]) == [5, 7, 3]
                assert median([4, 6, 4, 7, 3]) == 4
                assert subtract(7, 3) == 4
            """, "nondistinguishing-literal",
            ('reverse(b"aba")', 'order("level")', "rotate([3, 5, 3])",
             "median([4, 4, 4])", "subtract(6, 6)"))

    def test_rust_nondistinguishing_literals(self):
        # WHY: the same fixture weakness appears in Rust literal inputs to macros.
        # catches: applying the literal symmetry rule only to Python
        self.check_lint("""
            #[test]
            fn test_swaps() {
                assert_eq!(encode(b"aba"), expected);
                assert_eq!(swap([1, 2, 1]), expected);
                assert_eq!(difference(6, 6), 0);
                assert_eq!(encode(b"abc"), expected);
                assert_eq!(swap([1, 2, 3]), expected);
                assert_eq!(difference(6, 2), 4);
            }
            """, "nondistinguishing-literal",
            ('encode(b"aba")', "swap([1, 2, 1])", "difference(6, 6)"), "checks.rs")

    def test_local_suppressions_require_a_reason(self):
        # WHY: a reasoned exception covers one occurrence at its point of use.
        # catches: accepting reasonless suppressions or carrying suppressions into later lines
        path = self.write("test_cases.py", """
            def test_symmetry():
                # tq: allow nondistinguishing-literal WHY: a palindrome must be unchanged
                assert reverse("aba") == "aba"
                assert reverse("ada") == "ada"
                assert value == value  # tq: allow self-compare custom equality is reflexive
                assert data == data
                assert True  # tq: allow const-truth
            """)
        findings = self.findings("lint", path.name)
        self.assertIn((path.name, 4, "nondistinguishing-literal"), [row[:3] for row in findings])
        self.assertIn((path.name, 6, "self-compare"), [row[:3] for row in findings])
        self.assertNotIn(3, [row[1] for row in findings])
        self.assertNotIn(5, [row[1] for row in findings])
        self.assertTrue(any(line == 7 and "reason" in message.lower()
                            for _, line, _, message in findings), findings)

    def test_rust_same_line_suppression(self):
        # WHY: a Rust suppression must come from an actual comment.
        # catches: accepting a fake suppression token inside a Rust string
        self.check_lint("""
            #[test]
            fn fixture() {
                assert_eq!(encode(b"aba"), expected); // tq: allow nondistinguishing-literal palindrome is the claim
                assert_eq!(encode(b"aba"), expected, "// tq: allow nondistinguishing-literal misleading text");
            }
            """, "nondistinguishing-literal", ('expected, "//',), "checks.rs")

    def test_lint_text_and_json_order(self):
        # WHY: two rules on the same line and reverse path inputs exercise every sort key.
        # catches: emitting diagnostics in input or rule-discovery order
        self.write("z_test.py", """
            def test_z():
                assert True
            """)
        self.write("a_test.py", """
            def test_a():
                assert 73 == 73
                assert 1
            """)
        findings = self.findings("lint", "z_test.py", "a_test.py")
        self.assertEqual([row[:3] for row in findings], [
            ("a_test.py", 2, "const-truth"),
            ("a_test.py", 2, "self-compare"),
            ("a_test.py", 3, "const-truth"),
            ("z_test.py", 2, "const-truth"),
        ])
        output = self.tq("lint", "z_test.py", "a_test.py", code=(0, 1))
        self.assertEqual(output.splitlines(), [
            f"{name}:{line} {rule} {message}" for name, line, rule, message in findings
        ])
        for _, _, _, message in findings:
            self.assertTrue(message.strip())

    def test_rules_explain_every_exception(self):
        # WHY: users need to know which legitimate behavior merits a suppression.
        # catches: shipping a rule without allowed-when prose or an actionable message
        output = self.tq("rules")
        starts = list(re.finditer(
            rf"(?m)^\s*({'|'.join(sorted(RULES))})\b[^\n]*", output
        ))
        self.assertCountEqual([start[1] for start in starts], RULES)
        ends = [start.start() for start in starts[1:]] + [len(output)]
        for start, end in zip(starts, ends):
            with self.subTest(rule=start[1]):
                entry = output[start.start():end].strip()
                condition = re.search(r"allowed[ -]when\s*:?\s*(\S[^\n]*)", entry, re.I)
                self.assertIsNotNone(condition, entry)
                self.assertGreaterEqual(len(condition[1].split()), 3)
                description = entry[len(start[1]):condition.start()].strip(" :\n\t")
                self.assertTrue(description, entry)


class CatchesTests(TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")

    def fixture_with_fault(self, *, patch=True, observes=True, dirty=False):
        label = "drop the high word"
        comment = f"# catches: {label}" + (" [patch: byte.patch]" if patch else "")
        expected = "self.assertEqual(encode(), 0x1234)" if observes else "self.assertEqual(encode() & 0xff, 0x34)"
        self.write("payload.py", """
            def encode():
                return 0x1234
        """)
        self.write("test_payload.py", f"""
            import json
            import os
            import unittest
            from pathlib import Path
            from payload import encode

            class Payload(unittest.TestCase):
                def test_value(self):
                    {comment}
                    log = Path(os.environ["TQ_REPLAY_LOG"])
                    with log.open("a") as observations:
                        print(json.dumps({{
                            "cwd": str(Path.cwd()),
                            "value": encode(),
                            "untracked": Path("untracked.txt").exists(),
                        }}), file=observations)
                    Path("test-side-effect").write_text("touched by replay")
                    {expected}
        """)
        context = "    # working tree change\n" if dirty else ""
        header = "@@ -1,3 +1,3 @@" if dirty else "@@ -1,2 +1,2 @@"
        body = (
            "--- a/payload.py\n"
            "+++ b/payload.py\n"
            f"{header}\n"
            " def encode():\n"
            f"{' ' + context if context else ''}"
            "-    return 0x1234\n"
            "+    return 0x0034\n"
        )
        self.write("byte.patch", body)
        self.commit("Add encoder and named fault")
        if dirty:
            self.write("payload.py", f"def encode():\n{context}    return 0x1234\n")
        self.write("untracked.txt", "untracked working file")
        self.replay_log = self.scratch / "observations.jsonl"
        self.replay_env = dict(os.environ, TQ_REPLAY_LOG=str(self.replay_log))
        return label

    def replay(self, *, code=(0,), extra=(), command=None):
        return json.loads(self.tq(
            "catches", "--repo", self.repo, "--json", *extra, "--cmd",
            command or f"{shlex.quote(sys.executable)} -m unittest {{test}}",
            code=code, env=self.replay_env,
        ))

    def check_replay(self, rows, description, status):
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["description"], description)
        self.assertEqual(rows[0]["status"], status)

    def observations(self):
        return [json.loads(line) for line in self.replay_log.read_text().splitlines()]

    def test_replays_killed_fault_at_selected_test(self):
        # WHY: only the annotated test should run so an unrelated failure cannot make the replay red.
        # catches: substituting a file or suite for the qualified annotated test id
        label = self.fixture_with_fault()
        tests = self.repo / "test_payload.py"
        with tests.open("a") as file:
            file.write(textwrap.indent(textwrap.dedent("""
                def test_unrelated(self):
                    log = Path(os.environ["TQ_REPLAY_LOG"])
                    with log.open("a") as observations:
                        print(json.dumps({"value": "unrelated"}), file=observations)
                    self.fail("unrelated test ran")
            """), "    "))
        self.commit("Add unrelated test as command-selection control")
        rows = self.replay()
        self.check_replay(rows, label, "red")
        self.assertEqual(rows[0]["test"], "test_payload.Payload.test_value")
        observed_values = [row["value"] for row in self.observations()]
        self.assertIn(0x34, observed_values)
        self.assertNotIn("unrelated", observed_values)

    def test_replay_mutates_and_cleans_only_scratch_copy(self):
        # WHY: a test command can write files, and a successful mutant can leave changes behind.
        # catches: patching the real checkout, including untracked files in HEAD mode, or leaking the copy
        label = self.fixture_with_fault(observes=False)
        before = self.git("status", "--porcelain=v1")
        source = (self.repo / "payload.py").read_bytes()
        rows = self.replay(code=(1,))
        self.check_replay(rows, label, "green")
        observations = self.observations()
        self.assertIn(0x34, [row["value"] for row in observations])
        for row in observations:
            with self.subTest(cwd=row["cwd"]):
                self.assertNotEqual(Path(row["cwd"]), self.repo)
                self.assertFalse(Path(row["cwd"]).exists())
                self.assertFalse(row["untracked"])
        self.assertEqual((self.repo / "payload.py").read_bytes(), source)
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertEqual((self.repo / "untracked.txt").read_text(), "untracked working file")

    def test_defaults_to_head_and_worktree_uses_tracked_dirty_bytes(self):
        # WHY: the same contextual patch exists only against the locally edited tracked file.
        # catches: always archiving HEAD or applying the patch to the live dirty checkout
        label = self.fixture_with_fault(dirty=True)
        source = (self.repo / "payload.py").read_bytes()
        rows = self.replay(code=(0, 1, 2))
        self.check_replay(rows, label, "patch-failed")
        rows = self.replay(extra=("--worktree",))
        self.check_replay(rows, label, "red")
        self.assertIn(0x34, [row["value"] for row in self.observations()])
        self.assertEqual((self.repo / "payload.py").read_bytes(), source)
        self.assertFalse((self.repo / "test-side-effect").exists())

    def test_reports_command_launch_error(self):
        # WHY: failing to start any test is an execution error, not evidence the fault was detected.
        # catches: labeling a command launch failure as a killed mutation
        label = self.fixture_with_fault()
        impossible = str(self.scratch / "no-such-executable")
        rows = self.replay(code=(0, 1, 2), command=shlex.quote(impossible))
        self.check_replay(rows, label, "error")

    def test_lists_python_and_rust_notes_without_patches(self):
        # WHY: notes still document intended coverage even when they have no replay patch.
        # catches: ignoring Rust catches comments or trying to replay patchless notes
        label = self.fixture_with_fault(patch=False)
        self.write("tests.rs", """
            #[test]
            fn test_missing_ack() {
                // catches: issue an ack before saving the row
                assert_eq!(commit(row), Ack::Persisted);
            }
        """)
        self.commit("Add Rust fault note")
        rows = self.replay(command=shlex.quote(str(self.scratch / "no-such-executable")))
        self.assertCountEqual([(row["description"], row["status"]) for row in rows], [
            (label, "unreplayable"),
            ("issue an ack before saving the row", "unreplayable"),
        ])
        self.assertFalse(self.replay_log.exists())

    def test_removed_workq_lock_survives_the_actual_audited_suite(self):
        # WHY: the audited workq suite uses serial processes, so it cannot detect lost exclusion.
        # catches: missing the real lock-removal survivor or treating it as a killed fault
        for name in ("bin/workq", "bin/workq-hook", "skills/build-loop/scripts/tests/test_workq.py"):
            self.copy(ROOT / name, name)
        self.copy(FIXTURES / "workq-no-lock.patch")
        self.copy(FIXTURES / "workq_fault.py", "test_fault.py")
        self.commit("Snapshot audited workq and lock deletion")
        self.replay_env = os.environ.copy()
        command = shlex.join([
            sys.executable, "-I", "-m", "unittest", "discover",
            "-s", "skills/build-loop/scripts/tests", "-p", "test_workq.py",
        ])
        rows = self.replay(code=(1,), command=command)
        # The audited suite's own patchless catches notes are reported too (unreplayable, per the
        # brief); pick out the lock fault and check nothing else was replayed.
        replayed = [row for row in rows if row["status"] != "unreplayable"]
        self.check_replay(replayed, "remove the exclusive queue lock", "green")
        self.assertTrue(any(row["status"] == "unreplayable" for row in rows), rows)
        self.assertEqual(self.git("status", "--porcelain=v1"), "")


class WitnessTests(TqCase):
    def check_witness(self, source, wanted, *, name="test_property.py", rule="missing-witness"):
        path = self.write(name, source)
        expected = [(path.name, line, rule) for line in wanted]
        self.assertEqual([row[:3] for row in self.findings("witness", path.name)], expected)

    def test_conditional_property_requires_existing_witness(self):
        # WHY: a typo in reaches leaves the only assertion potentially unreachable.
        # catches: taking any reaches comment as evidence that the antecedent occurred
        self.check_witness("""
            def test_uncovered():
                if item is not None:
                    assert item.ready

            def test_typo():
                # reaches: test_itme
                if item is not None:
                    assert item.ready

            def test_covered():
                # reaches: test_item
                if item is not None:
                    assert item.ready

            def test_item():
                assert select_ready_item() is not None
            """, (1, 5))

    def test_witness_resolves_matching_test_across_scanned_paths(self):
        # WHY: a witness may live next to the example fixtures in another scanned file.
        # catches: validating reaches only against functions in the property's own file
        first = self.write("test_properties.py", """
            def test_supported_input():
                # reaches: test_valid_decoder_input
                if decoded is not None:
                    assert decoded.length == 5

            def test_unsupported_input():
                # reaches: test_helper_only
                if decoded is not None:
                    assert decoded.length == 13
            """)
        second = self.write("test_examples.py", """
            def test_valid_decoder_input():
                assert decode(b"valid") is not None

            def helper_only():
                assert decode(b"other") is not None
            """)
        self.assertEqual([row[:3] for row in self.findings("witness", first.name, second.name)], [
            (first.name, 6, "missing-witness"),
        ])

    def test_hypothesis_assume_filter_and_coverage_events(self):
        # WHY: bare assumptions and filtered strategies restrict the accepted inputs.
        # catches: checking only if statements or ignoring Hypothesis event and target
        self.check_witness("""
            @given(st.integers())
            def test_assumption(n):
                assume(n > 0)
                assert parse(str(n)) == n

            @given(st.integers().filter(lambda n: n > 0))
            def test_filtered(n):
                assert parse(str(n)) == n

            @given(st.integers())
            def test_event(n):
                assume(n > 0)
                event("positive integer")
                assert parse(str(n)) == n

            @given(st.integers().filter(lambda n: n > 0))
            def test_target(n):
                target(float(n), label="positive integer")
                assert parse(str(n)) == n
            """, (2, 7))

    def test_proptest_assume_requires_a_rust_witness_or_counter(self):
        # WHY: a proptest assume can discard every interesting example.
        # catches: skipping tests nested in proptest or ignoring their coverage counters
        self.check_witness("""
            proptest! {
                #[test]
                fn test_property(value in any::<i32>()) {
                    prop_assume!(value > 0);
                    prop_assert_eq!(normalized(value), value);
                }

                #[test]
                fn test_counted(values in prop::collection::vec(any::<i32>(), 8)) {
                    prop_assume!(values.iter().any(|value| *value > 0));
                    let mut reached = 0;
                    for value in values {
                        if value > 0 {
                            reached += 1;
                            prop_assert_eq!(normalized(value), value);
                        }
                    }
                    prop_assert!(reached > 0);
                }
            }
            """, (3,), name="properties.rs")


class RiskTests(TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")

    def snapshot(self, *options):
        return json.loads(self.tq("risk", "--repo", self.repo, "--json", *options))

    def test_history_counts_fix_subjects_and_all_changes(self):
        # WHY: fix-subject frequency and churn are different measures of maintenance risk.
        # catches: counting fix commits case-sensitively or counting commits that touch other files
        self.write("scheduler.py", "def minutes(): return 15\n")
        self.write("codec.py", "def version(): return 2\n")
        self.commit("Initial scheduler and codec")
        self.write("scheduler.py", "def minutes(): return 20\n")
        self.commit("Fix midnight rollover")
        self.write("scheduler.py", "def minutes(): return 35\n")
        self.commit("FIX early completion")
        self.write("codec.py", "def version(): return 3\n")
        self.commit("fix encoding version")
        rows = {row["path"]: row for row in self.snapshot()["files"]}
        self.assertEqual((rows["scheduler.py"]["fixes"], rows["scheduler.py"]["churn"]), (2, 3))
        self.assertEqual((rows["codec.py"]["fixes"], rows["codec.py"]["churn"]), (1, 2))
        self.assertGreater(rows["scheduler.py"]["score"], rows["codec.py"]["score"])

    def test_python_risk_counts_importing_packages_and_test_files(self):
        # WHY: duplicate imports and test mentions do not multiply dependent modules or test files.
        # catches: counting mentions or importing files instead of distinct top-level packages
        self.write("ledger.py", "def total(): return 8\n")
        self.write("worker/__init__.py", "")
        self.write("worker/importer.py", "import ledger\nfrom ledger import total\n")
        self.write("worker/checkpoint.py", "from ledger import total\n")
        self.write("service.py", "import ledger as journal\n")
        self.write("tests/test_ledger.py", """
            import ledger
            from ledger import total

            def test_balance():
                assert ledger.total() == 8
        """)
        self.write("tests/test_rollover.py", """
            import ledger

            def test_checkpoint():
                assert ledger.total() >= 5
        """)
        self.write("tests/test_unrelated.py", '"""ledger is mentioned in this comment-like string."""\n')
        self.commit("Add callers of ledger")
        rows = {row["path"]: row for row in self.snapshot()["files"]}
        self.assertEqual(rows["ledger.py"]["importers"], 2)
        self.assertEqual(rows["ledger.py"]["test_refs"], 2)

    def test_rust_risk_counts_use_and_mod_dependencies(self):
        # WHY: crate wiring and modules that use a source file both contribute to its reach.
        # catches: counting Python imports while missing Rust use crate:: and mod paths
        self.write("src/ledger.rs", "pub fn total() -> u32 { 8 }\n")
        self.write("src/lib.rs", "mod ledger;\nmod writer;\nmod reader;\n")
        self.write("src/writer.rs", "use crate::ledger::total;\npub fn next() -> u32 { total() + 1 }\n")
        self.write("src/reader.rs", "pub fn start() -> u32 { 3 }\n")
        self.write("tests/ledger_test.rs", "use example::ledger::total;\n#[test]\nfn sample() { assert_eq!(total(), 8); }\n")
        self.commit("Add ledger and Rust modules")
        rows = {row["path"]: row for row in self.snapshot()["files"]}
        self.assertEqual(rows["src/ledger.rs"]["importers"], 2)
        self.assertEqual(rows["src/ledger.rs"]["test_refs"], 1)
        self.assertEqual(rows["src/reader.rs"]["importers"], 1)
        self.assertEqual(rows["src/reader.rs"]["test_refs"], 0)

    def test_risk_ranks_repeatedly_fixed_untested_source_as_a_gap(self):
        # WHY: the risky file has more fixes and callers than a tested quiet file.
        # catches: sorting alphabetically or reporting every source file as a gap
        self.write("alpha.py", "def answer(): return 9\n")
        self.write("zeta.py", "def interval(): return 20\n")
        self.write("report.py", "from zeta import interval\n")
        self.write("notify.py", "from zeta import interval\n")
        self.write("test_alpha.py", "import alpha\n\ndef test_answer():\n    assert alpha.answer() == 9\n")
        self.commit("Add modules and alpha tests")
        for minutes in (30, 40, 55):
            self.write("zeta.py", f"def interval(): return {minutes}\n")
            self.commit(f"Fix interval to {minutes}")
        rows = self.snapshot()["files"]
        self.assertEqual(rows[0]["path"], "zeta.py")
        self.assertIs(rows[0]["gap"], True)
        alpha = next(row for row in rows if row["path"] == "alpha.py")
        self.assertIs(alpha["gap"], False)
        self.assertEqual(rows, sorted(rows, key=lambda row: (-row["score"], row["path"])))

    def test_launch_budget_funds_three_areas_and_favors_high_risk(self):
        # WHY: a sharply skewed rank distinguishes proportional allocation from a flat split.
        # catches: spending all tests on the first file, splitting evenly, or exceeding the budget
        for name in ("quiet", "second", "hot"):
            self.write(f"{name}.py", "def interval(): return 5\n")
        self.commit("Add three modules")
        for minutes in range(6, 14):
            self.write("hot.py", f"def interval(): return {minutes}\n")
            self.commit(f"Fix hot interval to {minutes}")
        self.write("second.py", "def interval(): return 8\n")
        self.commit("Fix second interval")
        default = self.snapshot()
        custom = self.snapshot("--budget", "7")
        for snapshot, ceiling in ((default, 12), (custom, 7)):
            with self.subTest(ceiling=ceiling):
                rows = snapshot["files"]
                self.assertEqual([row["path"] for row in rows[:3]], ["hot.py", "second.py", "quiet.py"])
                counts = [row["tests"] for row in rows]
                self.assertEqual(sum(counts), ceiling)
                self.assertGreaterEqual(min(counts[:3]), 1)
                self.assertGreater(counts[0], counts[1])
                self.assertGreaterEqual(counts[1], counts[2])

    def test_html_is_self_contained_and_snapshot_escapes_markup(self):
        # WHY: a legal filename can look like an HTML tag or terminate a JSON script block.
        # catches: inserting file paths or snapshot JSON as raw HTML, or dropping the theme fallback
        dangerous = "</script>/<!-- details & <tq-probe>.py"
        self.write(dangerous, "def revision(): return 3\n")
        self.commit("Add filename with markup characters")
        expected = self.snapshot()
        self.assertIn(dangerous, [row["path"] for row in expected["files"]])
        destination = self.scratch / "risk.html"
        self.tq("risk", "--repo", self.repo, "--html", destination)
        source = destination.read_text(encoding="utf-8")

        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.snapshot = []
                self.inside_snapshot = False

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                self.tags.append((tag, attributes))
                if tag == "script" and attributes.get("id") == "tq-risk":
                    self.inside_snapshot = True

            def handle_data(self, data):
                if self.inside_snapshot:
                    self.snapshot.append(data)

            def handle_endtag(self, tag):
                if tag == "script":
                    self.inside_snapshot = False

        page = Page()
        page.feed(source)
        self.assertEqual(
            [attrs.get("type") for tag, attrs in page.tags
             if tag == "script" and attrs.get("id") == "tq-risk"],
            ["application/json"],
        )
        raw_snapshot = "".join(page.snapshot)
        self.assertEqual(json.loads(raw_snapshot), expected)
        self.assertNotIn("</", raw_snapshot)
        self.assertNotIn("<!--", raw_snapshot)
        self.assertNotIn("tq-probe", [tag for tag, _ in page.tags])
        self.assertIn("&lt;tq-probe&gt;", source)
        self.assertFalse(any("src" in attrs or "href" in attrs
                             for tag, attrs in page.tags if tag in {"link", "script", "iframe"}))
        self.assertRegex(source, r"var\(\s*--[\w-]+\s*,\s*[^)]+\)")
        self.assertRegex(source, r"(?:padding|margin)(?:-bottom)?\s*:[^;}]*6rem\b")
        self.assertEqual(self.git("status", "--porcelain=v1"), "")


class PropsTests(TqCase):
    def props(self, *options):
        return json.loads(self.tq("props", *options, "--json"))

    def test_family_returns_cited_schema_records(self):
        # WHY: each of four families needs actionable records from the corpus directory.
        # catches: serving inline summaries or loading records from a missing pilot family
        directory = ROOT / "skills/test-quality/props"
        fields = {
            "id", "schema_version", "family", "variant", "applicability", "kind",
            "law", "input_strategy", "edge_cases", "oracle", "known_wrong_implementations",
            "evidence",
        }
        ids = []
        for family in ("crc", "cobs", "recurring-schedule", "queue-lock"):
            records = self.props(family)
            with self.subTest(family=family):
                self.assertIn(len(records), (2, 3))
                self.assertEqual({record["family"] for record in records}, {family})
                for record in records:
                    self.assertTrue(fields <= record.keys(), record.keys())
                    self.assertTrue(all(record[field] for field in fields), record["id"])
                    self.assertGreaterEqual(record["schema_version"], 1)
                    evidence = record["evidence"]
                    citations = [evidence] if isinstance(evidence, dict) else evidence
                    for citation in citations:
                        self.assertTrue(citation["source"])
                        self.assertTrue(citation.get("section") or citation.get("symbol"))
                        self.assertTrue(citation["revision"])
                        self.assertTrue(citation["license"])
                        self.assertTrue(citation["status"])
                ids.extend(record["id"] for record in records)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertCountEqual(ids, [
            json.loads(path.read_text(encoding="utf-8"))["id"]
            for path in directory.glob("*.json")
        ])

    def test_crc_named_variant_uses_independent_known_answer(self):
        # WHY: the RevEng check value distinguishes a byte sum and omission of the final XOR.
        # catches: storing the wrong variant's CRC or linking a circular runtime oracle
        records = self.props("crc")
        known = [record for record in records
                 if record["kind"] == "known-answer" and "ISO-HDLC" in record["variant"]]
        self.assertEqual(len(known), 1, records)
        record = known[0]
        self.assertIn("123456789", json.dumps(record["input_strategy"]))
        oracle = json.dumps(record["oracle"]).lower()
        self.assertRegex(oracle, r"(?:0xcbf43926|\b3421780262\b)")
        faults = json.dumps(record["known_wrong_implementations"]).lower()
        self.assertRegex(faults, r"byte.{0,15}sum|sum.{0,15}byte")
        self.assertRegex(faults, r"final.{0,15}xor|xor.{0,15}final")
        self.assertIn("reveng.sourceforge.io/crc-catalogue/", json.dumps(record["evidence"]))

    def test_other_families_cover_distinguishing_boundaries(self):
        # WHY: the audit gaps concern block boundaries, calendar transitions and real contention.
        # catches: omitting the requested distinguishing boundary cases from family retrieval
        targets = {
            "cobs": (r"interior.{0,12}zero|embedded.{0,12}zero|zero.{0,12}interior",
                     r"\b253\b", r"\b254\b", r"\b255\b"),
            "recurring-schedule": (r"weekday|byday", r"following week|next week|next monday",
                                   r"dst|daylight", r"gap|nonexistent"),
            "queue-lock": (r"separate.{0,15}process|two.{0,15}process|distinct.{0,15}process",
                           r"overlap|interleav"),
        }
        for family, patterns in targets.items():
            records = self.props(family)
            with self.subTest(family=family):
                text = json.dumps(records).lower()
                for pattern in patterns:
                    self.assertRegex(text, pattern)

    def test_search_matches_variant_beyond_family_name(self):
        # WHY: a reviewer may know the algorithm variant without knowing the corpus family.
        # catches: matching only the family name or doing a case-sensitive search
        rows = self.props("--search", "crc-32/iso-hdlc")
        self.assertTrue(rows)
        self.assertEqual({row["family"] for row in rows}, {"crc"})
        self.assertTrue(any("ISO-HDLC" in row["variant"] for row in rows))
        text = self.tq("props", "--search", "crc-32/iso-hdlc")
        self.assertIn("ISO-HDLC", text)
