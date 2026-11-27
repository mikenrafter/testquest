import importlib.util
import json
import os

from test_tq import ROOT, RULES, TqCase


class ReworkTests(TqCase):
    def test_catalogue_examples(self):
        # WHY: an example that misses its rule or still triggers lint teaches the wrong fix.
        # catches: shipping untested examples or no recipe for a language
        has_ast_grep = importlib.util.find_spec("ast_grep_py") is not None
        has_literal_port = (ROOT / "tq_rules" / "ast_grep_literals.py").is_file()
        rendered = set()
        specialized = {
            "implementation-selector": ("ts",),
            "misordered-expected": ("java",),
            "no-crash": ("py", "rs", "ts", "dart"),
            "swallowed-assertion": ("py", "ts"),
            "unawaited-navigation": ("ts",),
        }
        for rule in sorted(RULES - {"invalid-suppression", "parse-error", "too-deep"}):
            languages = specialized.get(rule, ("py", "rs", "ts"))
            for language in languages:
                with self.subTest(rule=rule, language=language):
                    output = self.tq("rules", rule, "--lang", language)
                    recipe, examples = output.split("Bad example\n")
                    bad, good = examples.split("Reworked example\n")
                    self.assertIn("Rework\n  1. ", recipe)
                    self.assertIn("\n  2. ", recipe)
                    self.assertIn("Review\n  Reject ", recipe)
                    rendered.add((rule, language))
                    name = f"test_recipe.{language}"
                    engines = ([] if language in {"ts", "dart", "java"}
                               or rule in {"micro-snapshot", "swallowed-assertion"} else ["legacy"])
                    if has_ast_grep and (language != "dart" or os.getenv("TQ_DART_GRAMMAR")):
                        engines.append("ast-grep")
                    for engine in engines:
                        with self.subTest(engine=engine):
                            args = ("--engine", engine, "--json", name)
                            self.write(name, good)
                            self.assertEqual(json.loads(self.tq("lint", *args)),
                                             {"findings": [], "suppressed": []})
                            if rule == "missing-witness":
                                self.assertEqual(json.loads(self.tq("witness", *args)), [])
                            if engine == "ast-grep":
                                if rule == "self-derived-expected":
                                    continue
                                if rule == "nondistinguishing-literal" and (language == "ts" or not has_literal_port):
                                    continue
                            self.write(name, bad)
                            command = "witness" if rule == "missing-witness" else "lint"
                            exit_code = 0 if rule in {
                                "micro-snapshot", "misordered-expected", "no-crash", "nondistinguishing-literal",
                                "self-derived-expected", "swallowed-assertion", "implementation-selector",
                                "unawaited-navigation",
                            } else 1
                            rows = json.loads(self.tq(command, *args, code=(exit_code,)))
                            if command == "lint":
                                self.assertEqual(rows["suppressed"], [])
                                rows = rows["findings"]
                            self.assertIn(rule, [row["rule"] for row in rows])

        self.assertEqual(len(rendered), 36)

    def test_suppressions_partition_findings_and_preserve_reason(self):
        # WHY: one allow moves one matched row and preserves its location and reason.
        # catches: dropping the match, accepting a weak reason, or transferring an allow
        path = self.write("test_rules.py", """
            def test_fixtures():
                # tq: allow const-truth Exercises the matcher setup
                assert 2 == 2
                assert True  # tq: allow const-truth known
                assert True  # tq: allow const-truth CONST TRUTH const-truth
                assert check() or True  # tq: allow const-truth Exercises the matcher setup
                assert True
                assert read() == 4  # tq: allow self-compare Exercises the matcher setup
            """)
        engines = ["legacy"]
        if importlib.util.find_spec("ast_grep_py"):
            engines.append("ast-grep")
        for engine in engines:
            with self.subTest(engine=engine):
                args = ("lint", "--engine", engine, path.name)
                result = json.loads(self.tq(*args, "--json", code=(1,)))
                self.assertEqual([(row["line"], row["rule"]) for row in result["findings"]], [
                    (3, "self-compare"), (4, "const-truth"), (4, "invalid-suppression"),
                    (5, "const-truth"), (5, "invalid-suppression"), (7, "const-truth"),
                ])
                self.assertEqual([(row["line"], row["rule"], row["reason"])
                                  for row in result["suppressed"]], [
                    (3, "const-truth", "Exercises the matcher setup"),
                    (6, "const-truth", "Exercises the matcher setup"),
                ])
                output = self.tq(*args, code=(1,)).splitlines()
                self.assertTrue(output[1].lstrip().startswith("fix: "))
                self.assertIn("tq rules self-compare", output[1])
                self.assertEqual(output[-1], "Suppressed 2 findings.")
                fix = result["findings"][0]["fix"]
                self.assertEqual(output[1], f"  fix: {fix}")
                self.assertEqual(fix, "Keep the observed result on one side of the comparison. "
                                      "See `tq rules self-compare`.")

    def test_ledger_keeps_suppressed_matches_and_counts_distinct_allows(self):
        # WHY: repeated scans should preserve lint evidence without inflating the allow count.
        # catches: recording only visible findings or counting suppressed observations as allows
        self.git("init", "-q")
        self.write("README", "fixture")
        self.commit("Base")
        self.git("remote", "add", "origin", "https://example.test/account/fixture.git")
        self.write("test_cases.py", """
            # tq: allow no-assert A strict parser rejects invalid output
            def test_parser():
                parse(render())
        """)
        local, shared = self.scratch / "local.jsonl", self.scratch / "shared.jsonl"
        args = ("log", "--ledger", local, "--global-ledger", shared)
        for _ in range(2):
            scan = json.loads(self.tq(*args, "lint", "--since", "HEAD", "--json"))
            self.assertEqual(scan["findings"], [])
            row, = scan["suppressed"]
            self.assertEqual((row["rule"], row["reason"]),
                             ("no-assert", "A strict parser rejects invalid output"))
        events = [json.loads(line) for line in local.read_text().splitlines()]
        self.assertEqual([(event["row"], event["reason"]) for event in events],
                         [(row["row"], row["reason"])] * 2)
        local_report = json.loads(self.tq(*args, "report", "--json"))
        self.assertEqual(local_report["allows"], {"no-assert": 1})

        self.repo = self.scratch / "second"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("README", "fixture")
        self.commit("Base")
        self.git("remote", "add", "origin", "https://example.test/account/second.git")
        if importlib.util.find_spec("ast_grep_py"):
            self.write("port.test.ts", """
                // tq: allow no-assert A strict parser rejects invalid output
                test("parses output", () => parse(render()));
                test("matcher plumbing", () => {
                  assert(true); // tq: allow const-truth Exercises the matcher setup
                });
            """)
            engine = "ast-grep"
        else:
            self.write("test_cases.py", """
                # tq: allow no-assert A strict parser rejects invalid output
                def test_parser():
                    parse(render())

                def test_matcher_plumbing():
                    assert True  # tq: allow const-truth Exercises the matcher setup
            """)
            engine = "legacy"
        self.tq(*args, "lint", "--since", "HEAD", "--engine", engine)
        global_report = json.loads(self.tq(*args, "report", "--global", "--json"))
        self.assertEqual(global_report["allows"], {"const-truth": 1, "no-assert": 2})
        self.assertEqual({name: report["allows"] for name, report in global_report["repositories"].items()}, {
            "https://example.test/account/fixture": {"no-assert": 1},
            "https://example.test/account/second": {"const-truth": 1, "no-assert": 1},
        })
        self.assertIn("no-assert  2", self.tq(*args, "report", "--global"))
