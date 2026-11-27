from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tq_rules import ast_grep_lint, ast_grep_ts, lint

FIXTURES = Path(__file__).parent / "fixtures" / "tq" / "holdout"


class HoldoutH5Tests(unittest.TestCase):
    def _rust_hits_by_test(self, path: Path) -> dict[str, set[str]]:
        by_test: dict[str, set[str]] = {}
        for _, tests, *_ in lint.scan_paths([path.resolve()], [], parser=ast_grep_lint.parse):
            for test in tests:
                rules = {rule for _, rule in test.issues}
                if test.guarded:
                    rules.add("guarded-assert")
                by_test[test.name] = rules
        return by_test

    def _ts_hits_by_test(self, path: Path) -> dict[str, set[str]]:
        resolved = path.resolve()
        tests, _ = ast_grep_ts.parse(resolved.read_text(), resolved, "typescript")
        by_test: dict[str, set[str]] = {}
        for test in tests:
            rules = {rule for _, rule in test.issues}
            if test.guarded:
                rules.add("guarded-assert")
            by_test[test.name] = rules
        return by_test

    def test_rust_holdout_false_forms_and_real_controls(self):
        by_test = self._rust_hits_by_test(FIXTURES / "rust_h5.rs")

        # False forms must be clean for their target gating rules.
        self.assertNotIn("guarded-assert", by_test.get("sample_iter_take_and_zip_unbounded", set()))
        self.assertNotIn("guarded-assert", by_test.get("nested_inclusive_len_loops", set()))
        self.assertNotIn("guarded-assert", by_test.get("retry_until_ok_loop", set()))
        self.assertNotIn("guarded-assert", by_test.get("while_poll_panic_check", set()))
        self.assertNotIn("guarded-assert", by_test.get("let_else_continue_static_slice", set()))
        self.assertNotIn("guarded-assert", by_test.get("every_match_arm_checks", set()))

        self.assertNotIn("no-assert", by_test.get("unwrap_err_and_expect_err", set()))
        self.assertNotIn("no-assert", by_test.get("select_macro_panic", set()))
        self.assertNotIn("no-assert", by_test.get("local_fn_alias", set()))
        self.assertNotIn("no-assert", by_test.get("where_clause_generic_assert", set()))
        self.assertNotIn("no-assert", by_test.get("impl_associated_helper", set()))

        self.assertNotIn("self-compare", by_test.get("distinct_turbofish_layout", set()))
        self.assertNotIn("self-compare", by_test.get("snapshot_mutated_through_assert", set()))
        self.assertNotIn("self-compare", by_test.get("cloned_tokens_are_considered_equal", set()))

        self.assertNotIn("absence-no-control", by_test.get("helper_with_flat_map_and_arg", set()))

        # Negative controls must stay flagged.
        self.assertIn("guarded-assert", by_test.get("real_unguarded_dynamic_loop", set()))
        self.assertIn("no-assert", by_test.get("real_only_logs", set()))
        self.assertIn("no-assert", by_test.get("real_harness_qex_unguarded", set()))
        self.assertIn("no-assert", by_test.get("real_build_fixture_unguarded", set()))
        self.assertIn("self-compare", by_test.get("real_self_eq", set()))
        self.assertIn("absence-no-control", by_test.get("real_absence_only", set()))

    def test_rust_cross_file_crate_common_helper(self):
        crate = FIXTURES / "rust_crate"
        by_test = self._rust_hits_by_test(crate)
        self.assertNotIn("no-assert", by_test.get("fish_example", set()))

    def test_ts_playwright_reexports_waits_and_exec(self):
        pw = self._ts_hits_by_test(FIXTURES / "ts_h5" / "holdout_pw.spec.ts")
        self.assertNotIn("no-assert", pw.get("pw wrapper expect", set()))
        self.assertNotIn("no-assert", pw.get("playwright waits", set()))
        self.assertIn("no-assert", pw.get("real missing assert after waitForSelector", set()))
        self.assertIn("no-assert", pw.get("real missing assert", set()))

        cjs = self._ts_hits_by_test(FIXTURES / "ts_h5" / "holdout_cjs.spec.ts")
        self.assertNotIn("no-assert", cjs.get("pw cjs expect", set()))

        npm = self._ts_hits_by_test(FIXTURES / "ts_h5" / "holdout_npm.spec.ts")
        self.assertNotIn("no-assert", npm.get("exec fixture runs and checks exit status", set()))

    def test_ts_sql_transaction_call_client_does_not_crash(self):
        sql_test = FIXTURES / "ts_h5" / "holdout_sql.spec.ts"
        by_test = self._ts_hits_by_test(sql_test)
        self.assertIn("effect sql call client transaction", by_test)

    def test_ts_vitest_gating_false_forms_and_real_controls(self):
        vt = self._ts_hits_by_test(FIXTURES / "ts_h5" / "holdout_vitest.spec.ts")

        self.assertNotIn("no-assert", vt.get("type level expectTypeOf and assertType", set()))
        self.assertNotIn("const-truth", vt.get("type level expectTypeOf and assertType", set()))
        self.assertNotIn("no-assert", vt.get("type level assertEqual", set()))
        self.assertNotIn("const-truth", vt.get("type level assertEqual", set()))
        self.assertNotIn("no-assert", vt.get("type error comment", set()))
        self.assertNotIn("no-assert", vt.get("untilBrowserLogAfter wait", set()))
        self.assertNotIn("no-assert", vt.get("untilConsoleLogAfter callback", set()))
        self.assertNotIn("no-assert", vt.get("untilConsoleLogAfter target array", set()))

        self.assertNotIn("guarded-assert", vt.get("let array table", set()))
        self.assertNotIn("guarded-assert", vt.get("nested png loop", set()))
        self.assertNotIn("guarded-assert", vt.get("does not update loop", set()))
        self.assertIn("guarded-assert", vt.get("real unguarded loop", set()))

        self.assertNotIn("absence-no-control", vt.get("object keys baseline", set()))
        self.assertNotIn("absence-no-control", vt.get("strips artifacts", set()))
        self.assertNotIn("absence-no-control", vt.get("last tool message", set()))
        self.assertIn("absence-no-control", vt.get("both in role loop unguarded", set()))
        self.assertIn("no-assert", vt.get("real imported setup helper return value assigned", set()))


if __name__ == "__main__":
    unittest.main()
