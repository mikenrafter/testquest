"""Red/green tests for holdout round H4 (Dart and Kotlin gating precision, pass 2)."""
import importlib.util
import json
import os
import shutil
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class HoldoutH4Tests(TqCase):
    def lint_files(self, *paths):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]

    def rule_lines(self, *paths):
        return [(row["line"], row["rule"]) for row in self.lint_files(*paths)]

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart_holdout_h4(self):
        shutil.copytree(FIXTURES / "holdout" / "dart_h4", self.repo / "dart_h4")
        rows = self.rule_lines("dart_h4/test/dart_h4_test.dart")
        self.assertEqual(rows, [
            (60, "no-assert"),
            (64, "guarded-assert"),
            (70, "guarded-assert"),
            (80, "absence-no-control"),
        ])

    def test_kotlin_holdout_h4(self):
        shutil.copytree(FIXTURES / "holdout" / "kotlin_h4", self.repo / "kotlin_h4")
        rows = self.rule_lines("kotlin_h4/core/jvm/test/KotlinH4Test.kt")
        self.assertEqual(rows, [
            (282, "no-assert"),
            (288, "no-assert"),
            (296, "const-truth"),
            (299, "guarded-assert"),
            (306, "guarded-assert"),
            (320, "absence-no-control"),
            (327, "absence-no-control"),
        ])
