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

    def findings(self, *paths):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *paths, code=(0, 1)))["findings"]

    def locations(self, *paths):
        return [(row["line"], row["rule"]) for row in self.findings(*paths)]

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

    def test_wrappers_follow_invoked_callback_bindings(self):
        # WHY: PersonaRotationLockTest.kt:182 and PersonaSeedLoggingTest.kt:32 run checks through a wrapper.
        # catches: counting an unresolved or retained lambda, or missing an invoked lambda across files
        wrappers = self.fixture("jvm", "Wrappers.kt")
        self.fixture("jvm", "Round2Support.kt")
        self.assertEqual(self.locations(wrappers.name), [(26, "no-assert")])

    def test_fixed_inputs_execute_the_loop_body(self):
        # WHY: ActionTypeLabelTest.kt:44, MarkovQueryGeneratorSafetyTest.kt:100,
        # PersonaNamesTest.kt:38 and CircadianRateModulatorTest.kt:69 iterate over fixed fixtures.
        # SelfReportLayerTest.kt:25 can still pass when its product loop is empty.
        # catches: treating fixed inputs and a potentially empty result as interchangeable
        path = self.fixture("jvm", "FixedInputs.kt")
        rows = self.findings(path.name)
        self.assertEqual([(row["line"], row["rule"]) for row in rows], [(18, "guarded-assert")])
        self.assertIn("`weights().values`", rows[0]["message"])

    def test_failure_helpers_and_polls_check_completion(self):
        # WHY: ConnectionFlowTest.kt:243, RoundLimitTest.kt:66 and PersonaSeedParserTest.kt:120
        # check after failure handling. OnboardingScreenTest.kt:134, CustomPoolRepositoryTest.kt:103
        # and TargetingViewModelSeedLedgerTest.kt:60 fail when their condition fails to arrive.
        # catches: letting a try/catch bypass later checks, or overlooking a poll that fails on timeout
        path = self.fixture("jvm", "Completion.kt")
        self.assertEqual(self.locations(path.name), [])

    def test_kotlin_sibling_controls_match_actual_recorders(self):
        # WHY: BridgeSupervisionTest.kt:111, PoisonEngineLoopTest.kt:940 and PersonaEgressToggleTest.kt:131
        # have positive siblings. CustomPoolRepositoryTest.kt:76 and TrackerHostsTest.kt:83 check results.
        # catches: ignoring a same-recorder sibling, accepting a different field, or treating a result as a recorder
        path = self.fixture("jvm", "RecorderControls.kt")
        self.assertEqual(self.locations(path.name), [(18, "absence-no-control"), (28, "absence-no-control")])

    def test_java_sibling_verification_matches_the_mock(self):
        # WHY: PlaybackManagerMediaSessionTrimTest.java:82 has a positive verification on the same mock.
        # catches: treating any positive verification as evidence for every mock in the class
        path = self.fixture("jvm", "RecorderControlsJava.java")
        self.assertEqual(self.locations(path.name), [(7, "absence-no-control")])

    def test_expression_body_stops_before_next_registration(self):
        # WHY: Changing the registration after TrackerBlockMonitorTest.kt:222 must select only its own test.
        # catches: selecting a preceding expression-bodied test when changing only its sibling
        path = self.fixture("jvm", "Spans.kt")
        self.git("init", "-q")
        self.commit("baseline")
        path.write_text(path.read_text().replace("        work()", "        moreWork()"))
        self.assertEqual(self.locations("--since", "HEAD"), [(11, "no-assert")])

    def test_object_delegation_does_not_swallow_a_test(self):
        # WHY: LocationResetPathsTest.kt:138 has a delegate before try/use and another test.
        # catches: treating the object's class body as a trailing callback to the delegate
        path = self.fixture("jvm", "DelegatedSpans.kt")
        self.assertEqual(self.locations(path.name), [(16, "no-assert")])

    def test_controls_share_the_resolved_support_field_across_files(self):
        # WHY: PersonaEgressToggleTest.kt:131 uses a rig whose payload recorder is checked elsewhere.
        # catches: requiring identical local receiver names or using a control for a different rig field
        paths = [self.copy(FIXTURES / "jvm" / name, f"src/test/kotlin/example/{name}")
                 for name in ("RigSibling.kt", "RigSupport.kt")]
        self.assertEqual(self.locations(*paths), [(8, "absence-no-control")])
