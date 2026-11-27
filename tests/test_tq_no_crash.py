import importlib.util
import json
import os
import unittest

from test_tq import TqCase


HAS_AST_GREP = importlib.util.find_spec("ast_grep_py") is not None


class NoCrashTests(TqCase):
    def test_python_validator(self):
        # why: the polara metadata validator throws when it rejects a manifest.
        # catches: gating an explicitly stated completion test, or using the title alone.
        self.write("test_capture.py", '''
            def test_capture_metadata(tmp_path):
                """Valid metadata must not raise."""
                (tmp_path / "capture.json").write_text('{"schema":1}')
                require_native_capture(tmp_path)

            def test_invalid_metadata_does_not_raise(tmp_path):
                try:
                    require_native_capture(tmp_path)
                except ValueError:
                    pass

            def test_validation_does_not_raise():
                assert require_native_capture() == {"schema": 1}
        ''')
        wanted = [(1, "no-crash"), (6, "no-assert")]
        self.check_rules("test_capture.py", wanted, "legacy")
        if HAS_AST_GREP:
            self.check_rules("test_capture.py", wanted)

    def test_rust_panic_contract(self):
        # why: diff-core's readonly cache regression uses the call as the panic witness.
        # catches: requiring an assertion helper or counting assertions on returned values.
        self.write("cache.rs", """
            #[test]
            fn store_readonly_dir_does_not_panic() {
                store_cached("/dev/null/impossible", "key");
            }

            #[test]
            fn store_readonly_with_wrapper() {
                assert_no_panic(|| store_cached("/dev/null/impossible", "key"));
            }

            #[test]
            fn store_readonly_and_check_status() {
                assert_no_panic(|| store_cached("/dev/null/impossible", "key"));
                assert!(cache.is_empty());
            }

            #[test]
            fn path_normalization_never_panics() {
                let _ = normalize_path(".");
            }
        """)
        wanted = [(2, "no-crash"), (7, "no-crash"), (18, "no-crash")]
        self.check_rules("cache.rs", wanted, "legacy")
        if HAS_AST_GREP:
            self.check_rules("cache.rs", wanted)

    @unittest.skipUnless(HAS_AST_GREP, "ast-grep-py is unavailable")
    def test_typescript_completion_only(self):
        # why: the judged REA subprocess case observes successful completion.
        # catches: losing an explicit negated matcher or treating all toThrow checks alike.
        self.write("completion.test.ts", """
            import { test, expect } from "vitest";
            test("script does not throw", () => {
              execute();
            });
            test("successful script", () => {
              expect(() => execute()).not.toThrow();
            });
            test("rejects bad script", () => {
              expect(() => execute("invalid")).toThrow();
            });
            test("checks script output", () => {
              expect(() => execute()).not.toThrow();
              expect(execute()).toBe("ready");
            });
            test("checks output inside the wrapper", () => {
              expect(() => expect(execute()).toBe("ready")).not.toThrow();
            });
            test("script does not throw but catches its errors", () => {
              setup();
              try { execute(); } catch (error) { log(error); }
            });
            test("script callback does not throw", () => {
              expect(execute).not.toThrow();
            });
        """)
        self.check_rules("completion.test.ts", [
            (2, "no-crash"), (5, "no-crash"), (18, "no-assert"), (22, "no-crash")
        ])

    @unittest.skipUnless(HAS_AST_GREP and os.getenv("TQ_DART_GRAMMAR"), "Dart grammar is unavailable")
    def test_dart_close_and_native_getter(self):
        # why: crypto checks repeatable close, while http leaves taskIdentifier unverified.
        # catches: reading a getter without checking it, or overlooking returnsNormally.
        self.write("completion_test.dart", """
            import 'package:test/test.dart';
            void main() {
              test('close may be called multiple times', () {
                var sink = startChunkedConversion();
                sink.close();
                sink.close();
              });
              test('close handles an empty sink', () {
                expect(() => sink.close(), returnsNormally);
              });
              test('taskIdentifier', () {
                task.taskIdentifier; // Just verify that there is no crash.
              });
              test('close empties the sink', () {
                expect(() {
                  sink.close();
                  expect(sink.length, 0);
                }, returnsNormally);
              });
            }
        """)
        self.check_rules("completion_test.dart", [(3, "no-crash"), (8, "no-crash"), (11, "no-assert")])

    @unittest.skipUnless(HAS_AST_GREP, "ast-grep-py is unavailable")
    def test_jvm_completion_and_unchecked_outcomes(self):
        # why: doubleCloseIsNoOp checks an exception, but OpusWriter's empty data
        # and PoisonEngine's destroy path claim effects their tests never observe.
        # catches: clearing the gate whenever a comment mentions a lack of exceptions.
        self.write("WriterTest.java", """
            import org.junit.jupiter.api.Test;
            import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
            class WriterTest {
                @Test void doubleCloseIsNoOp() {
                    var writer = open();
                    writer.close();
                    writer.close(); // should not throw
                }
                @Test void closeEmpty() {
                    assertDoesNotThrow(() -> open().close());
                }
                @Test void ignoresNullAndEmptyData() {
                    try (var writer = open()) {
                        writer.writeOpusData(null);
                    }
                    // If we got here without throwing, the test passes.
                }
            }
        """)
        self.write("EngineTest.kt", """
            import org.junit.Test
            class EngineTest {
                @Test fun `engine calls destroy without crashing`() {
                    engine = createEngine()
                    engine.start()
                    engine.destroy()
                    // No exception = pass. Verifies destroy cancels scope cleanly.
                }

                @Test fun `a broken store never fails the action`() {
                    val broken = FailingStore()
                    report(broken)
                }

                @Test fun `engine stops gracefully when no modules enabled`() = runTest {
                    engine = createEngine()
                    engine.start()
                    delay(200)
                    engine.destroy()
                    // Engine should have stayed in its loop without crashing.
                }
            }
        """)
        self.check_rules("WriterTest.java", [(4, "no-crash"), (9, "no-crash"), (12, "no-assert")])
        self.check_rules("EngineTest.kt", [(3, "no-assert"), (10, "no-crash"), (15, "no-assert")])

    def test_since_summary_counts_advisories_in_changed_tests(self):
        # why: new completion-only tests should prompt the same review as a deliberate
        # double-close regression while leaving the gate open.
        # catches: counting unchanged tests, using an error exit, or omitting JSON summary.
        self.git("init", "-q")
        path = self.write("test_close.py", """
            def test_double_close_does_not_throw():
                sink.close()
                sink.close()
        """)
        self.commit("Original completion test")
        with path.open("a") as stream:
            stream.write(
                "\ndef test_close_empty_does_not_raise():\n    empty.close()\n"
                "\n# tq: allow no-crash Closing an unopened stream used to throw\n"
                "def test_close_stream_does_not_raise():\n    stream.close()\n"
            )
        args = ("lint", "--since", "HEAD")
        output = self.tq(*args)
        self.assertIn("2 new no-crash tests: confirm each belongs", output.splitlines())
        payload = json.loads(self.tq(*args, "--json"))
        self.assertEqual(payload["summary"], {"no_crash": 2})
        self.assertEqual([row["rule"] for row in payload["findings"]], ["no-crash"])
        self.assertEqual([row["rule"] for row in payload["suppressed"]], ["no-crash"])

    def check_rules(self, path, wanted, engine="ast-grep"):
        payload = json.loads(self.tq("lint", "--engine", engine, "--json", path, code=(0, 1)))
        rows = payload["findings"]
        self.assertEqual([(row["line"], row["rule"]) for row in rows], wanted)
        self.assertTrue(all(row["level"] == "advisory" and "result" in row["message"]
                            for row in rows if row["rule"] == "no-crash"))
