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
        rows = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))
        return [(row["line"], row["rule"]) for row in rows]

    def test_discovery(self):
        # catches: counting setUp evidence or losing the inherited skip
        path = self.copy(FIXTURES / "dart" / "discovery_test.dart")
        self.assertEqual(self.locations("."), [(8, "no-assert"), (11, "no-assert")])
        self.assertEqual(json.loads(self.tq("lint", "--json", path.name)), [])

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
                         [(site, "guarded-assert") for site in (4, 6, 8, 10, 16, 18)])

    def test_recorder_control(self):
        # catches: accepting a different recorder or a conditional check as a positive control
        path = self.copy(FIXTURES / "dart" / "absence_test.dart")
        self.assertEqual(self.locations(path.name),
                         [(site, "absence-no-control") for site in (7, 21, 27, 37)])

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
