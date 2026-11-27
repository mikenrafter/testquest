"""Regressions from the third JVM corpus round."""

import importlib.util
import json
import unittest

from test_tq import TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class JvmRoundThree(TqCase):
    def rules(self, path):
        result = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", path, code=(0, 1)))
        return [(row["line"], row["rule"]) for row in result["findings"]]

    def test_mockk_capture_returned_by_helper(self):
        # why: SearchPoisonModuleTest.kt:273 observes the scripts only in a sibling test.
        # catches: ignoring an observed helper capture or transferring it to another method.
        self.write("Scripts.kt", """
            class ScriptTest {
                private val browser = mockk<Browser>()

                private fun captureScripts(): MutableList<String> {
                    val scripts = mutableListOf<String>()
                    every { browser.evaluate(capture(scripts)) } answers { Unit }
                    return scripts
                }

                @Test fun scripted() {
                    val scripts = captureScripts()
                    trigger(browser)
                    assertTrue(scripts.any { it.contains("clicked") })
                }

                @Test fun noScripts() {
                    triggerWithoutScripts(browser)
                    verify(exactly = 0) { browser.evaluate(any()) }
                }

                @Test fun noNavigation() {
                    triggerWithoutScripts(browser)
                    verify(exactly = 0) { browser.navigate(any()) }
                }
            }
        """)
        self.assertEqual(self.rules("Scripts.kt"), [(23, "absence-no-control")])

    def test_mockito_answer_observed_by_latch(self):
        # why: AudioRecordStreamEarlyWakeTest.java:53 waits for the mock's answer.
        # catches: ignoring a checked await or accepting setup that releases the latch directly.
        self.write("StreamTest.java", """
            import java.util.concurrent.CountDownLatch;
            import java.util.concurrent.TimeUnit;

            class StreamTest {
                @Test void keepOpen() throws Exception {
                    CountDownLatch observed = new CountDownLatch(1);
                    Device device = mock(Device.class);
                    when(device.read()).thenAnswer(call -> {
                        observed.countDown();
                        return 1;
                    });
                    readOnWorker(device);
                    assertThat(observed.await(5, TimeUnit.SECONDS)).isTrue();
                    verify(device, never()).stop();
                }

                @Test void releaseBySetup() throws Exception {
                    CountDownLatch observed = new CountDownLatch(1);
                    OtherDevice device = mock(OtherDevice.class);
                    when(device.read()).thenAnswer(call -> {
                        observed.countDown();
                        return 1;
                    });
                    observed.countDown();
                    assertTrue(observed.await(5, TimeUnit.SECONDS));
                    verify(device, never()).stop();
                }
            }
        """)
        self.assertEqual(self.rules("StreamTest.java"), [(26, "absence-no-control")])

    def test_reported_count_checks_the_same_callback_consumer(self):
        # why: OpusPacketFramingTest.java:277 and :291 check the packet count too.
        # catches: accepting a count from a different call as a control on the collector.
        self.write("FramesTest.java", """
            import java.util.ArrayList;

            class FramesTest {
                @Test void truncated() {
                    var recovered = new ArrayList<Packet>();
                    int count = Decoder.read(truncatedInput(), recovered::add);
                    assertThat(count).isZero();
                    assertThat(recovered).isEmpty();
                }

                @Test void differentCall() {
                    var recovered = new ArrayList<Packet>();
                    var ignored = new ArrayList<Packet>();
                    int count = Decoder.read(truncatedInput(), ignored::add);
                    Decoder.read(validInput(), recovered::add);
                    assertThat(count).isZero();
                    assertThat(recovered).isEmpty();
                }
            }
        """)
        self.assertEqual(self.rules("FramesTest.java"), [(17, "absence-no-control")])

    def test_positive_control_for_connection_failures(self):
        # why: WireFailureTest.kt:91 observes that the connection reaches normal return.
        # catches: treating an unchecked result as a completion-only positive control.
        self.write("ConnectionTest.kt", """
            import org.junit.Test

            class ConnectionTest {
                private fun connect() = endpoint.open()

                // why: positive control. The rejection tests below use the same endpoint.
                @Test fun acceptedKeyConnects() {
                    setUpEndpoint()
                    connect().close()
                }

                @Test fun rejectsInvalidKey() {
                    assertThrows<IOException> { connect() }
                }

                // why: positive control. The rejection tests below use the same endpoint.
                @Test fun reportsPeerIdentity() {
                    connect().readPeerIdentity()
                }
            }
        """)
        self.assertEqual(self.rules("ConnectionTest.kt"), [
            (7, "no-crash"), (17, "no-assert"),
        ])

    def test_nonempty_projection_controls_empty_collection(self):
        # why: CustomPoolRepositoryTest.kt:100 checks surviving rows before clearing the DAO.
        # catches: treating an unchecked or unrelated collection view as evidence.
        self.write("RowsTest.kt", """
            class RowsTest {
                @Test fun clearRows() {
                    val selected = dao.rows.value.filter { it.active }.map { it.id }
                    assertEquals(listOf(7), selected)
                    dao.clear()
                    assertTrue(dao.rows.value.isEmpty())
                    assertTrue(dao.errors.value.isEmpty())
                }
            }
        """)
        self.assertEqual(self.rules("RowsTest.kt"), [(7, "absence-no-control")])

    def test_derived_set_difference_is_a_result(self):
        # why: AppSignalKeywordsCoverageTest.kt:34 compares required and available keys directly.
        # catches: treating a calculated set difference as a test-owned recorder.
        self.write("CoverageTest.kt", """
            class CoverageTest {
                @Test fun coversKeys() {
                    val missing = allCategories - localeMap.keys
                    assertTrue(missing.isEmpty())
                    val recorded = mutableListOf<String>()
                    inspect { recorded.add(it) }
                    assertTrue(recorded.isEmpty())
                }
            }
        """)
        self.assertEqual(self.rules("CoverageTest.kt"), [(7, "absence-no-control")])

    def test_observed_alternate_branch_controls_test_owned_scanner(self):
        # why: OkHttpOrphanGuardTest.kt:68 checks that the same scan found the allowlisted entry.
        # catches: accepting an independently collected allowlist as proof the scanner ran.
        self.write("ScanTest.kt", """
            class ScanTest {
                @Test fun scansEntries() {
                    val rejected = mutableListOf<String>()
                    val reviewed = mutableListOf<String>()
                    entries.forEach {
                        if (it.startsWith("request")) {
                            if (it == allowed) reviewed += it else rejected += it
                        }
                    }
                    assertTrue(rejected.isEmpty())
                    assertEquals(listOf(allowed), reviewed)
                }

                @Test fun separateCollectors() {
                    val rejected = mutableListOf<String>()
                    val reviewed = mutableListOf<String>()
                    entries.forEach { if (it != allowed) rejected += it }
                    reviewed += allowed
                    assertTrue(rejected.isEmpty())
                    assertEquals(listOf(allowed), reviewed)
                }
            }
        """)
        self.assertEqual(self.rules("ScanTest.kt"), [(19, "absence-no-control")])

    def test_cached_identity_assertion_uses_its_message(self):
        # why: LocationRegionalPackTest.kt:201 tests whether two reads return the same cached object.
        # catches: treating the cache check and a direct self-comparison the same way.
        self.write("CatalogTest.kt", """
            import org.junit.Assert.assertSame

            class CatalogTest {
                @Test fun loadsCatalog() {
                    assertSame("second read comes from memory", catalog.load(region), catalog.load(region))
                    assertSame("both regions are supported", catalog.load(region), catalog.load(region))
                    assertSame("second object comes from memory", catalog, catalog)
                }
            }
        """)
        self.assertEqual(self.rules("CatalogTest.kt"), [(6, "self-compare"), (7, "self-compare")])

    def test_sibling_observes_the_same_destructured_fake(self):
        # why: PersonaRotationExpiryTest.kt:199 gets its recorder from a Pair-returning fixture.
        # catches: losing the recorder's type through destructuring or confusing distinct fixture types.
        self.write("StoreTest.kt", """
            class StoreTest {
                class Journal {
                    val rows = mutableListOf<Int>()
                    fun write(id: Int) { rows += id }
                }
                class UnwiredJournal {
                    val rows = mutableListOf<Int>()
                    fun write(id: Int) { rows += id }
                }
                private fun harness(): Pair<Service, Journal> {
                    val journal = Journal()
                    return Service(journal) to journal
                }
                @Test fun accepted() {
                    val (service, journal) = harness()
                    service.record()
                    assertEquals(1, journal.rows.size)
                }
                @Test fun rejected() {
                    val (service, journal) = harness()
                    service.validate()
                    assertTrue(journal.rows.isEmpty())
                    val unrelated = UnwiredJournal()
                    assertTrue(unrelated.rows.isEmpty())
                }
            }
        """)
        self.assertEqual(self.rules("StoreTest.kt"), [(24, "absence-no-control")])

    def test_profile_defaults_disable_the_verified_output(self):
        # why: LocationSpoofPersonaBindingTest.kt:82 and :95 construct a disabled profile.
        # catches: missing a constructor default or treating an explicit override as disabled.
        self.write("DeliveryTest.kt", """
            data class Options(val enabled: Boolean = false)
            class Delivery(private val repo: Profiles, private val context: Context) {
                private val writer = context.lookup(Context.OUTPUT) as Writer
                fun attempt() { val delivered = deliver() }
                private fun deliver() = lock.withLock {
                    val options = repo.get()
                    if (!options.enabled) return@withLock false
                    writer.write()
                    true
                }
            }
            class DeliveryTest {
                val context = mockk<Context>()
                val writer = mockk<Writer>()
                val unrelated = mockk<Writer>()
                @Before fun wire() {
                    every { context.lookup(Context.OUTPUT) } returns writer
                    every { context.lookup(Context.DIFFERENT) } returns unrelated
                }
                fun defaultDelivery() = Delivery(repo = mockk<Profiles> {
                    every { get() } returns Options()
                }, context = context)
                fun enabledDelivery() = Delivery(repo = mockk<Profiles> {
                    every { get() } returns Options(enabled = true)
                }, context = context)
                @Test fun disabled() {
                    val delivery = defaultDelivery()
                    delivery.attempt()
                    verify(exactly = 0) { writer.write() }
                    verify(exactly = 0) { unrelated.write() }
                }
                @Test fun enabled() {
                    val delivery = enabledDelivery()
                    delivery.attempt()
                    verify(exactly = 0) { writer.write() }
                }
            }
        """)
        payload = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", "DeliveryTest.kt", code=(1,)))
        rows = payload["findings"]
        self.assertEqual([(row["line"], row["rule"]) for row in rows],
                         [(29, "absence-no-control"), (30, "absence-no-control"), (35, "absence-no-control")])
        self.assertEqual(["disabled by `Options.enabled = false`" in row["message"] for row in rows],
                         [True, False, False])

    def test_readback_confirms_the_mock_is_installed(self):
        # why: PlaybackManagerMediaSessionTrimTest.java:83 reads the installed player back.
        # catches: treating the identity check for another field or an uninstalled mock as proof.
        self.write("ManagerTest.java", """
            class ManagerTest {
                private static Object get(Manager manager, String name) throws Exception {
                    var field = Manager.class.getDeclaredField(name);
                    field.setAccessible(true);
                    return field.get(manager);
                }
                @Test void playing() throws Exception {
                    Manager manager = manager();
                    Player player = mock(Player.class);
                    Session session = mock(Session.class);
                    set(manager, "player", player);
                    set(manager, "session", session);
                    releaseIfIdle(manager);
                    assertThat(get(manager, "player")).isSameAs(player);
                    assertThat(get(manager, "session")).isSameAs(mock(Session.class));
                    verifyNoInteractions(player);
                    verifyNoInteractions(session);
                }
            }
        """)
        self.assertEqual(self.rules("ManagerTest.java"), [(17, "absence-no-control")])

    def test_stubbed_read_drives_opposite_checked_results(self):
        # why: SearchPoisonModuleTest.kt:569-570 observes the injected mock's read path before rejecting writes.
        # catches: counting a stub from the same type even when the checked output uses a different binding.
        self.write("PolicyTest.kt", """
            class Policy(private val config: Config, private val state: Status) {
                fun blocked() = active(config.get().excluded, state.current)
            }
            class PolicyTest {
                private val config = mockk<Config>()
                private val other = mockk<Config>()

                private fun policy(state: Status) = Policy(config = config, state = state)

                @Test fun checksPolicy() {
                    every { config.get().excluded } returns setOf("primary")
                    every { other.get().excluded } returns setOf("secondary")
                    val state = Status()
                    assertTrue(policy(state).blocked())
                    state.current = true
                    assertFalse(policy(state).blocked())
                    verify(exactly = 0) { config.save(any()) }
                    verify(exactly = 0) { other.save(any()) }
                }
            }
        """)
        self.assertEqual(self.rules("PolicyTest.kt"), [(18, "absence-no-control")])
