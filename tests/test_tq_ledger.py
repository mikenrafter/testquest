import json
import os
import shlex
import subprocess
import sys

from test_tq import ROOT, TqCase


class LedgerTests(TqCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.write("README", "fixture")
        self.commit("Base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.ledger = self.scratch / "ledger.jsonl"

    def log(self, *args, **kwargs):
        kwargs.setdefault("env", self.config_env())
        return self.tq("log", "--ledger", self.ledger, *args, **kwargs)

    def events(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def report(self):
        return json.loads(self.log("report", "--json"))

    def test_since_uses_worktree_and_filters_by_test_span(self):
        # catches: selecting all rows in touched files or scanning only committed/indexed content
        self.write("test_local.py", """
            def test_unchanged():
                assert True

            def test_modified():
                assert True
                verify_extra()

            def test_staged_only():
                assert True
        """)
        self.commit("Add tests")
        path = self.repo / "test_local.py"
        source = path.read_text()
        path.write_text(source.replace("def test_staged_only():", "def test_renamed():"))
        self.git("add", str(path))
        path.write_text(source.replace("    verify_extra()\n", ""))
        self.write("test_untracked.py", "def test_untracked():\n    assert True\n")
        rows = self.findings("lint", "--since", "HEAD")
        self.assertEqual([(path, line, rule) for path, line, rule, _ in rows], [
            ("test_local.py", 5, "const-truth"), ("test_untracked.py", 2, "const-truth"),
        ])
        self.assertEqual(len(self.findings("lint", "--since", "HEAD", "test_local.py")), 1)
        self.assertEqual(len(self.findings("lint", "test_local.py")), 3)

    def test_record_tracks_bodies_across_renames_and_leaves_worktree_intact(self):
        # catches: counting a rename as death and birth, counting other tests as changed,
        # and comparing HEAD to the dirty worktree instead of comparing commit snapshots
        self.write("test_before.py", """
            def test_original():
                # catches: CLAIM(damaged-entry) an unchecked entry
                assert first()
                assert second()

            def test_neighbor():
                assert neighbor()
        """)
        self.git("add", "-A")
        self.git("commit", "-qm", "Add two tests", "-m",
                 "Test-Phase: red\nCo-Authored-By: Review Model <review@example.test>")
        self.git("mv", "test_before.py", "test_after.py")
        path = self.repo / "test_after.py"
        path.write_text(path.read_text().replace("test_original", "test_renamed"))
        self.commit("Rename test and file")
        path.write_text(path.read_text().replace("    assert second()\n", ""))
        self.git("add", "-A")
        self.git("commit", "-qm", "Drop one check", "-m", "Test-Phase: review-fix")
        path.write_text(path.read_text().split("\n\ndef test_neighbor")[0] + "\n")
        self.commit("Drop neighbor")
        path.write_text("def test_dirty():\n    pass\n")
        self.git("add", str(path))
        self.write("test_scratch.py", "def test_untracked():\n    pass\n")
        status = self.git("status", "--porcelain")
        self.log("record", "--since", self.base)
        original = self.ledger.read_bytes()
        self.log("record", "--since", self.base)
        self.assertEqual(self.ledger.read_bytes(), original)
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(path.read_text(), "def test_dirty():\n    pass\n")
        events = self.events()
        born = [e for e in events if e["event"] == "born"]
        self.assertEqual({e["test"] for e in born}, {
            "test_before.py::test_original", "test_before.py::test_neighbor",
        })
        self.assertEqual({e["phase"] for e in born}, {"red"})
        primary = next(e for e in born if e["test"].endswith("test_original"))
        self.assertEqual(primary["author"], "Tq Tests")
        self.assertEqual(primary["co_authors"], ["Review Model"])
        self.assertEqual(primary["claim"], "damaged-entry")
        self.assertEqual(primary["catches"], ["CLAIM(damaged-entry) an unchecked entry"])
        changed = [e for e in events if e.get("loosened")]
        self.assertEqual([(e["test"], e["checks_before"], e["checks_after"], e["phase"]) for e in changed],
                         [("test_after.py::test_renamed", 2, 1, "review-fix")])
        self.assertEqual(changed[0]["test_id"], primary["test_id"])
        self.assertEqual([e["test"] for e in events if e["event"] == "died"],
                         ["test_after.py::test_neighbor"])
        self.assertEqual(self.report()["phases"]["red"], {"born": 2, "died": 1, "survived": 1})

    def test_lint_verdict_and_import_compute_precision_by_rule_version(self):
        # catches: counting intended verdicts in loose precision, or attributing older
        # cache judgments to the current rules by the stable row id alone
        self.write("test_local.py", """
            def test_literal():
                assert True
        """)
        self.log("lint", "--since", self.base, code=(1,))
        row, = self.events()
        self.assertEqual((row["event"], row["rule"], row["test"]),
                         ("lint", "const-truth", "test_local.py::test_literal"))
        self.assertTrue(row["rule_version"])
        self.log("verdict", "--row", row["row"], "--verdict", "intended", "--why",
                 "Checks the test runner", "--pattern", "runner", "--judge", "reviewer")
        cache = self.scratch / "cache.jsonl"
        entries = [
            dict(repo="repo", path="test_local.py", rule="const-truth", hash="body-" + judgment,
                 offset=1, line=2, test="test_literal", verdict=judgment,
                 why="Fixture judgment", pattern=None, judge="reviewer", round="r0")
            for judgment in ["real", "false", "intended"]
        ]
        cache.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        self.log("import-verdicts", cache)
        recorded = self.ledger.read_bytes()
        self.log("import-verdicts", cache)
        self.assertEqual(self.ledger.read_bytes(), recorded)
        by_version = self.report()["precision"]["const-truth"]
        self.assertEqual(by_version[row["rule_version"]]["loose"], None)
        self.assertEqual(by_version["dd4d81b"]["strict"], 1 / 3)
        self.assertEqual(by_version["dd4d81b"]["loose"], 1 / 2)

    def test_bad_import_preserves_existing_bytes(self):
        # catches: appending half an import before detecting an invalid cache line
        self.write("test_local.py", "def test_new():\n    assert True\n")
        self.log("lint", "--since", self.base, code=(1,))
        original = self.ledger.read_bytes()
        cache = self.scratch / "cache.jsonl"
        cache.write_text(json.dumps(dict(repo="repo", path="cases.py", rule="no-assert", hash="body",
                                         offset=0, line=1, test="test_missing", verdict="real",
                                         why="An empty test", pattern=None, judge="reviewer",
                                         round="r0")) + "\n{")
        self.log("import-verdicts", cache, code=(2,))
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_kill_groups_tests_by_patch_and_reports_unique_kills(self):
        # catches: reporting two unique kills for one shared patch, or deleting
        # a test that failed a real run or never ran against a mutant
        self.write("payload.py", "value = 1234\n")
        self.write("fault.patch", """
            --- a/payload.py
            +++ b/payload.py
            @@ -1 +1 @@
            -value = 1234
            +value = 34
        """)
        self.write("test_payload.py", """
            from payload import value

            def test_full_word():
                # catches: truncate the output [patch: fault.patch]
                assert value == 1234

            def test_lower_digits():
                # catches: truncate the output [patch: fault.patch]
                assert value % 100 == 34

            def test_also_full_word():
                # catches: truncate the output [patch: fault.patch]
                assert value > 1000

            def test_never_replayed():
                assert value > 0
        """)
        self.commit("Add patch and tests")
        self.log("record", "--since", self.base)
        self.write("test_runner.py", "raise RuntimeError('not part of HEAD')\n")
        command = f"""{shlex.quote(sys.executable)} -c "import runpy; ns = runpy.run_path('test_payload.py'); ns['{{test}}'.split('.')[-1]]()" """
        self.log("kill", "--cmd", command, code=(1,))
        kill, = [event for event in self.events() if event["event"] == "kill"]
        self.assertEqual(set(kill["red"]), {
            "test_payload.py::test_full_word", "test_payload.py::test_also_full_word",
        })
        self.assertEqual(kill["green"], ["test_payload.py::test_lower_digits"])
        with self.ledger.open("a") as stream:
            print(json.dumps(dict(event="run", id="run-1", repo="repo",
                                  test="test_payload.py::test_also_full_word", status="failed")), file=stream)
        report = self.report()
        self.assertEqual(report["tests"]["test_payload.py::test_full_word"]["unique_kills"], 0)
        self.assertEqual(report["deletion_candidates"], [
            "test_payload.py::test_full_word", "test_payload.py::test_lower_digits",
        ])

    def test_default_ledger_lives_in_main_worktree(self):
        # WHY: linked worktrees must share a ledger without sharing the history they inspect.
        # catches: anchoring the default at the caller's current directory or linked worktree
        self.write("src/test_added.py", "def test_added():\n    assert actual() == 19\n")
        self.commit("Add test")
        main = self.repo
        linked = self.scratch / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        self.repo = linked
        self.write("src/test_linked.py", "def test_linked():\n    assert actual() == 42\n")
        self.commit("Add linked worktree test")
        self.repo = linked / "src"
        self.tq("log", "record", "--since", self.base, env=self.config_env())
        path = main / ".tq" / "ledger.jsonl"
        self.assertEqual([entry["test"] for entry in self.read_ledger(path)],
                         ["src/test_added.py::test_added", "src/test_linked.py::test_linked"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def config_env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if key not in {"TQ_LEDGER", "TQ_GLOBAL_LEDGER"}}
        return env | {"XDG_CONFIG_HOME": str(self.scratch / "config"),
                      "HOME": str(self.scratch)} | overrides

    def write_global_config(self, config):
        path = self.scratch / "config" / "tq" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config)
        return path

    def read_ledger(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def where(self, *args, **env):
        return json.loads(self.tq("log", "where", "--json", *args, env=self.config_env(**env)))

    def test_ledger_config_resolves_paths_per_key_with_provenance(self):
        # WHY: each ledger can be configured independently at every precedence level.
        # catches: replacing the whole TOML table or choosing the environment over flags
        global_config = self.write_global_config(
            '[ledger]\npath = "{parent}/{name}.research/tq/ledger.jsonl"\n'
            'global = "{home}/notes/tq/ledger.jsonl"\n')
        configured = {"path": str(self.scratch / "repo.research/tq/ledger.jsonl"),
                      "source": f"{global_config} [ledger].path"}
        shared = {"path": str(self.scratch / "notes/tq/ledger.jsonl"),
                  "source": f"{global_config} [ledger].global"}
        self.assertEqual(self.where(), {"ledger": configured, "global": shared})
        local_config = self.write(".tq/config.toml", '[ledger]\npath = "{root}/custom.jsonl"\n')
        local = {"path": str(self.repo / "custom.jsonl"),
                 "source": f"{local_config} [ledger].path"}
        self.assertEqual(self.where(), {"ledger": local, "global": shared})
        self.write(".tq/config.toml", '[ledger]\npath = "{root}/custom.jsonl"\nglobal = "none"\n')
        self.assertEqual(self.where(), {"ledger": local,
                                       "global": {"path": None, "source": f"{local_config} [ledger].global"}})
        self.assertEqual(self.where(TQ_LEDGER="env.jsonl", TQ_GLOBAL_LEDGER="~/shared.jsonl"),
                         {"ledger": {"path": str(self.repo / "env.jsonl"), "source": "TQ_LEDGER"},
                          "global": {"path": str(self.scratch / "shared.jsonl"), "source": "TQ_GLOBAL_LEDGER"}})
        self.assertEqual(self.where("--ledger", "cli.jsonl", "--global-ledger", "all.jsonl",
                                    TQ_LEDGER="env.jsonl", TQ_GLOBAL_LEDGER="env-all.jsonl"),
                         {"ledger": {"path": str(self.repo / "cli.jsonl"), "source": "--ledger"},
                          "global": {"path": str(self.repo / "all.jsonl"), "source": "--global-ledger"}})

    def test_ledger_config_rejects_unknown_entries(self):
        # WHY: typos in a shadowed config still need a diagnostic.
        # catches: ignoring a misspelled key or accepting access to fields outside the template
        for line, diagnostic in (('paht = "old.jsonl"', "unknown key ledger.paht"),
                                 ('path = "{root.name}/ledger.jsonl"', "unknown template field {root.name}"),
                                 ('global = 42', "ledger.global must be a nonempty string")):
            with self.subTest(line=line):
                config = self.write_global_config(f"[ledger]\n{line}\n")
                result = subprocess.run(
                    [sys.executable, "-I", str(ROOT / "tq"), "log", "--ledger", "override", "where"],
                    cwd=self.repo, env=self.config_env(), capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"{config} ", result.stderr)
                self.assertIn(diagnostic, result.stderr)

    def test_global_record_keeps_same_ids_from_distinct_repositories(self):
        # WHY: repositories with identical histories and directory names can have the same local event ID.
        # catches: dropping the second repository under contention by deduplicating on id alone
        from concurrent.futures import ThreadPoolExecutor
        self.write("test_local.py", "def test_new():\n    assert actual() == 6\n")
        self.commit("Add test")
        shared = self.scratch / "global.jsonl"
        self.write_global_config(f'[ledger]\npath = "{{root}}/.tq/ledger.jsonl"\nglobal = "{shared}"\n')
        main = self.repo
        second = self.scratch / "other" / main.name
        self.git("clone", "--quiet", str(main), str(second))
        origins = ("https://example.test/team-a/repo", "https://example.test/team-b/repo")
        for checkout, origin in zip((main, second), origins):
            self.repo = checkout
            self.git("remote", "set-url" if checkout == second else "add", "origin",
                     origin.replace("example.test", "EXAMPLE.TEST") + ".git/")
        command = [sys.executable, "-I", str(ROOT / "tq"), "log", "record", "--since", self.base]
        with ThreadPoolExecutor(4) as workers:
            runs = [workers.submit(subprocess.run, command, cwd=checkout, env=self.config_env(),
                                   capture_output=True, text=True, timeout=10)
                    for checkout in (main, second, main, second)]
            for run in runs:
                result = run.result()
                self.assertEqual(result.returncode, 0, result.stderr)
        first, = self.read_ledger(main / ".tq" / "ledger.jsonl")
        second_local, = self.read_ledger(second / ".tq" / "ledger.jsonl")
        self.assertEqual(first, second_local)
        self.assertCountEqual(self.read_ledger(shared),
                              [first | {"repo": origin, "repo_name": "repo"} for origin in origins])
        report = json.loads(self.tq("log", "report", "--global", "--json", env=self.config_env()))
        self.assertEqual(report["phases"]["unknown"]["born"], 2)
        self.assertEqual({origin: row["phases"]["unknown"]["born"]
                          for origin, row in report["repositories"].items()}, dict.fromkeys(origins, 1))
        only = json.loads(self.tq("log", "report", "--global", "--repo", origins[1] + ".git",
                                 "--json", env=self.config_env()))
        self.assertEqual(set(only["repositories"]), {origins[1]})
        command = [sys.executable, "-I", str(ROOT / "tq"), "log", "report", "--global",
                   "--repo", "repo", "--json"]
        by_name = subprocess.run(command, cwd=self.scratch, env=self.config_env(),
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(by_name.returncode, 0, by_name.stderr)
        self.assertEqual(json.loads(by_name.stdout), report)

    def test_global_only_keeps_history_and_accepts_verdicts(self):
        # WHY: a single global ledger must supply the prior rows and history for writes.
        # catches: turning every write into a no-op when the local path is disabled
        self.write_global_config('[ledger]\npath = "none"\nglobal = "~/all.jsonl"\n')
        env = self.config_env()
        self.write("test_local.py", "def test_new():\n    assert True\n")
        self.commit("Add test")
        self.tq("log", "record", "--since", self.base, env=env)
        born, = self.read_ledger(self.scratch / "all.jsonl")
        self.git("rm", "test_local.py")
        after_birth = self.git("rev-parse", "HEAD").strip()
        self.commit("Remove test")
        self.tq("log", "record", "--since", after_birth, env=env)
        self.write("test_lint.py", "def test_lint():\n    assert True\n")
        row, = json.loads(self.tq("log", "lint", "--since", "HEAD", "--json", env=env, code=(1,)))
        self.tq("log", "verdict", "--row", row["row"], "--verdict", "real", "--why", "Constant",
                env=env)
        events = self.read_ledger(self.scratch / "all.jsonl")
        self.assertEqual([entry["event"] for entry in events], ["born", "died", "lint", "verdict"])
        self.assertEqual(events[1]["test_id"], born["test_id"])
        self.assertEqual({entry["repo"] for entry in events}, {str(self.repo)})
        self.assertFalse((self.repo / ".tq").exists())

    def test_global_append_failure_preserves_local_then_retries(self):
        # WHY: a failed global append must leave the first write available to retry.
        # catches: writing globally first or duplicating local events when the global write recovers
        self.write("test_local.py", "def test_new():\n    assert observed() == 6\n")
        self.commit("Add test")
        shared = self.scratch / "global.jsonl"
        shared.mkdir()
        command = [sys.executable, "-I", str(ROOT / "tq"), "log", "record",
                   "--global-ledger", str(shared), "--since", self.base]
        result = subprocess.run(command, cwd=self.repo, env=self.config_env(),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"global ledger {shared}", result.stderr)
        path = self.repo / ".tq" / "ledger.jsonl"
        recorded = path.read_bytes()
        first, = self.read_ledger(path)
        shared.rmdir()
        self.tq("log", "record", "--global-ledger", shared, "--since", self.base, env=self.config_env())
        self.assertEqual(path.read_bytes(), recorded)
        self.assertEqual(self.read_ledger(shared), [first | {"repo": str(self.repo), "repo_name": "repo"}])

    def test_concurrent_record_adds_each_event_once(self):
        # catches: checking for duplicate keys before taking the append lock
        from concurrent.futures import ThreadPoolExecutor
        self.write("test_local.py", "def test_new():\n    assert True\n")
        self.commit("Add test")
        with ThreadPoolExecutor(4) as workers:
            runs = [workers.submit(self.log, "record", "--since", self.base) for _ in range(4)]
            for run in runs:
                run.result()
        event, = self.events()
        self.assertEqual((event["event"], event["test"]), ("born", "test_local.py::test_new"))

    def test_append_preserves_a_valid_last_line_without_newline(self):
        # catches: joining a complete existing JSON object to the first new event
        self.write("test_local.py", "def test_new():\n    assert True\n")
        self.commit("Add test")
        existing = b'{"event":"run","id":"external-1","repo":"repo","test":"test_local.py::test_new","status":"passed"}'
        self.ledger.write_bytes(existing)
        self.log("record", "--since", self.base)
        self.assertTrue(self.ledger.read_bytes().startswith(existing + b"\n"))
        self.assertEqual([event["event"] for event in self.events()], ["run", "born"])

    def test_backfill_preserves_birth_phase_and_rename_lineage(self):
        # catches: reporting an already-dead test as surviving when its birth is backfilled
        self.write("test_local.py", "def test_first():\n    assert True\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "Add test", "-m", "Test-Phase: green")
        after_birth = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "test_local.py", "test_renamed.py")
        self.commit("Rename")
        self.git("rm", "test_renamed.py")
        self.commit("Remove")
        self.log("record", "--since", after_birth)
        self.log("record", "--since", self.base)
        self.assertEqual(self.report()["phases"], {"green": {"born": 1, "died": 1, "survived": 0}})

    def test_file_rename_keeps_lineage_for_identical_bodies(self):
        # catches: discarding a known Git file move when distinct tests have identical bodies
        self.write("test_local.py", """
            def test_first():
                assert True

            def test_second():
                assert True
        """)
        self.commit("Add tests")
        self.git("mv", "test_local.py", "test_renamed.py")
        self.commit("Rename file")
        self.log("record", "--since", self.base)
        self.assertEqual([event["event"] for event in self.events()], ["born", "born", "changed", "changed"])
        for old, new in zip(self.events()[:2], self.events()[2:]):
            self.assertEqual(old["test_id"], new["test_id"])

    def test_record_does_not_count_a_merged_test_twice(self):
        # catches: treating a branch import as a second birth of the same test
        main = self.git("branch", "--show-current").strip()
        self.git("switch", "-qc", "branch")
        self.write("test_branch.py", "def test_branch():\n    assert True\n")
        self.commit("Add test on the branch")
        self.git("switch", "-q", main)
        self.write("unrelated", "main branch")
        self.commit("Advance main")
        self.git("merge", "--no-ff", "-q", "branch")
        self.log("record", "--since", self.base)
        self.assertEqual([entry["test"] for entry in self.events() if entry["event"] == "born"],
                         ["test_branch.py::test_branch"])

    def test_record_attributes_same_line_rust_change_to_one_test(self):
        # catches: marking any test on the changed source line as edited
        self.write("cases.rs", "#[test] fn first() { assert!(foo()); } #[test] fn second() { assert!(bar()); }")
        self.commit("Add compact Rust tests")
        self.write("cases.rs", "#[test] fn first() { assert!(foo2()); } #[test] fn second() { assert!(bar()); }")
        self.commit("Edit only first")
        self.log("record", "--since", self.base)
        self.assertEqual([entry["test"] for entry in self.events() if entry["event"] == "changed"],
                         ["cases.rs::first"])

    def test_strict_precision_counts_unjudged_rows_once(self):
        # catches: counting re-lints as new evidence or dropping unjudged findings from strict precision
        self.write("cases.py", """
            def test_two_observations():
                assert True
                assert not False
        """)
        rows = json.loads(self.log("lint", "--since", self.base, "--json", code=(1,)))
        self.log("lint", "--since", self.base, code=(1,))
        self.assertEqual(len(rows), 2)
        self.log("verdict", "--row", rows[0]["row"], "--verdict", "real", "--why", "Cannot fail")
        report = self.report()["precision"]["const-truth"][rows[0]["rule_version"]]
        self.assertEqual((report["unjudged"], report["strict"], report["loose"]), (1, 0.5, 1))

    def test_corrupt_ledger_rejects_append_without_rewriting(self):
        # catches: skipping or replacing an incomplete preexisting record
        self.write("cases.py", "def test_new():\n    assert True\n")
        self.commit("Add test")
        original = b'{"event":"run","id":"old","repo":"repo"}\n{"event":"lit'
        self.ledger.write_bytes(original)
        self.log("record", "--since", self.base, code=(2,))
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_death_carries_kills_from_before_a_rename(self):
        # catches: counting a dead test's kills by its final pathname alone
        self.write("test_original.py", "def test_case():\n    assert matches()\n")
        self.commit("Add a test")
        self.log("record", "--since", self.base)
        born, = self.events()
        with self.ledger.open("a") as stream:
            print(json.dumps(dict(event="kill", id="observed-1", repo="repo", mutant="mutant-1",
                                  commit=born["commit"], red=[born["test"]], green=[],
                                  bodies={born["test"]: born["hash"]})), file=stream)
        self.git("mv", "test_original.py", "test_moved.py")
        self.commit("Move test")
        self.git("rm", "test_moved.py")
        self.commit("Remove test")
        self.log("record", "--since", self.base)
        death, = [entry for entry in self.events() if entry["event"] == "died"]
        self.assertEqual((death["test_id"], death["kills"]), (born["test_id"], 1))
        self.assertGreaterEqual(death["lifetime_seconds"], 0)
