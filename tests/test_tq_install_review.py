"""Regression tests for the review before installing tq."""
import json
import subprocess
import sys

import test_tq as contract


class InstallReview(contract.TqCase):
    def test_deep_sources_are_advisory_and_scan_continues(self):
        # WHY: exceeding a parser's stack limit should preserve lint results from subsequent files.
        # catches: aborting the scan or treating valid deep source as a syntax error.
        self.write("a_deep.py", "VALUE = " + " + ".join(["1"] * 20_000) + "\n")
        self.write("b_deep.rs", "#[test]\nfn deep() { assert!(" + "(" * 1_200
                   + "flag()" + ")" * 1_200 + "); }\n")
        self.write("c_bad.py", "def test_bad():\n    assert True\n")
        rows = json.loads(self.tq("lint", "a_deep.py", "b_deep.rs", "--json"))["findings"]
        self.assertEqual([(row["path"], row["rule"], row["level"]) for row in rows], [
            ("a_deep.py", "too-deep", "advisory"),
            ("b_deep.rs", "too-deep", "advisory"),
        ])
        rows = json.loads(self.tq("lint", ".", "--json", code=(1,)))["findings"]
        self.assertEqual([(row["path"], row["rule"]) for row in rows], [
            ("a_deep.py", "too-deep"), ("b_deep.rs", "too-deep"), ("c_bad.py", "const-truth"),
        ])

    def test_long_module_expression_keeps_test_findings(self):
        # WHY: a parseable expression outside a test should not exhaust the scope walk.
        # catches: containing recursion errors without fixing the recursive module scope walk.
        self.write("test_chain.py", "VALUE = " + " + ".join(["'part'"] * 1_500)
                   + "\n\ndef test_bad():\n    assert True\n")
        rows = json.loads(self.tq("lint", ".", "--json", code=(1,)))["findings"]
        self.assertEqual([(row["line"], row["rule"]) for row in rows], [(4, "const-truth")])

    def test_deep_python_import_adds_no_diagnostic(self):
        # WHY: an unreadable imported module leaves an imported iterable unproven.
        # catches: crashing, reporting a skip for a dependency, or assuming it has data.
        self.write("pyproject.toml", "")
        self.write("pkg/gen.py", "CASES = [1]\nVALUE = " + " + ".join(["1"] * 20_000) + "\n")
        self.write("tests/test_cases.py", """
            from pkg.gen import CASES

            def test_cases():
                for case in CASES:
                    assert render(case) == "1"
            """)
        rows = json.loads(self.tq("lint", "tests/test_cases.py", "--json", code=(1,)))["findings"]
        self.assertEqual([(row["path"], row["line"], row["rule"]) for row in rows], [
            ("tests/test_cases.py", 3, "guarded-assert"),
        ])

    def test_deep_python_import_leaves_wrapper_comparison_unresolved(self):
        # WHY: advisory rules also read imported source, and must still leave the test scannable.
        # catches: containing only the guard refinement reader's parse failures.
        self.write("core.py", "def analyze(rows):\n    return Result(rows)\n")
        self.write("adapter.py", "from core import analyze as underlying\n"
                   "def analyze(rows):\n    return underlying(list(rows))\n"
                   "VALUE = " + " + ".join(["1"] * 20_000) + "\n")
        self.write("test_cases.py", """
            import adapter
            import core

            def test_pair():
                rows = [5, 9]
                assert core.analyze(rows).count == adapter.analyze(rows).count
            """)
        self.assertEqual(json.loads(self.tq("lint", "test_cases.py", "--json"))["findings"], [])

    def test_deep_rust_import_adds_no_diagnostic(self):
        # WHY: a crate's deep imported module should only leave its iterable unproven.
        # catches: reporting a skip for the test instead of silently skipping its dependency.
        self.write("Cargo.toml", '[package]\nname = "probe"\n')
        self.write("src/lib.rs", "mod values;\n")
        self.write("src/values.rs", "pub const CASES: [u8; 1] = [7];\n"
                   + "mod nested {" * 1_200 + "}" * 1_200)
        self.write("src/checks.rs", """
            use crate::values::CASES;
            #[test]
            fn test_cases() { for value in CASES { assert!(valid(value)); } }
            """)
        rows = json.loads(self.tq("lint", "src/checks.rs", "--json", code=(1,)))["findings"]
        self.assertEqual([(row["path"], row["line"], row["rule"]) for row in rows], [
            ("src/checks.rs", 3, "guarded-assert"),
        ])

    def test_nested_assertions_can_check_the_outcome(self):
        # WHY: a setup assertion followed by a mutation can still have an outcome check.
        # catches: looking at direct assertions only or crediting tests with just a precondition.
        self.check_lint("""
            def test_with():
                cache = {}
                assert not cache
                fill(cache)
                with open("expected") as stream:
                    assert cache["key"] == stream.read()

            def test_for():
                cache = {}
                assert not cache
                fill(cache)
                for key in ("left", "right"):
                    assert cache[key] == key.upper()

            def test_finally():
                cache = {}
                assert not cache
                fill(cache)
                try:
                    close(cache)
                finally:
                    assert cache["key"] == "value"

            def test_unchecked():
                cache = {}
                assert not cache
                fill(cache)
            """, "no-assert", ("def test_unchecked",))

    def test_unchecked_mock_requires_a_truthiness_assertion(self):
        # WHY: a dummy collaborator can accompany assertions that independently pin the result.
        # catches: flagging compared results, or missing direct and negated truthiness assertions.
        self.check_lint("""
            from unittest.mock import AsyncMock, MagicMock, Mock

            def test_result():
                logger = Mock()
                assert parse("1", logger) == 1

            def test_attribute():
                clock = MagicMock()
                scheduled = schedule(clock, "job")
                assert scheduled.name == "job"

            def test_comparison_under_not():
                sink = AsyncMock()
                assert not (render(sink, "x") != "<x>")

            def test_call_outside_assert():
                sink = Mock()
                render(sink, "x")
                assert output() == "<x>"

            def test_positive_truthiness():
                sink = Mock()
                assert render(sink, "x")  # unchecked positive

            def test_negative_truthiness():
                sink = Mock()
                assert not render(sink, "x")  # unchecked negative
            """, "absence-no-control", ("# unchecked positive", "# unchecked negative"))

    def test_python_loops_over_nonempty_text_and_literal_adapters(self):
        # WHY: these built-ins cannot erase the first item of a non-empty literal.
        # catches: overlooking literal types, empty input, or rebound built-ins.
        self.check_lint("""
            def test_text():
                for value in "abc":
                    assert accepts(value)

            def test_bytes():
                for value in b"xyz":
                    assert accepts(value)

            def test_reversed():
                for value in reversed([1, 2]):
                    assert accepts(value)

            def test_list():
                for value in list((1, 2)):
                    assert accepts(value)

            def test_tuple():
                for value in tuple([1, 2]):
                    assert accepts(value)

            def test_set():
                for value in set([1, 2]):
                    assert accepts(value)

            def test_empty_text():
                for value in "":
                    assert accepts(value)

            def test_empty_bytes():
                for value in b"":
                    assert accepts(value)

            def test_empty_adapter():
                for value in reversed([]):
                    assert accepts(value)

            def test_rebound_adapter():
                reversed = select
                for value in reversed([1, 2]):
                    assert accepts(value)
            """, "guarded-assert", ("def test_empty_text", "def test_empty_bytes",
                                    "def test_empty_adapter", "def test_rebound_adapter"))

    def test_rust_loops_over_nonempty_bytes_and_positive_steps(self):
        # WHY: a positive step can skip later items but preserves the first, and bytes are iterable.
        # catches: treating every step as positive or treating empty byte strings as non-empty.
        self.check_lint(r'''
            #[test]
            fn bytes() { for value in b"abc" { assert!(valid(value)); } }

            #[test]
            fn raw_bytes() { for value in br#"abc"# { assert!(valid(value)); } }

            #[test]
            fn escaped_bytes() { for value in b"\x00" { assert!(valid(value)); } }

            #[test]
            fn steps() { for value in (0..10).step_by(2) { assert!(valid(value)); } }

            #[test]
            fn empty_bytes() { for value in b"" { assert!(valid(value)); } }

            #[test]
            fn empty_raw_bytes() { for value in br#""# { assert!(valid(value)); } }

            #[test]
            fn continued_empty_bytes() {
                for value in b"\
                    " { assert!(valid(value)); }
            }

            #[test]
            fn zero_step() { for value in (0..10).step_by(0) { assert!(valid(value)); } }

            #[test]
            fn empty_range() { for value in (0..0).step_by(2) { assert!(valid(value)); } }
            ''', "guarded-assert", ("fn empty_bytes", "fn empty_raw_bytes", "fn continued_empty_bytes",
                                    "fn zero_step", "fn empty_range"), "checks.rs")

    def test_lint_parses_one_import_once_across_guarded_tests(self):
        # WHY: linting additional tests against the same module must not reparse that module.
        # catches: keeping the cache on an inspector or a single test file.
        self.write("pyproject.toml", "")
        self.write("pkg/core.py", "CASES = [1, 2]\n")
        for name in ("test_first.py", "test_second.py"):
            self.write(name, """
                from pkg.core import CASES

                def test_first():
                    for case in CASES:
                        assert render(case) == "1"

                def test_second():
                    for case in CASES:
                        assert render(case) == "2"
                """)
        result = subprocess.run(
            [sys.executable, "-I", "-c", """
import ast
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from tq_rules import lint

with patch.object(ast, "parse", wraps=ast.parse) as parse:
    result = lint.find(["test_first.py", "test_second.py"])
    reads = sum(Path(call.kwargs.get("filename", "")).name == "core.py"
                for call in parse.call_args_list)
print(json.dumps([reads, result.findings]))
""", str(contract.ROOT)], cwd=self.repo, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [1, []])

    def test_rust_guard_refinement_tolerates_non_string_cargo_names(self):
        # WHY: malformed crate metadata should not abort scanning an otherwise readable test.
        # catches: calling str.replace on a TOML integer in either name field.
        self.write("tests/checks.rs", """
            #[test]
            fn cases() { for value in get_cases() { assert!(valid(value)); } }
            """)
        for manifest in ('[package]\nname = 7\n', '[package]\nname = "sample"\n[lib]\nname = 7\n'):
            with self.subTest(manifest=manifest):
                self.write("Cargo.toml", manifest)
                rows = json.loads(self.tq("lint", "tests/checks.rs", "--json", code=(1,)))["findings"]
                self.assertEqual([(row["line"], row["rule"]) for row in rows], [(2, "guarded-assert")])
