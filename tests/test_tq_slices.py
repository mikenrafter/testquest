import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from test_tq import TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class SliceTools(TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.write("feature.py", "def count(items):\n    return len(items) - 1\n")
        self.commit("Broken implementation")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.ledger = self.scratch / "ledger.jsonl"
        self.env = dict(os.environ, TQ_LEDGER=str(self.ledger), TQ_GLOBAL_LEDGER="none",
                        TMPDIR=str(self.scratch))
        self.command = f"{shlex.quote(sys.executable)} -m unittest {{test}}"

    def add_test(self, catches="M-012", reaches="when two items arrive, then both count"):
        self.write("test_feature.py", f"""
            import unittest
            from feature import count

            class CountTest(unittest.TestCase):
                # catches: {catches}
                def test_count(self):
                    # reaches: {reaches}
                    self.assertEqual(count(["a", "b"]), 2)
        """)
        self.commit("Add slice test")

    def fix(self, swap="return len(items) - 1", *, header=""):
        self.write("feature.py", f"""
            def count(items):
                # tq-mutant M-012 boundary assumes=A-07 killed-by=test_feature.CountTest.test_count{header}
                # | {swap}
                return len(items)
        """)
        self.commit("Fix boundary with pinned mutant")

    def run_tq(self, *args, code=(0,), command=None):
        return self.tq(*args, "--cmd", command or self.command, code=code, env=self.env)

    def events(self, path=None):
        return [json.loads(line) for line in (path or self.ledger).read_text().splitlines()]

    def test_red_and_green_slice_gates_log_assertion_and_mutation(self):
        self.add_test()
        args = ("slice", "check", 1, "--feature", "count", "--base", self.base)
        self.run_tq(*args)
        self.fix()
        self.run_tq(*args, "--after-green")
        entries = self.events(self.repo / "slices/count/01.jsonl")
        self.assertEqual([row["event"] for row in entries], ["gate", "gate-ok", "gate", "gate-ok"])
        self.assertEqual([row["outcome"] for row in entries if row["event"] == "gate"], ["pass", "pass"])
        self.assertEqual(entries[0]["tests"][0]["classifier"], "unittest")
        self.assertEqual([(row["event"], row["mutant"], row["outcome"]) for row in self.events()],
                         [("mutant", "M-012", "killed")])

    def test_slice_rejects_import_errors_and_bad_notes(self):
        self.add_test(catches="the boundary", reaches="counts all items")
        args = ("slice", "check", 1, "--feature", "count", "--base", self.base)
        self.run_tq(*args, code=(1,))
        self.write("test_feature.py", """
            import unittest
            from missing_module import count

            class CountTest(unittest.TestCase):
                def test_count(self):
                    # catches: M-012
                    # reaches: when two items arrive, then both count
                    self.assertEqual(count(["a", "b"]), 2)
        """)
        self.commit("Add a broken import")
        self.run_tq(*args, code=(1,))
        wrong_notes, bad_import = self.events(self.repo / "slices/count/01.jsonl")
        self.assertTrue(any("catches:" in reason for reason in wrong_notes["reasons"]), wrong_notes)
        self.assertTrue(any("reaches:" in reason for reason in wrong_notes["reasons"]), wrong_notes)
        self.assertEqual(bad_import["tests"][0]["outcome"], "error")
        self.assertIn("ModuleNotFoundError", bad_import["tests"][0]["detail"])

    def test_inline_survives_or_fails_to_parse_and_stale_is_detected(self):
        self.add_test()
        self.fix("return len(items)")
        self.run_tq("mutants", "check", code=(1,))
        self.fix("return (")
        self.run_tq("mutants", "check", code=(1,))
        self.fix()
        pin = self.git("rev-parse", "HEAD").strip()
        source = self.repo / "feature.py"
        source.write_text(source.read_text().replace("return len(items)\n", "return len(tuple(items))\n"))
        self.commit("Change target, leave pinned comment")
        self.run_tq("mutants", "check", "--since", pin, code=(1,))
        self.run_tq("mutants", "check", "--since", "HEAD")
        self.assertEqual([row["outcome"] for row in self.events()],
                         ["survived", "unparseable", "stale"])

    def test_sidecar_anchor_is_unique_and_keeps_relative_indentation(self):
        self.add_test()
        self.write("feature.py", """
            def count(items):
                if items:
                    return len(items)
                return 0
        """)
        self.write(".tq/config", 'mutants = "sidecar"\n')
        sidecar = dict(id="M-012", file="feature.py", anchor="if items:\n    return len(items)",
                       swap="if items:\n    return len(items) - 1", assumes="A-07",
                       flip="boundary", killed_by=["test_feature.CountTest.test_count"])
        self.write(".tq/mutants/M-012.json", json.dumps(sidecar))
        self.commit("Select sidecar")
        self.run_tq("mutants", "check")
        self.write(".tq/mutants/M-012.json", json.dumps(sidecar | {"anchor": "return sum(items)"}))
        self.commit("Anchor drift")
        self.run_tq("mutants", "check", code=(1,))
        self.assertEqual([row["outcome"] for row in self.events()], ["killed", "anchor-missing"])

    def test_revert_check_records_red_then_green_with_dirty_tree(self):
        self.add_test()
        self.fix()
        expected = "test_feature.py::CountTest::test_count"
        source = self.repo / "feature.py"
        source.write_text("local staged edit")
        self.git("add", "feature.py")
        source.write_text("local unstaged edit")
        self.write("notes.txt", "untracked")
        status = self.git("status", "--porcelain")
        worktrees = self.git("worktree", "list", "--porcelain")
        self.run_tq("revert-check", expected, "--mutant", "M-012")
        self.run_tq("revert-check", expected, "--revert", "HEAD")
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(source.read_text(), "local unstaged edit")
        for row in self.events():
            self.assertEqual((row["event"], row["red"], row["green"]), ("kill", [expected], []))
            self.assertTrue(row["bodies"][expected])

    def test_revert_check_catches_same_status_live_file_edit_and_keeps_scratch(self):
        self.add_test()
        self.fix()
        note = self.repo / "notes.txt"
        ignored = self.repo / "ignored.txt"
        self.write(".gitignore", "ignored.txt\n")
        self.write("run.py", f"""
            import runpy
            import sys
            from pathlib import Path
            Path({str(note)!r}).write_text('after')
            Path({str(ignored)!r}).write_text('after')
            sys.argv[0] = 'unittest'
            runpy.run_module('unittest', run_name='__main__')
        """)
        self.commit("Add runner")
        note.write_text("before")
        ignored.write_text("before")
        status = self.git("status", "--porcelain")
        output = self.run_tq("revert-check", "test_feature.CountTest.test_count", "--mutant", "M-012",
                             command=f"{shlex.quote(sys.executable)} run.py {{test}}", code=(1,))
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(note.read_text(), "after")
        self.assertIn("notes.txt", output)
        self.assertIn("ignored.txt", output)
        kept = Path(next(line.removeprefix("Scratch kept at ") for line in output.splitlines()
                         if line.startswith("Scratch kept at ")))
        self.assertTrue((kept / "broken" / ".git").exists())
        self.assertIn("Scratch kept at", output)
        self.assertNotIn("git status --porcelain changed", output)
        row, = self.events()
        self.assertEqual(row["outcome"], "fail")
        self.assertEqual(row["live_before"]["status"], row["live_after"]["status"])
        self.assertNotEqual(row["live_before"]["files_sha256"], row["live_after"]["files_sha256"])
        self.assertEqual(len(row["tests"]), 1)

    @unittest.skipUnless(shutil.which("rustc"), "requires rustc")
    def test_rust_libtest_assertion_kills_a_multiline_swap(self):
        self.write("lib.rs", """
            fn count(items: &[u8]) -> usize {
                // tq-mutant M-031 boundary assumes=A-02 killed-by=tests::last_item replaces=1
                // | let total = items.len();
                // | total - 1
                items.len()
            }

            #[cfg(test)]
            mod tests {
                use super::count;

                #[test]
                fn last_item() {
                    // catches: M-031
                    assert_eq!(count(&[1, 2]), 2);
                }
            }
        """)
        self.write("run_rust.py", """
            import subprocess
            import sys

            subprocess.run(["rustc", "--test", "lib.rs", "-o", "suite"], check=True)
            sys.exit(subprocess.run(["./suite", "--exact", sys.argv[1]]).returncode)
        """)
        self.commit("Rust slice")
        command = f"{shlex.quote(sys.executable)} run_rust.py {{test}}"
        self.run_tq("slice", "check", 1, "--base", self.base, "--after-green", command=command)
        row, = self.events()
        self.assertEqual((row["mutant"], row["outcome"]), ("M-031", "killed"))
        self.assertEqual(row["tests"][0]["mutated"]["classifier"], "rust-libtest")

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_typescript_native_runner_distinguishes_assertion_from_import_error(self):
        check = subprocess.run(["node", "--experimental-strip-types", "--version"], capture_output=True)
        if check.returncode:
            self.skipTest("Node.js needs native TypeScript support")
        self.write("package.json", '{"type":"module"}')
        self.write("number.ts", """
            export function count(items: string[]): number {
                // tq-mutant M-014 boundary assumes=A-03 killed-by=counts both items
                // | return items.length - 1;
                return items.length;
            }
        """)
        source = self.write("number.test.ts", """
            import {test} from "node:test";
            import assert from "node:assert/strict";
            import {count} from "./number.ts";

            test("counts both items", () => {
                // catches: M-014
                assert.equal(count(["a", "b"]), 2);
            });
        """)
        self.commit("TypeScript slice")
        command = "node --experimental-strip-types --test --test-name-pattern={test} number.test.ts"
        self.run_tq("mutants", "check", command=command)
        source.write_text(source.read_text().replace("./number.ts", "./missing.ts"))
        self.commit("Break TypeScript import")
        self.run_tq("slice", "check", 1, "--base", self.base, command=command, code=(1,))
        mutant, = self.events()
        self.assertEqual((mutant["mutant"], mutant["outcome"]), ("M-014", "killed"))
        gate, = self.events(self.repo / "slices/repo/01.jsonl")
        self.assertEqual(gate["tests"][0]["classifier"], "javascript")
        self.assertEqual(gate["tests"][0]["outcome"], "error")
        self.assertIn("ERR_MODULE_NOT_FOUND", gate["tests"][0]["detail"])

    def test_since_does_not_replay_untouched_pin_in_changed_file(self):
        self.add_test()
        self.fix()
        before = self.git("rev-parse", "HEAD").strip()
        source = self.repo / "feature.py"
        source.write_text("# Unrelated comment above the function.\n" + source.read_text())
        self.commit("Move pin without editing either span")
        self.run_tq("mutants", "check", "--since", before)
        self.assertFalse(self.ledger.exists())
        source.write_text(source.read_text().replace("return len(items) - 1", "return len(items) + 1"))
        self.commit("Change the swap")
        self.run_tq("mutants", "check", "--since", before)
        self.assertEqual([row["outcome"] for row in self.events()], ["killed"])

    def test_green_gate_rejects_unreferenced_pin_and_wrong_killer(self):
        self.add_test()
        self.fix()
        self.write("another.py", """
            def total(items):
                # tq-mutant M-014 boundary assumes=A-07 killed-by=test_feature.CountTest.test_count
                # | return 0
                return len(items)
        """)
        self.commit("Add an uncited pin")
        self.run_tq("slice", "check", 1, "--base", self.base, "--after-green", code=(1,))
        gate, = self.events(self.repo / "slices/repo/01.jsonl")
        self.assertIn("M-014: no slice test cites the pinned mutant", gate["reasons"])

        source = self.repo / "feature.py"
        source.write_text(source.read_text().replace("test_feature.CountTest.test_count", "test_other"))
        self.commit("Remove the actual killer")
        self.run_tq("slice", "check", 1, "--base", self.base, "--after-green", code=(1,))
        gate = self.events(self.repo / "slices/repo/01.jsonl")[-1]
        self.assertIn("M-012: killed-by does not name every slice test that cites it", gate["reasons"])
