"""Exercise ECMAScript test checks through the optional CLI engine."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class AstGrepTypeScript(TqCase):
    def rows(self, *args):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *args, code=(0, 1)))["findings"]

    def fixture(self, name):
        return self.copy(FIXTURES / "ts" / name)

    def locations(self, *paths):
        return [(row["line"], row["rule"]) for row in self.rows(*paths)]

    def test_framework_discovery_and_callback_ownership(self):
        # WHY: registrations own their callback body, including curried each.
        # catches: attributing a nested test or skipped test's checks to its parent
        path = self.fixture("discovery.test.ts")
        self.assertEqual(self.locations(path.name), [(4, "no-assert"), (5, "no-assert"), (8, "no-assert"), (18, "no-assert")])

    def test_checks_and_comparisons(self):
        # WHY: checks vary by framework and only a proven tautology gets a finding.
        # catches: treating custom matcher arguments as equality operands
        path = self.fixture("checks.test.ts")
        self.assertEqual(self.locations(path.name), [
            (18, "no-assert"), (19, "self-compare"), (20, "self-compare"),
            (21, "const-truth"), (21, "self-compare"), (22, "const-truth"), (23, "self-compare"),
            (31, "self-compare"),
        ])

    def test_branch_coverage_and_iteration(self):
        # WHY: a known iteration count is insufficient when a break can skip its check.
        # catches: assuming that either one branch or a nonempty loop covers every path
        path = self.fixture("guards.test.ts")
        self.assertEqual(self.locations(path.name), [
            *((line, "guarded-assert") for line in (1, 3, 5, 6, 7, 8, 9, 11, 13, 14, 16)),
            (17, "no-assert"),
        ])

    def test_async_checks_require_completion(self):
        # WHY: waiting for a raw array leaves the promises produced by map detached.
        # catches: accepting an unrelated await or an array of promises as completion
        path = self.fixture("async.test.ts")
        self.assertEqual(self.locations(path.name), [(line, "guarded-assert") for line in (1, 4, 7, 9, 13, 14, 16)])

    def test_positive_control_is_on_the_same_recorder(self):
        # WHY: mock.calls and the spy are one recorder, but a conditional control can be skipped.
        # catches: allowing another spy or a guarded check to stand in for a positive control
        path = self.fixture("absence.test.ts")
        self.assertEqual(self.locations(path.name), [(line, "absence-no-control") for line in (3, 11, 20, 32)])

    def test_all_suffixes_and_ignored_directories(self):
        # WHY: generated and ignored files must not appear in a directory scan.
        # catches: walking generated output or applying the TypeScript grammar to JSX
        self.git("init", "-q")
        self.write(".gitignore", "ignored*.ts\n")
        for directory in ("node_modules", "dist", "build", "coverage"):
            self.write(f"{directory}/junk.test.ts", 'test("junk", () => {});')
        self.write("ignored.test.ts", 'test("ignored", () => {});')
        for suffix in ("ts", "tsx", "js", "jsx", "mjs", "cjs"):
            expression = "<div />" if suffix in {"tsx", "jsx"} else "work()"
            self.write(f"test.{suffix}", f'test("smoke", () => {expression});')
        rows = self.rows(".")
        self.assertEqual([(row["path"], row["rule"]) for row in rows], [
            (f"test.{suffix}", "no-assert") for suffix in ("cjs", "js", "jsx", "mjs", "ts", "tsx")
        ])
        self.assertEqual(json.loads(self.tq("lint", "--json", "."))["findings"], [])

    def test_notes_use_callback_ownership(self):
        # WHY: both line and block comments must reach the existing note consumers.
        # catches: scanning JavaScript for lint but excluding it from witness or catches
        path = self.fixture("notes.test.ts")
        self.assertEqual(self.locations(path.name), [(3, "guarded-assert"), (7, "guarded-assert")])
        witness = json.loads(self.tq("witness", "--engine", "ast-grep", "--json", path.name, code=(1,)))
        self.assertEqual([(row["line"], row["rule"]) for row in witness], [(7, "missing-witness")])
        self.git("init", "-q")
        self.commit("notes")
        replays = json.loads(self.tq("catches", "--engine", "ast-grep", "--repo", self.repo,
                                    "--cmd", f"{sys.executable} -V", "--json"))
        self.assertEqual([(row["test"], row["description"]) for row in replays], [
            ("reaches the branch", "wrong value"), ("multiline note", "throws the wrong error"),
            ("inner", "nested fault"),
        ])

    def test_since_includes_deleting_a_check_from_a_qualified_test(self):
        # WHY: the deleted line is outside the new callback span and the peer shares its short name.
        # catches: applying the old-side lookup by short test name or by Python/Rust suffix only
        self.git("init", "-q")
        source = '''describe("first", () => {
          test("shared", () => {
            consume();
            expect(out()).toBe(7);
          });
        });
        describe("second", () => { test("shared", () => consume()); });
        '''
        self.write("suite.ts", source)
        self.commit("guarded outcome")
        self.write("suite.ts", "".join(line for line in source.splitlines(keepends=True) if "expect" not in line))
        self.assertEqual(self.locations("--since", "HEAD"), [(2, "no-assert")])
