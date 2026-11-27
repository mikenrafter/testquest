"""Exercise Dart test checks through the optional CLI engine."""

import importlib.util
import json
import os
import sys
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py") and os.environ.get("TQ_DART_GRAMMAR"),
                     "Dart requires ast-grep-py and TQ_DART_GRAMMAR")
class AstGrepDart(TqCase):
    def locations(self, *paths):
        rows = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]
        return [(row["line"], row["rule"]) for row in rows]

    def test_discovery(self):
        # catches: counting setUp evidence or losing the inherited skip
        path = self.copy(FIXTURES / "dart" / "discovery_test.dart")
        self.assertEqual(self.locations("."), [(8, "no-assert"), (11, "no-assert")])
        self.assertEqual(json.loads(self.tq("lint", "--json", path.name)), {"findings": [], "suppressed": []})

    def test_matchers_and_tautologies(self):
        # catches: reading contains as equality or flagging a repeatability check
        path = self.copy(FIXTURES / "dart" / "checks_test.dart")
        self.assertEqual(self.locations(path.name), [
            (4, "self-compare"), (5, "const-truth"), (8, "self-compare"),
            (9, "self-compare"), (12, "const-truth"), (16, "self-compare"),
        ])

    def test_guards(self):
        # catches: treating an optional list element or a breaking loop as an unconditional check
        path = self.copy(FIXTURES / "dart" / "guards_test.dart")
        self.assertEqual(self.locations(path.name),
                         [(site, "guarded-assert") for site in (4, 6, 8, 9, 10, 12, 16)])

    def test_future_completion(self):
        # catches: accepting an unrelated await or an awaited list as completing a Future
        path = self.copy(FIXTURES / "dart" / "async_test.dart")
        self.assertEqual(self.locations(path.name),
                         [(site, "guarded-assert") for site in (8, 10)])

    def test_recorder_control(self):
        # catches: accepting a different recorder or a conditional check as a positive control
        path = self.copy(FIXTURES / "dart" / "absence_test.dart")
        self.assertEqual(self.locations(path.name),
                         [(site, "absence-no-control") for site in (7, 21, 27, 37)])

    def test_callbacks_across_helpers_and_scheduling(self):
        # catches: skipping resolved wrapper bodies or counting unrelated completion
        self.copy(FIXTURES / "dart" / "round_one_support.dart")
        self.copy(FIXTURES / "dart" / "round_one_support.dart", "lib/support.dart")
        self.write("pubspec.yaml", "name: dart_fixtures\n")
        path = self.copy(FIXTURES / "dart" / "round_one_callbacks_test.dart")
        self.assertEqual(self.locations(path.name), [(15, "no-assert"), (38, "no-assert"), (48, "no-assert")])

    def test_fixed_inputs_and_value_equality(self):
        # catches: confusing fixed input with product output or two calls with one value
        path = self.copy(FIXTURES / "dart" / "round_one_loops_test.dart")
        self.assertEqual(self.locations(path.name), [(25, "guarded-assert"), (30, "guarded-assert")])

    def test_registration_context_and_forwarding(self):
        # catches: ignoring supplied checks, joining detached runners, or counting unused setup callbacks
        for fixture in ("round_two_platform.dart", "round_two_support.dart"):
            self.copy(FIXTURES / "dart" / fixture)
        path = self.copy(FIXTURES / "dart" / "round_two_callbacks_test.dart")
        self.assertEqual(self.locations(path.name), [
            (25, "no-assert"), (44, "guarded-assert"), (48, "guarded-assert"),
            (69, "no-assert"), (74, "no-assert"), (76, "no-assert"), (77, "no-assert"),
        ])

    def test_fixed_loop_inputs_and_equivalent_guard(self):
        # catches: accepting empty product results, mutable input, or a guard that changes the checked value
        path = self.copy(FIXTURES / "dart" / "round_two_loops_test.dart")
        self.assertEqual(self.locations(path.name), [
            (51, "guarded-assert"), (54, "guarded-assert"), (57, "guarded-assert"),
            (61, "guarded-assert"), (65, "guarded-assert"), (70, "guarded-assert"),
            (93, "guarded-assert"), (109, "guarded-assert"),
        ])

    def test_absence_controls_and_unproven_detectors(self):
        # catches: accepting a witness for the wrong detector or a swallowed error
        path = self.copy(FIXTURES / "dart" / "round_one_absence_test.dart")
        self.assertEqual(self.locations(path.name), [
            (31, "absence-no-control"), (57, "absence-no-control"),
            (66, "absence-no-control"), (85, "absence-no-control"), (104, "absence-no-control"),
        ])

    def test_comment_consumers(self):
        # catches: separating Dart's block comments or qualified names from the shared note consumers
        path = self.copy(FIXTURES / "dart" / "notes_test.dart")
        self.assertEqual(self.locations(path.name), [(5, "guarded-assert")])
        witness = json.loads(self.tq("witness", "--engine", "ast-grep", "--json", path.name, code=(1,)))
        self.assertEqual([(row["line"], row["rule"]) for row in witness], [(11, "missing-witness")])
        self.git("init", "-q")
        self.commit("notes")
        replays = json.loads(self.tq("catches", "--engine", "ast-grep", "--repo", self.repo,
                                    "--cmd", f"{sys.executable} -V", "--json"))
        self.assertEqual([(row["test"], row["description"]) for row in replays],
                         [("probed branch", "wrong value"), ("missing witness", "wrong callback")])

    def test_deleting_a_check_selects_its_qualified_owner(self):
        # catches: matching deletion-side tests by short name or excluding Dart from the diff
        self.git("init", "-q")
        source = """void main() {
          group('first', () {
            test('shared', () {
              work();
              expect(result, 7);
            });
          });
          group('second', () { test('shared', () => work()); });
        }
        """
        self.write("suite_test.dart", source)
        self.commit("checked result")
        self.write("suite_test.dart", source.replace("              expect(result, 7);\n", ""))
        self.assertEqual(self.locations("--since", "HEAD"), [(3, "no-assert")])
