"""Exercise JVM discovery, gating and source notes through the optional CLI."""
import importlib.util
import json
import sys
import unittest

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class AstGrepJvm(TqCase):
    def fixture(self, language, name):
        return self.copy(FIXTURES / language / name)

    def locations(self, *paths):
        return [(row["line"], row["rule"]) for row in json.loads(
            self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]]

    def test_kotlin_discovery_and_evidence(self):
        # WHY: registrations, annotated functions and inner assertions have distinct owners.
        # catches: counting a deferred lambda or an uncalled assertion helper as evidence
        path = self.fixture("kotlin", "Spec.kt")
        self.assertEqual(self.locations(path.name), [
            (8, "no-assert"), (11, "guarded-assert"), (21, "no-assert"),
            (24, "self-compare"), (25, "const-truth"), (26, "self-compare"),
            (32, "guarded-assert"), (33, "no-assert"), (36, "no-assert"),
        ])

    def test_java_methods_and_frameworks(self):
        # WHY: JUnit 4's leading message shifts its equality and boolean operands.
        # catches: accepting a disabled test or ignoring the assertion after a message
        path = self.fixture("java", "Instrumented.java")
        self.assertEqual(self.locations(path.name), [
            (8, "no-assert"), (12, "self-compare"), (13, "const-truth"),
            (14, "self-compare"), (15, "self-compare"), (19, "guarded-assert"),
            (26, "no-assert"),
        ])

    def test_java_fluent_and_loop_boundaries(self):
        # WHY: a fluent transformation is only setup, while a one-entry loop runs its check.
        # catches: treating builder options as checks or ignoring the first Java iteration
        path = self.fixture("java", "ControlFlow.java")
        self.assertEqual(self.locations(path.name), [(8, "no-assert"), (17, "guarded-assert"), (35, "const-truth")])

    def test_kotest_styles_and_immutable_fixture_sizes(self):
        # WHY: disabled DSL registrations and mutable fixture lengths require distinct handling.
        # catches: applying a mutable list initializer's size after the list has been cleared
        path = self.fixture("kotlin", "Styles.kt")
        self.assertEqual(self.locations(path.name), [
            (9, "no-assert"), (15, "no-assert"), (20, "no-assert"), (26, "no-assert"),
            (35, "guarded-assert"),
        ])

    def test_mockk_requires_an_unconditional_control_on_the_same_recorder(self):
        # WHY: neither a different recorder nor a bypassable check witnesses a negative observation.
        # catches: treating a shadowed binding or a conditional positive check as a control
        path = self.fixture("kotlin", "Recorders.kt")
        self.assertEqual(self.locations(path.name), [(line, "absence-no-control")
                                                     for line in (5, 14, 22, 25, 30)])

    def test_mockito_verification_modes(self):
        # WHY: never and times(0) make absence claims that need a positive verification.
        # catches: counting an unrelated Mockito interaction as a same-recorder control
        path = self.fixture("java", "Recorders.java")
        self.assertEqual(self.locations(path.name), [(4, "absence-no-control"), (12, "absence-no-control")])

    def test_notes_and_kotlin_script_scan(self):
        # WHY: Kotlin line and doc comments must reach lint, witness and catches.
        # catches: including Kotlin scripts in lint while dropping them or their notes from witness and catches
        self.fixture("kotlin", "Notes.kts")
        self.assertEqual(self.locations("."), [(12, "guarded-assert"), (17, "guarded-assert")])
        witness = json.loads(self.tq("witness", "--engine", "ast-grep", "--json", ".", code=(1,)))
        self.assertEqual([(row["line"], row["rule"]) for row in witness], [(17, "missing-witness")])
        self.git("init", "-q")
        self.commit("notes")
        catches = json.loads(self.tq("catches", "--engine", "ast-grep", "--repo", self.repo,
                                    "--cmd", f"{sys.executable} -V", "--json"))
        self.assertEqual([(row["test"], row["description"]) for row in catches],
                         [("withNotes", "the wrong error type")])

    def test_history_uses_qualified_methods_when_a_check_is_deleted(self):
        # WHY: Java permits identical short test names in distinct nested classes.
        # catches: using the short name or missing Java deletions when selecting changed tests
        self.git("init", "-q")
        source = """class Suite {
          class First {
            @Test void shared() {
              work();
              assertTrue(ready());
            }
          }
          class Second {
            @Test void shared() { work(); }
          }
        }
        """
        path = self.write("Suite.java", source)
        self.commit("baseline")
        path.write_text(source.replace("      assertTrue(ready());\n", ""))
        self.assertEqual(self.locations("--since", "HEAD"), [(3, "no-assert")])
