"""Label assertion forms by the rows expected from the optional analyzer."""

import importlib.util
import json
from pathlib import Path
import unittest

from test_tq import TqCase


FIXTURES = Path(__file__).with_name("fixtures") / "tq" / "jetbrains"
LABELS = {"const-truth", "guarded-assert", "misordered-expected", "no-assert",
          "self-derived-expected", "swallowed-assertion"}


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class JetBrainsFixtures(TqCase):
    def rows(self, name):
        source = (FIXTURES / f"{name}.fixture").read_text()
        path = self.write(name, source)
        response = self.tq("lint", "--engine", "ast-grep", "--json", path.name, code=(0, 1))
        actual = {(row["line"], row["rule"]) for row in json.loads(response)["findings"]
                  if row["rule"] in LABELS}
        expected = {(line, label) for line, text in enumerate(source.splitlines(), 1)
                    if (label := text.split()[-1] if text.split() else "") in LABELS}
        return actual, expected

    def test_python(self):
        # WHY: a swallowed failure, shared invocation and branch agreement have different causes.
        # catches: following the last branch, using a later handler, or losing the operand's origin
        actual, expected = self.rows("test_assertions.py")
        self.assertEqual(actual, expected)

    def test_rust(self):
        # WHY: macro operands retain the source position of their bindings after reparsing.
        # catches: resolving the alias at the macro fragment's synthetic offset
        actual, expected = self.rows("examples.rs")
        self.assertEqual(actual, expected)

    def test_typescript(self):
        # WHY: only a bare catch that lets the test pass should emit the advisory.
        # catches: considering a named catch, rethrow or soft check to be a swallowed failure
        actual, expected = self.rows("examples.test.ts")
        self.assertEqual(actual, expected)

    def test_java(self):
        # WHY: a multi-catch catches each listed type, and Jupiter puts the message last.
        # catches: counting only a single catch type or taking the message as the actual value
        actual, expected = self.rows("ExamplesTest.java")
        self.assertEqual(actual, expected)

    def test_kotlin(self):
        # WHY: Throwable captures assertion failures, and named arguments state their roles.
        # catches: treating Exception as Throwable or following lexical order for named arguments
        actual, expected = self.rows("ExamplesTest.kt")
        self.assertEqual(actual, expected)

    @unittest.expectedFailure
    def test_kotlin_callable_reference_alias(self):
        # WHY: the callable reference forwards a check.
        # Known gap: the JVM adapter does not resolve local callable references.
        actual, expected = self.rows("CallableReferenceTest.kt")
        self.assertEqual(actual, expected)

    @unittest.expectedFailure
    def test_java_method_reference_alias(self):
        # WHY: compare.accept dispatches to the method reference bound to compare.
        # Known gap: the JVM adapter does not resolve assertion method references.
        self.assertEqual(*self.rows("MethodReferenceTest.java"))

    @unittest.expectedFailure
    def test_python_bound_method_alias(self):
        # WHY: the saved bound method still fails the enclosing test on inequality.
        # Known gap: the Python adapter does not associate the local alias with a check.
        self.assertEqual(*self.rows("test_assertion_alias.py"))

    @unittest.expectedFailure
    def test_typescript_local_expect_alias(self):
        # WHY: the locally bound expect creates the same matcher as the framework import.
        # Known gap: the TypeScript adapter does not follow bindings of expect.
        self.assertEqual(*self.rows("alias.test.ts"))

    @unittest.expectedFailure
    def test_rust_imported_macro_alias(self):
        # WHY: the renamed macro still panics on unequal operands.
        # Known gap: the Rust adapter does not identify this external macro through its alias.
        self.assertEqual(*self.rows("alias.rs"))
