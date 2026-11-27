"""Red/green tests for holdout round H2 (Java and Python gating precision)."""
import importlib.util
import json
import shutil
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class HoldoutH2Tests(TqCase):
    def lint_files(self, *paths):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]

    def rule_lines(self, *paths):
        return [(row["line"], row["rule"]) for row in self.lint_files(*paths)]

    def test_java_h2_precision_and_recall(self):
        self.git("init")
        shutil.copytree(FIXTURES / "holdout" / "java_h2", self.repo / "java_h2")
        rows = self.rule_lines("java_h2/sub/src/test/java/org/example/sub/JavaH2Tests.java")
        self.assertEqual(rows, [
            (295, "no-assert"),
            (300, "guarded-assert"),
            (311, "absence-no-control"),
            (318, "absence-no-control"),
        ])

    def test_python_h2_precision_and_recall(self):
        self.copy(FIXTURES / "holdout" / "py_h2_helpers.py")
        path = self.copy(FIXTURES / "holdout" / "test_py_h2_guards_and_checks.py")
        rows = self.rule_lines(path.name)
        self.assertEqual(rows, [
            (151, "no-assert"),
            (155, "guarded-assert"),
            (162, "guarded-assert"),
            (167, "no-assert"),
            (179, "absence-no-control"),
        ])
