import importlib.util
import json
import os
import shlex
import subprocess
import sys
import unittest

from test_tq import FIXTURES, ROOT, TqCase


SLICES = FIXTURES / "slices"


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class SliceRules(TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.ledger = self.scratch / "ledger.jsonl"
        self.env = dict(os.environ, TQ_LEDGER=str(self.ledger), TQ_GLOBAL_LEDGER="none",
                        TMPDIR=str(self.scratch))
        self.write("clock.py", "def hour():\n    return 20\n")
        self.commit("Base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def events(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def gate(self, *args, command, code=1):
        stdout = self.tq("slice", "check", 1, "--base", self.base, *args, "--cmd", command,
                         "--json", code=(code,), env=self.env)
        row = json.loads(stdout)
        self.assertIn(row, self.events(self.repo / "slices/repo/01.jsonl"))
        return row

    def test_reaches_reports_the_comment_line_and_keeps_a_distinct_condition(self):
        # why: A duplicated test title or effect does not witness a reachable condition.
        # catches: applying the old sentence-shape check without testing the content.
        self.copy(SLICES / "reaches.test.tsx")
        self.commit("Add reachability notes")
        row = self.gate(command=f"{shlex.quote(sys.executable)} -c 'pass' {{test}}")
        reasons = [reason for reason in row["reasons"] if "reaches:" in reason]
        self.assertEqual([reason.split(": ")[0] for reason in reasons],
                         [f"reaches.test.tsx:{line}" for line in (6, 12, 18, 24)])
        self.assertIn("test name", reasons[0])
        self.assertIn("test name", reasons[1])
        self.assertIn("distinct", reasons[2])
        self.assertIn("empty", reasons[3])

    def test_trivial_handlers_and_elements_differ_from_a_condition_swap(self):
        # why: A deleted JSX control or no-op handler does not challenge a decision.
        # catches: treating every replacement with an added line as non-trivial.
        path = SLICES / "clock.tsx"
        original = path.read_text()
        changes = [
            ("    <small>Time</small>\n", "", "trivial"),
            ("ready && !hidden ? ", "ready && false ? ", "trivial"),
            ("ready && !hidden ? ", "check() && false ? ", "non-trivial"),
            ("if (!hidden) open();", "if (!hidden) { /* nothing */ }", "trivial"),
            ("(event: Event) => open(event)", "() => {}", "trivial"),
            ("(event: Event) => open(event)", "(event: Event) => { return undefined; }", "trivial"),
            ("active !== tonight", "!entries.includes(active)", "non-trivial"),
            ("onClick={onClick}", "onClick={open}", "non-trivial"),
        ]
        for before, after, expected in changes:
            with self.subTest(before=before):
                result = subprocess.run(
                    [sys.executable, "-c", """
import json
from pathlib import Path
import sys
from tq_rules.pinned_mutants import classify_swap
path, source, updated = json.load(sys.stdin)
print(classify_swap(Path(path), source, updated))
"""],
                    cwd=ROOT, check=True, input=json.dumps([str(path), original, original.replace(before, after)]),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.stdout.strip(), expected)

    def test_all_trivial_kills_are_reported_and_disagreement_fails_both_commands(self):
        # why: Even a clean kill cannot justify a set made entirely of trivial pins.
        # catches: treating a pin claiming boundary as sufficient evidence of diversity.
        self.write("test_clock.py", """
            import unittest
            from clock import hour

            class ClockTest(unittest.TestCase):
                def test_hour(self):
                    # catches: M-012
                    self.assertEqual(hour(), 21)
        """)
        self.write("clock.py", """
            def hour():
                # tq-mutant M-012 boundary assumes=A-01 killed-by=test_clock.ClockTest.test_hour
                # | return None
                return 21
        """)
        self.commit("Add a pin that empties the return value")
        command = f"{shlex.quote(sys.executable)} -m unittest {{test}}"
        stdout = self.tq("mutants", "check", "--json", "--cmd", command, code=(1,), env=self.env)
        row = self.gate("--after-green", command=command)
        mutant, = row["mutants"]
        self.assertEqual((mutant["outcome"], mutant["classification"], mutant["line"]),
                         ("killed", "trivial", 4))
        recorded, _ = self.events(self.ledger)
        self.assertEqual(json.loads(stdout), {"mutants": [recorded]})
        self.assertEqual(recorded["classification"], "trivial")
        self.assertIn("boundary", recorded["flip_disagreement"])
        self.assertTrue(any("every pinned mutant is trivial" in reason for reason in row["reasons"]))
        self.assertEqual(row["advisories"], ["all 1 killed, 1 trivial"])

    def test_mixed_sidecar_set_reports_advisory_and_passes(self):
        # why: A deleted call still deserves a kill when a separate pin tests a boundary.
        # catches: failing any slice with a trivial pin or counting that pin as non-trivial.
        self.write("test_clock.py", """
            import unittest
            from clock import hour

            class ClockTest(unittest.TestCase):
                def test_hour(self):
                    # catches: M-012 M-013
                    self.assertEqual(hour(), 21)
        """)
        self.write("clock.rule", "set_hour(21);\n")
        self.write("clock.py", """
            from pathlib import Path

            def hour():
                line = Path("clock.rule").read_text().strip()
                return int(line.removeprefix("set_hour(").removesuffix(");") or 0)
        """)
        self.write(".tq/config", "mutants = sidecar\n")
        settings = dict(file="clock.rule", anchor="set_hour(21);", assumes="A-01",
                        killed_by=["test_clock.ClockTest.test_hour"])
        for pin, swap, flip in (("M-012", "", "drop-effect"),
                                ("M-013", "set_hour(20);", "off-by-one")):
            self.write(f".tq/mutants/{pin}.json", json.dumps(settings | dict(id=pin, swap=swap, flip=flip)))
        self.commit("Add clock boundary and deletion pins")
        command = f"{shlex.quote(sys.executable)} -m unittest {{test}}"
        row = self.gate("--after-green", command=command, code=0)
        self.assertEqual([(pin["classification"], pin["outcome"]) for pin in row["mutants"]],
                         [("trivial", "killed"), ("non-trivial", "killed")])
        self.assertEqual(row["advisories"], ["all 2 killed, 1 trivial"])
