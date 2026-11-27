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

    def test_typed_mock_and_capture_controls(self):
        # WHY: ConsentedUsageObserverTest.kt:61 proves delegate.start in a sibling,
        # and LocationOutputContainmentTest.kt:131 observes a capture on the same mock.
        # catches: requiring local binding identity or accepting a positive on an unrelated mock type
        path = self.write("Controls.kt", """
            import org.junit.Assert.assertTrue
            class Controls {
                @Test fun active() {
                    val observer = mockk<Observer>()
                    verify(exactly = 1) { observer.start() }
                    val manager = mockk<Manager>()
                    val captured = mutableListOf<Fix>()
                    every { manager.publish(capture(captured)) } just Runs
                    exercise(manager)
                    assertTrue("recorded a fix", captured.isNotEmpty())
                }

                @Test fun inactive() {
                    val renamed = mockk<Observer>()
                    val manager = mockk<Manager>()
                    val unrelated = mockk<OtherObserver>()
                    verify(exactly = 0) { renamed.start() }
                    verify(exactly = 0) { manager.publish("gps") }
                    verify(exactly = 0) { unrelated.start() }
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(19, "absence-no-control")])

    def test_spy_activity_and_on_off_twins_are_controls(self):
        # WHY: ConnoisseurBatchEncodeTest.java:158 witnesses actual activity on the
        # spy; PersonaCharacterTest.kt:160 explicitly checks the on and off results.
        # catches: requiring the exact field on a recording spy or missing a conjunction's positive check
        java = self.write("EncoderTest.java", """
            class EncoderTest {
                static class SpyEncoder implements FrameEncoder {
                    final List<Integer> batchCalls = new ArrayList<>();
                    final List<Integer> oldCalls = new ArrayList<>();
                    void batch(int n) { batchCalls.add(n); }
                    void perFrame(int n) { oldCalls.add(n); }
                }

                @Test void noOldCalls() {
                    SpyEncoder spy = new SpyEncoder();
                    SpyEncoder other = new SpyEncoder();
                    run(spy);
                    assertThat(spy.batchCalls).hasSize(3);
                    assertThat(spy.oldCalls).isEmpty();
                    assertThat(other.oldCalls).isEmpty();
                }
            }
        """)
        kotlin = self.write("CharacterTest.kt", """
            class CharacterTest {
                @Test fun off() {
                    val on = Behavior.view(persona, true)!!
                    val off = Behavior.view(persona, false)!!
                    val different = Behavior.view(otherPersona, false)!!
                    assertTrue(on.flavour.isNotEmpty() && on.visible)
                    assertTrue(off.flavour.isEmpty())
                    assertTrue(different.flavour.isEmpty())
                }
            }
        """)
        self.assertEqual([(row["path"], row["line"], row["rule"]) for row in self.findings(java.name, kotlin.name)],
                         [(kotlin.name, 8, "absence-no-control"), (java.name, 15, "absence-no-control")])

    def test_single_file_lint_finds_support_control_in_another_package(self):
        # WHY: PersonaEgressConnectorTest witnesses the rig's lookup recorder.
        # catches: looking only in the selected files or in the requesting test's package
        base = "src/test/kotlin/example"
        self.write(f"{base}/support/Rig.kt", """
            package example.support
            class Rig { val lookups = mutableListOf<String>() }
        """)
        negative = self.write(f"{base}/road/RoadTest.kt", """
            package example.road
            import example.support.Rig
            class RoadTest {
                @Test fun offline() {
                    val rig = Rig()
                    assertTrue(rig.lookups.isEmpty())
                }
            }
        """)
        self.write(f"{base}/dns/ConnectorTest.kt", """
            package example.dns
            import example.support.Rig
            class ConnectorTest {
                @Test fun online() {
                    val differentName = Rig()
                    connect(differentName)
                    assertEquals(listOf("target.invalid"), differentName.lookups)
                }
            }
        """)
        self.assertEqual(self.locations(negative.relative_to(self.repo)), [])

    def test_literal_false_precondition_requires_wiring(self):
        # WHY: PoisonEngineFailClosedTest.kt:118 passes a profile with two flags off,
        # but a true isEnabled override makes those collaborators eligible for dispatch.
        # catches: treating the false flag as decisive even when the tested mock ignores it
        path = self.write("DispatchTest.kt", """
            class DispatchTest {
                private val profile = Profile(cookieEnabled = false, signalEnabled = false)
                @Test fun actionsStop() {
                    val cookie = mockk<CookieModule> {
                        every { isEnabled() } returns profile.cookieEnabled
                    }
                    val signal = mockk<SignalModule> {
                        every { isEnabled() } returns true
                    }
                    dispatch(cookie, signal)
                    coVerify(exactly = 0) { cookie.onAction() }
                    coVerify(exactly = 0) { signal.onAction() }
                }
            }
        """)
        rows = self.findings(path.name)
        self.assertEqual([(row["line"], row["rule"]) for row in rows],
                         [(11, "absence-no-control"), (12, "absence-no-control")])
        self.assertIn("fixture disables the collaborator", rows[0]["message"])
        self.assertIn("can report a result", rows[1]["message"])

    def test_try_fail_only_catches_the_expected_exception(self):
        # WHY: AndroidImportJournalTest.kt:57 rejects every malformed input; a catch
        # that also catches the assertion's failure would make acceptance pass.
        # catches: counting a swallowed AssertionError as a successful expected-failure check
        path = self.write("JournalTest.kt", """
            class JournalTest {
                @Test fun wrongType() {
                    for (record in listOf("duplicate", "truncated")) {
                        try { read(record); fail("accepted") }
                        catch (_: JournalUnreadableException) {}
                    }
                }
                @Test fun catchesAssertion() {
                    try { read("truncated"); fail("accepted") }
                    catch (_: Throwable) {}
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(8, "guarded-assert")])

    def test_sibling_size_contract_and_fixed_kotlin_inputs(self):
        # WHY: FakeRouteGeneratorTest.kt:55 checks a range derived from a sized result;
        # QueryBlocklistAnyLocaleTest.kt:47 iterates an enum and a fixed map's values.
        # catches: ignoring witnessed cardinality, or granting it to an unrelated filtered result
        path = self.write("RouteTest.kt", """
            class RouteTest {
                @Test fun length() {
                    val route = generator.generateRoute(city, count = 10)
                    assertEquals(10, route.size)
                }
                @Test fun order() {
                    val route = generator.generateRoute(city, count = 10)
                    for (index in 1 until route.size) assertTrue(route[index] > route[index - 1])
                }
                @Test fun fixedRange() {
                    val fixture = Fixture()
                    (0..23).forEach { assertTrue(valid(fixture, it)) }
                }
                @Test fun fixedCollections() {
                    repeat(2) { assertTrue(valid(it)) }
                    val samples = mapOf(Locale.EN to "en", Locale.RU to "ru")
                    for (locale in Locale.entries) {
                        samples.values.forEach { assertTrue(valid(locale, it)) }
                    }
                }
                @Test fun possiblyEmpty() {
                    val route = generator.generateRoute(city, count = 10)
                    route.mapNotNull { runCatching { parse(it) }.getOrNull() }
                        .forEach { assertTrue(valid(it)) }
                }
            }
            enum class Locale { EN, RU }
        """)
        rows = self.findings(path.name)
        self.assertEqual([(row["line"], row["rule"]) for row in rows], [(21, "guarded-assert")])
        self.assertIn("filter", rows[0]["message"])

    def test_literal_java_fixtures_reach_the_check_past_continue(self):
        # WHY: ScribeStoreCompactOpusTest.java:289 and :336 each have a literal
        # fixture item that reaches the assertion regardless of product behavior.
        # catches: accepting a product-controlled filter or requiring all fixtures to reach the assertion
        path = self.write("StoreTest.java", """
            class StoreTest {
                enum Encoding { PCM, OPUS }
                record Item(Encoding encoding) {}

                static List<Item> mix() {
                    List<Item> items = new ArrayList<>();
                    items.add(new Item(Encoding.OPUS));
                    items.add(new Item(Encoding.PCM));
                    return items;
                }
                @Test void raw() {
                    for (Item item : mix()) {
                        boolean clean = item.encoding() == Encoding.OPUS && isFramed(item);
                        if (clean) continue;
                        assertTrue(matches(item));
                    }
                }
                @Test void opus() {
                    for (Item item : mix()) {
                        if (item.encoding() != Encoding.OPUS) continue;
                        assertTrue(matches(item));
                    }
                }
                @Test void productFiltered() {
                    for (Item item : mix()) {
                        if (!isFramed(item)) continue;
                        assertTrue(matches(item));
                    }
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(24, "guarded-assert")])

    def test_callback_control_follows_the_constructor_argument(self):
        # WHY: ModuleSilentFailureTest.kt:150 observes the same outcome sink in
        # sibling tests. A collector wired to another argument has a separate path.
        # catches: keying callback evidence by local variable name or by constructor alone
        path = self.write("SinkTest.kt", """
            class SinkTest {
                @Test fun online() {
                    val observed = mutableListOf<String>()
                    Service(onEvent = { event -> observed += event }, onError = {}).run()
                    assertEquals(listOf("event"), observed)
                }
                @Test fun offline() {
                    val renamed = mutableListOf<String>()
                    val errors = mutableListOf<String>()
                    Service(onEvent = { renamed += it }, onError = { errors += it }).run()
                    assertTrue(renamed.isEmpty())
                    assertTrue(errors.isEmpty())
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(12, "absence-no-control")])

    def test_checked_capture_slot_witnesses_invocation(self):
        # WHY: FacebookDyiImporterTest.kt:134 is backed by a sibling test that
        # reads the DAO's captured row. Installing an unused stub supplies no control.
        # catches: requiring a collection size check, or accepting an unread capture
        path = self.write("ImporterTest.kt", """
            class ImporterTest {
                private val dao = mockk<Dao>()
                private val unrelated = mockk<Dao>()
                @Test fun validImport() {
                    val saved = slot<Row>()
                    val unused = slot<Row>()
                    coEvery { dao.save(capture(saved)) } returns Unit
                    coEvery { unrelated.save(capture(unused)) } returns Unit
                    importRows()
                    assertEquals("campaign", saved.captured.name)
                }
                @Test fun invalidImport() {
                    importRows()
                    coVerify(exactly = 0) { dao.save(any()) }
                    coVerify(exactly = 0) { unrelated.save(any()) }
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(15, "absence-no-control")])

    def test_java_mocks_keep_factory_and_type_controls(self):
        # WHY: TrackMediatorTest.java:240 shares the provider factory and
        # PlaybackManagerMediaSessionTrimTest.java:82 has a sibling MediaSession mock.
        # catches: requiring one Java mock binding in cases that deliberately build several
        path = self.write("SessionsTest.java", """
            class SessionsTest {
                private Session session(boolean armed) {
                    Session session = mock(Session.class);
                    when(session.isArmed()).thenReturn(armed);
                    return session;
                }

                @Test void activeOnly() {
                    Session active = session(true);
                    Session inactive = session(true);
                    Session different = session(false);
                    dispatch(active, inactive, different);
                    verify(active, times(1)).release();
                    verify(inactive, never()).release();
                    verify(different, never()).release();
                }

                @Test void playback() {
                    Player player = mock(Player.class);
                    play(player);
                    verify(player).start();
                }

                @Test void idle() {
                    Player player = mock(Player.class);
                    Session session = mock(Session.class);
                    idle(player, session);
                    verifyNoInteractions(player);
                    verifyNoInteractions(session);
                }
            }
        """)
        self.assertEqual(self.locations(path.name), [(15, "absence-no-control"), (29, "absence-no-control")])
