"""Red/green tests for holdout round H1 (Java and Python gating precision)."""
import importlib.util
import json
import shutil
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class HoldoutH1Tests(TqCase):
    def lint_files(self, *paths):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]

    def rule_lines(self, *paths):
        return [(row["line"], row["rule"]) for row in self.lint_files(*paths)]

    def test_java_assertion_lib_under_test(self):
        # AssertJ repo defines org.assertj.core.api.Assertions in src/main/java.
        shutil.copytree(FIXTURES / "holdout" / "assertion_lib" / "assertj", self.repo / "assertj")
        findings = self.lint_files("assertj")
        self.assertEqual(findings, [])

        # JUnit repo defines org.junit.jupiter.api.Assertions in src/main/java,
        # but must still flag empty tests and foreign-AssertJ absence checks.
        shutil.copytree(FIXTURES / "holdout" / "assertion_lib" / "junit", self.repo / "junit")
        junit_findings = [(row["line"], row["rule"]) for row in self.lint_files("junit")]
        self.assertEqual(junit_findings, [
            (22, "no-assert"),
            (29, "absence-no-control"),
        ])

    def test_java_helpers_and_frameworks(self):
        shutil.copytree(FIXTURES / "holdout" / "helpers", self.repo / "helpers")
        rows = [(row["line"], row["rule"]) for row in self.lint_files("helpers")]
        self.assertEqual(rows, [
            (19, "no-assert"),
            (93, "no-assert"),
        ])

    def test_java_guards_and_controls(self):
        path = self.copy(FIXTURES / "holdout" / "JavaGuardsAndControlsTest.java")
        rows = self.rule_lines(path.name)
        self.assertEqual(rows, [
            (155, "guarded-assert"),
            (167, "absence-no-control"),
        ])

    def test_python_checks_and_guards(self):
        path = self.copy(FIXTURES / "holdout" / "test_py_checks_and_guards.py")
        rows = self.rule_lines(path.name)
        self.assertEqual(rows, [
            (86, "guarded-assert"),
            (91, "guarded-assert"),
            (96, "guarded-assert"),
            (100, "guarded-assert"),
            (106, "no-assert"),
        ])

    def test_python_self_compare(self):
        path = self.copy(FIXTURES / "holdout" / "test_py_self_compare.py")
        rows = self.rule_lines(path.name)
        self.assertEqual(rows, [
            (40, "self-compare"),
        ])

    def test_python_runner_fixture_directories_ignored(self):
        p1 = self.copy(FIXTURES / "holdout" / "example_scripts" / "test_fixture_trivial.py",
                       "example_scripts/test_fixture_trivial.py")
        p2 = self.copy(FIXTURES / "holdout" / "plugins_integration" / "test_plugin_smoke.py",
                       "plugins_integration/test_plugin_smoke.py")
        self.assertEqual(self.lint_files(str(p1.relative_to(self.repo)), str(p2.relative_to(self.repo))), [])
