"""Check micro-snapshot through lint with the ast-grep engine."""

import importlib.util
import json
import os
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class MicroSnapshot(TqCase):
    def findings_for(self, language, fixture, *, code=(0, 1), rule="micro-snapshot", bucket="findings"):
        path = self.copy(FIXTURES / language / fixture)
        result = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", path.name, code=code))
        return [(row["line"], row["level"], row["message"])
                for row in result[bucket] if row["rule"] == rule]

    def expected(self, site, subject, count):
        return (site, "advisory", f"advisory: {count} substring checks on {subject} pin only parts of the output.")

    def test_python_canonical_hook(self):
        # WHY: these are the original assertions and their golden replacement.
        # catches: counting only positive checks or reporting a whole-output golden
        self.assertEqual(
            (self.findings_for("python", "micro_fragments.py", code=(0,)),
             self.findings_for("python", "micro_golden.py", code=(0,))),
            ([self.expected(12, "alert", 4)], []),
        )

    def test_python_subject_groups(self):
        # WHY: multiple qualifying subjects, keyword operands and equality to a different subject
        # distinguish grouping and exemptions from a counter that covers the whole test.
        # WHY: the judged Markdown report also pins a count on the rows of its first render.
        # catches: picking the largest group, counting variable needles or exempting unrelated equality
        self.assertEqual(self.findings_for("python", "micro_snapshot.py"),
                         [self.expected(2, "report.text()", 3), self.expected(25, "other", 3),
                          (41, "advisory", "advisory: 2 substring checks on text and a line count "
                           "pin only parts of the output.")])

    def test_rust(self):
        # WHY: a mixed group of generic predicates is insufficient evidence. References
        # and snapshots compare whole values.
        # WHY: the judged qex diagnostics span separate bindings. The two additions and
        # the absence of deletions in the judged diff describe one deterministic output.
        # WHY: the migration already has the auth column before the code runs.
        # catches: checking whole coverage across bindings or counting only three-needle snapshots
        self.assertEqual(self.findings_for("rust", "micro_snapshot.rs"),
                         [self.expected(40, "diff", 3),
                          (50, "advisory", "advisory: 1 substring check on residues pins only parts of the "
                           "output. 'auth' already occurs in a setup literal."),
                          self.expected(58, "text", 3)])

    def test_typescript(self):
        # WHY: a regex, template interpolation and a deferred function cannot add checks.
        # WHY: the parity, VS Code audit and mini-bar cases check membership, fixture text and
        # repeated reads respectively. Worker diagnostics belong to separate runtime branches.
        # The parity helper's exported return type and the generated README are both external inputs.
        # catches: counting membership, constants, repeated reads, or checks from opposite arms
        self.copy(FIXTURES / "ts" / "micro_snapshot_helper.ts")
        self.assertEqual(self.findings_for("ts", "micro_snapshot.test.ts", code=(0,)),
                         [self.expected(26, "out", 3), self.expected(38, "out", 3),
                          self.expected(59, "report", 3), self.expected(89, "readme", 3)])

    def test_typescript_fixture_truth(self):
        # WHY: the keybinding audit searched a fixed string, so every assertion passed
        # without running the code under test.
        # catches: dropping the advisory without exposing fixture truth
        rows = self.findings_for("ts", "micro_snapshot.test.ts", code=(0,), rule="const-truth",
                                 bucket="suppressed")
        self.assertEqual([(site, level) for site, level, _ in rows], [(49, "error"), (50, "error"), (51, "error")])

    def test_jvm(self):
        # WHY: the Java assertion has a leading message, while Kotlin uses an infix check.
        # catches: taking the message as the predicate or dropping Kotlin literal membership
        # WHY: the judged user-agent set compares whole values, whereas ffmpegArgs is rendered text.
        # The fluent calls also share a start position and each must count.
        self.assertEqual(
            (self.findings_for("java", "MicroSnapshot.java", code=(0,)),
             self.findings_for("kotlin", "MicroSnapshot.kt", code=(0,))),
            ([self.expected(19, "response.text()", 3)],
             [self.expected(24, "plan.ffmpegArgs", 3)]),
        )

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart(self):
        # WHY: matcher wrappers must expose their actual needles and a golden exempts its subject.
        # WHY: the judged export and import tests assert membership of mapped values and dates.
        # catches: ignoring isNot or counting an interpolated string or collection as a fragment
        self.assertEqual(self.findings_for("dart", "micro_snapshot_test.dart", code=(0,)),
                         [self.expected(35, "report", 3)])
