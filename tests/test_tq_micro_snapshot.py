"""Check micro-snapshot through lint with the ast-grep engine."""

import importlib.util
import json
import os
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class MicroSnapshot(TqCase):
    def findings_for(self, language, fixture, *, code=(0, 1)):
        path = self.copy(FIXTURES / language / fixture)
        result = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", path.name, code=code))
        return [(row["line"], row["level"], row["message"])
                for row in result["findings"] if row["rule"] == "micro-snapshot"]

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
        # catches: picking the largest group, counting variable needles or exempting unrelated equality
        self.assertEqual(self.findings_for("python", "micro_snapshot.py"),
                         [self.expected(2, "report.text()", 3), self.expected(25, "other", 3)])

    def test_rust(self):
        # WHY: negated macro predicates count, references and named snapshots still compare the whole value.
        # catches: requiring simple assertion arguments or ignoring the subject of a snapshot
        self.assertEqual(self.findings_for("rust", "micro_snapshot.rs"),
                         [self.expected(3, "response.text()", 3)])

    def test_typescript(self):
        # WHY: a regex, template interpolation and a deferred function cannot add checks.
        # catches: treating all toMatch operands or checks inside nested functions as literal fragments
        self.assertEqual(self.findings_for("ts", "micro_snapshot.test.ts", code=(0,)),
                         [self.expected(2, "response.text", 3), self.expected(24, "out", 3),
                          self.expected(36, "out", 3), self.expected(36, "log", 3)])

    def test_jvm(self):
        # WHY: the Java assertion has a leading message, while Kotlin uses an infix check.
        # catches: taking the message as the predicate or dropping Kotlin literal membership
        # The fluent calls also share a start position and each must count.
        self.assertEqual(
            (self.findings_for("java", "MicroSnapshot.java", code=(0,)),
             self.findings_for("kotlin", "MicroSnapshot.kt", code=(0,))),
            ([self.expected(4, "response.text()", 3), self.expected(19, "response.text()", 3)],
             [self.expected(3, "response.text", 3)]),
        )

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart(self):
        # WHY: matcher wrappers must expose their actual needles and a golden exempts its subject.
        # catches: ignoring isNot or counting an interpolated Dart string
        self.assertEqual(self.findings_for("dart", "micro_snapshot_test.dart", code=(0,)),
                         [self.expected(3, "response.text()", 3)])
