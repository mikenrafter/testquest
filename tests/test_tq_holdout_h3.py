"""Red/green tests for holdout round H3 (Dart and Kotlin gating precision)."""
import importlib.util
import json
import os
import shutil
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class HoldoutH3Tests(TqCase):
    def lint_files(self, *paths):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]

    def rule_lines(self, *paths):
        return [(row["line"], row["rule"]) for row in self.lint_files(*paths)]

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart_checks_guards_and_helpers(self):
        shutil.copytree(FIXTURES / "holdout" / "dart", self.repo / "dart_pkg")
        rows = self.rule_lines("dart_pkg/test/dart_checks_and_guards_test.dart")
        self.assertEqual(rows, [
            (127, "guarded-assert"),
            (133, "no-assert"),
            (137, "no-assert"),
            (145, "absence-no-control"),
        ])

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart_self_compare_and_absence(self):
        path = self.copy(FIXTURES / "holdout" / "dart" / "test" / "dart_self_compare_and_absence_test.dart")
        rows = self.rule_lines(path.name)
        self.assertEqual(rows, [
            (48, "self-compare"),
            (55, "self-compare"),
            (63, "absence-no-control"),
            (79, "absence-no-control"),
        ])

    def test_kotlin_holdout_precision(self):
        shutil.copytree(FIXTURES / "holdout" / "kotlin", self.repo / "kt")
        rows = self.rule_lines("kt/KotlinHoldoutTest.kt")
        self.assertEqual(rows, [
            (272, "guarded-assert"),
            (281, "no-assert"),
            (289, "self-compare"),
            (294, "const-truth"),
            (300, "absence-no-control"),
        ])

    def test_kotlin_mockk_own_package_confirm_verified(self):
        shutil.copytree(FIXTURES / "holdout" / "kotlin_mockk", self.repo / "mockk_pkg")
        rows = self.rule_lines("mockk_pkg/src/commonTest/kotlin/io/mockk/it/MockkConfirmTest.kt")
        self.assertEqual(rows, [
            (32, "absence-no-control"),
            (33, "absence-no-control"),
        ])
