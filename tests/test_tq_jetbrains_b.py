"""Exercise assertion and recorder resolution through tq lint."""

import importlib.util
import json
import os
import unittest

from test_tq import TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class ResolvedChecks(TqCase):
    def findings(self, path):
        rows = json.loads(self.tq("lint", "--engine", "ast-grep", "--json", path, code=(0, 1)))["findings"]
        return [(row["line"], row["rule"]) for row in rows]

    def test_assertion_owner_and_junit4_message_binding(self):
        # why: the expected and actual slots follow the imported JUnit 4 overload.
        # catches: treating a known foreign owner as JUnit, or reading the message as expected.
        self.write("Overloads.java", """
            import org.junit.Assert;
            import static org.junit.jupiter.api.Assertions.assertEquals;
            import example.Metrics;
            class Overloads {
                @Test void legacy() { Assert.assertEquals(message(), actual(), actual()); }
                @Test void jupiter() { assertEquals(actual(), actual(), message()); }
                @Test void metrics() { Metrics.assertEquals(actual(), actual()); }
            }
        """)
        self.assertEqual(self.findings("Overloads.java"), [
            (5, "self-compare"), (6, "self-compare"), (7, "no-assert"),
        ])

    def test_assertall_executes_later_callbacks(self):
        # why: a failing executable leaves the remaining executables runnable (Jupiter assertAll).
        # catches: stopping the aggregate at its first assertion failure or counting a guarded child unconditionally.
        self.write("Aggregates.java", """
            import static org.junit.jupiter.api.Assertions.*;
            class Aggregates {
                @Test void all() {
                    assertAll(() -> fail("first"), () -> assertEquals(actual(), actual()));
                }
                @Test void guarded() {
                    assertAll(() -> { if (condition()) assertEquals(5, actual()); });
                }
            }
        """)
        self.assertEqual(self.findings("Aggregates.java"), [(4, "self-compare"), (6, "guarded-assert")])

    def test_soft_assertion_receiver(self):
        # why: AssertJ passes a live SoftAssertions receiver and flushes at the callback boundary.
        # catches: counting the fluent setup as a check or ignoring the terminal on the soft receiver.
        self.write("Soft.java", """
            import org.assertj.core.api.SoftAssertions;
            class Soft {
                @Test void checked() {
                    SoftAssertions.assertSoftly(softly -> softly.assertThat(actual()).isEqualTo(actual()));
                }
                @Test void setup() {
                    SoftAssertions.assertSoftly(softly -> softly.assertThat(actual()));
                }
            }
        """)
        self.assertEqual(self.findings("Soft.java"), [(4, "self-compare"), (6, "no-assert")])

    def test_mockk_alias_to_field(self):
        # why: j7's sibling controls only count for the same recorder and method.
        # catches: using the alias declaration as its own recorder, or following it past an escape.
        self.write("Aliased.kt", """
            import io.mockk.*
            class Aliased {
                private val browser = mockk<Browser>()
                @Test fun navigates() { verify { browser.navigate() } }
                @Test fun avoidsNavigation() {
                    val recorder = this.browser
                    verify(exactly = 0) { recorder.navigate() }
                }
                @Test fun unknownEscape() {
                    val recorder = this.browser
                    handOff(recorder)
                    verify(exactly = 0) { recorder.navigate() }
                }
            }
        """)
        self.assertEqual(self.findings("Aliased.kt"), [(12, "absence-no-control")])

    def test_typescript_assertion_imports(self):
        # why: named node:assert imports and Vitest soft terminals check outcomes.
        # catches: counting namespace utilities and shadowed expect as assertions.
        self.write("owners.test.ts", """
            import mainAssert, {strictEqual as same} from 'node:assert';
            import {expect as check, test} from 'vitest';
            test('bad equality', () => same(actual(), actual()));
            test('soft', () => check.soft(actual()).toBe(actual()));
            test('utility', () => mainAssert.CallTracker());
            test('shadowed', () => { const check = fakeExpect; check.soft(actual()).toBe(1); });
        """)
        self.assertEqual(self.findings("owners.test.ts"), [
            (3, "self-compare"), (4, "self-compare"), (5, "no-assert"), (6, "no-assert"),
        ])

    def test_typescript_destructured_recorder(self):
        # why: j6 compares sibling controls on a shared fixture with controls on fresh recorders.
        # catches: separating a direct object member from its same-test destructured binding.
        self.write("recorders.test.ts", """
            import {expect, it, vi} from 'vitest';
            it('controlled', () => {
                const fixture = {spy: vi.fn(), other: vi.fn()};
                const {spy: recorder} = fixture;
                expect(recorder).not.toHaveBeenCalled();
                fixture.spy();
                expect(fixture.spy).toHaveBeenCalled();
                expect(fixture.other).not.toHaveBeenCalled();
            });
        """)
        self.assertEqual(self.findings("recorders.test.ts"), [(8, "absence-no-control")])

    def test_pytest_raises_import(self):
        # why: aliases preserve the pytest.raises outcome check, and a foreign raises does no checking.
        # catches: treating raises as a generic assertion or ignoring the imported alias.
        self.write("test_raises.py", """
            from pytest import raises as expects_error
            from metrics import raises
            from unittest import TestCase as Case

            def test_expected_error():
                with expects_error(ValueError):
                    parse_bad_input()

            def test_record_error():
                raises(ValueError)

            def test_unbound_assertion():
                case = Case()
                Case.assertEqual(case, actual(), actual())
        """)
        self.assertEqual(self.findings("test_raises.py"), [(9, "no-assert"), (14, "self-compare")])

    def test_python_recorder_aliases(self):
        # why: a sibling observation can exercise the same collector even through a tuple binding.
        # catches: ignoring the positive alias or reusing it after handing it to an unknown function.
        self.write("test_recorders.py", """
            def test_control():
                calls = []
                others = []
                recorder, spare = (calls, others)
                register(calls.append)
                assert recorder == [expected()]
                assert not calls
                assert not others

            def test_escaped():
                calls = []
                recorder = calls
                hand_off(recorder)
                assert recorder == [expected()]
                assert not calls
        """)
        self.assertEqual(self.findings("test_recorders.py"), [
            (8, "absence-no-control"), (15, "absence-no-control"),
        ])

    def test_rust_recorder_reborrow(self):
        # why: a mutable reborrow addresses the original recorder during its lifetime.
        # catches: separating the positive check on the borrow from the original empty check.
        self.write("recorders.rs", """
            #[test]
            fn controlled() {
                let mut calls = Vec::new();
                let other = Vec::new();
                let recorder = &mut calls;
                recorder.push(42);
                assert_eq!(recorder.len(), 1);
                calls.clear();
                assert!(calls.is_empty());
                assert!(other.is_empty());
            }
        """)
        self.assertEqual(self.findings("recorders.rs"), [(10, "absence-no-control")])

    def test_typescript_micro_snapshot_alias(self):
        # why: all three substrings come from one render, even via a stable alias.
        # catches: splitting substring checks by spelling instead of the observed binding.
        self.write("fragments.test.ts", """
            import {expect, it} from 'vitest';
            it('renders row summaries', () => {
                const report = renderRows();
                const readback = report;
                expect(report).toContain('Mode');
                expect(readback).toContain('Release');
                expect(report).toContain('Build');
            });
        """)
        self.assertEqual(self.findings("fragments.test.ts"), [(5, "micro-snapshot")])

    @unittest.skipUnless(os.environ.get("TQ_DART_GRAMMAR"), "Dart requires TQ_DART_GRAMMAR")
    def test_dart_recorder_alias_to_field(self):
        # why: the named recorder and the destructured field refer to the same list.
        # catches: missing the alias control or treating a different field as the same recorder.
        self.write("recorders_test.dart", """
            import 'package:test/test.dart';
            class Harness {
              final calls = [];
              final other = [];
              void register() {
                test('checks recorder identity', () {
                  var alias = this.calls;
                  final (recorder, spare) = (alias, other);
                  subscribe(alias.add);
                  subscribe(other.add);
                  publish();
                  expect(recorder, isNotEmpty);
                  calls.clear();
                  expect(calls, isEmpty);
                  expect(other, isEmpty);
                });
              }
            }
        """)
        self.assertEqual(self.findings("recorders_test.dart"), [(15, "absence-no-control")])

    def test_soft_assertions_require_a_flush(self):
        # why: a SoftAssertions instance accumulates failures until assertAll runs.
        # catches: counting a buffered check as enforced when the test omits the flush.
        self.write("Flush.java", """
            import org.assertj.core.api.SoftAssertions;
            class Flush {
                @Test void missingFlush() {
                    var softly = new SoftAssertions();
                    softly.assertThat(actual()).isEqualTo(42);
                }
                @Test void flushes() {
                    var softly = new SoftAssertions();
                    softly.assertThat(actual()).isEqualTo(actual());
                    softly.assertAll();
                }
                @Test void guardedCheck() {
                    var softly = new SoftAssertions();
                    if (condition()) softly.assertThat(actual()).isEqualTo(42);
                    softly.assertAll();
                }
            }
        """)
        self.assertEqual(self.findings("Flush.java"), [
            (3, "no-assert"), (9, "self-compare"), (12, "guarded-assert"),
        ])
