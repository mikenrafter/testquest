"""Exercise ECMAScript test checks through the optional CLI engine."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from test_tq import FIXTURES, TqCase


@unittest.skipUnless(importlib.util.find_spec("ast_grep_py"), "ast-grep-py is optional")
class AstGrepTypeScript(TqCase):
    def rows(self, *args):
        return json.loads(self.tq("lint", "--engine", "ast-grep", "--json", *args, code=(0, 1)))["findings"]

    def fixture(self, name):
        return self.copy(FIXTURES / "ts" / name)

    def locations(self, *paths):
        return [(row["line"], row["rule"]) for row in self.rows(*paths)]

    def test_framework_discovery_and_callback_ownership(self):
        # WHY: registrations own their callback body, including curried each.
        # catches: attributing a nested test or skipped test's checks to its parent
        path = self.fixture("discovery.test.ts")
        self.assertEqual(self.locations(path.name), [(4, "no-assert"), (5, "no-assert"), (8, "no-assert"), (18, "no-assert")])

    def test_checks_and_comparisons(self):
        # WHY: checks vary by framework and only a proven tautology gets a finding.
        # catches: treating custom matcher arguments as equality operands
        path = self.fixture("checks.test.ts")
        self.assertEqual(self.locations(path.name), [
            (18, "no-assert"), (19, "self-compare"), (20, "self-compare"),
            (21, "const-truth"), (21, "self-compare"), (22, "const-truth"), (23, "self-compare"),
            (31, "self-compare"),
        ])

    def test_branch_coverage_and_iteration(self):
        # WHY: a known iteration count is insufficient when a break can skip its check.
        # catches: assuming that either one branch or a nonempty loop covers every path
        path = self.fixture("guards.test.ts")
        self.assertEqual(self.locations(path.name), [
            *((line, "guarded-assert") for line in (1, 3, 5, 6, 7, 8)),
            (8, "swallowed-assertion"),
            *((line, "guarded-assert") for line in (9, 11, 13, 14, 16)),
            (17, "no-assert"),
        ])

    def test_async_checks_require_completion(self):
        # WHY: waiting for a raw array leaves the promises produced by map detached.
        # catches: accepting an unrelated await or an array of promises as completion
        path = self.fixture("async.test.ts")
        self.assertEqual(self.locations(path.name), [(line, "guarded-assert") for line in (1, 4, 7, 9, 13, 14, 16)])

    def test_positive_control_is_on_the_same_recorder(self):
        # WHY: mock.calls and the spy are one recorder, but a conditional control can be skipped.
        # catches: allowing another spy or a guarded check to stand in for a positive control
        path = self.fixture("absence.test.ts")
        self.assertEqual(self.locations(path.name), [(line, "absence-no-control") for line in (3, 11, 20, 32)])

    def test_all_suffixes_and_ignored_directories(self):
        # WHY: generated and ignored files must not appear in a directory scan.
        # catches: walking generated output or applying the TypeScript grammar to JSX
        self.git("init", "-q")
        self.write(".gitignore", "ignored*.ts\n")
        for directory in ("node_modules", "dist", "build", "coverage"):
            self.write(f"{directory}/junk.test.ts", 'test("junk", () => {});')
        self.write("ignored.test.ts", 'test("ignored", () => {});')
        for suffix in ("ts", "tsx", "js", "jsx", "mjs", "cjs"):
            expression = "<div />" if suffix in {"tsx", "jsx"} else "work()"
            self.write(f"test.{suffix}", f'test("smoke", () => {expression});')
        rows = self.rows(".")
        self.assertEqual([(row["path"], row["rule"]) for row in rows], [
            (f"test.{suffix}", "no-assert") for suffix in ("cjs", "js", "jsx", "mjs", "ts", "tsx")
        ])
        self.assertEqual(json.loads(self.tq("lint", "--json", "."))["findings"], [])

    def test_notes_use_callback_ownership(self):
        # WHY: both line and block comments must reach the existing note consumers.
        # catches: scanning JavaScript for lint but excluding it from witness or catches
        path = self.fixture("notes.test.ts")
        self.assertEqual(self.locations(path.name), [(3, "guarded-assert"), (7, "guarded-assert")])
        witness = json.loads(self.tq("witness", "--engine", "ast-grep", "--json", path.name, code=(1,)))
        self.assertEqual([(row["line"], row["rule"]) for row in witness], [(7, "missing-witness")])
        self.git("init", "-q")
        self.commit("notes")
        replays = json.loads(self.tq("catches", "--engine", "ast-grep", "--repo", self.repo,
                                    "--cmd", f"{sys.executable} -V", "--json"))
        self.assertEqual([(row["test"], row["description"]) for row in replays], [
            ("reaches the branch", "wrong value"), ("multiline note", "throws the wrong error"),
            ("inner", "nested fault"),
        ])

    def test_since_includes_deleting_a_check_from_a_qualified_test(self):
        # WHY: the deleted line is outside the new callback span and the peer shares its short name.
        # catches: applying the old-side lookup by short test name or by Python/Rust suffix only
        self.git("init", "-q")
        source = '''describe("first", () => {
          test("shared", () => {
            consume();
            expect(out()).toBe(7);
          });
        });
        describe("second", () => { test("shared", () => consume()); });
        '''
        self.write("suite.ts", source)
        self.commit("guarded outcome")
        self.write("suite.ts", "".join(line for line in source.splitlines(keepends=True) if "expect" not in line))
        self.assertEqual(self.locations("--since", "HEAD"), [(2, "no-assert")])

    def test_joined_wrappers_credit_checks(self):
        # WHY: doctorRuntime.test.ts:215 and navigation.sources.test.ts:65 await their checks.
        # catches: skipping joined callbacks or crediting a dropped callback
        path = self.write("wrappers.test.ts", """
            test("awaited", async () => {
              await withVerifierCleanup(async () => { expect(selected()).toBe(true); });
            });
            test("returned", () => page.settled(() => expect(page.pw("source-detail")).toHaveLength(1)));
            test("dropped", () => { withVerifierCleanup(async () => expect(selected()).toBe(true)); });
            test("unchecked Result", async () => {
              await withSignatureTarget(target, async (binding) => {
                expect(binding.snapshotPath).toContain("copy");
                return verifySignatureTarget(binding);
              });
            });
            test("ignored by local wrapper", async () => {
              async function ignore(callback) {}
              await ignore(() => expect(selected()).toBe(true));
            });
            async function withClient(callback) { await callback(client); }
            test("local forwarding", () => withClient(async client => expect(client.ready).toBe(true)));
            async function start(callback) { callback(client); }
            test("local detached", () => start(async client => expect(client.ready).toBe(true)));
        """)
        self.assertEqual(self.locations(path.name), [
            (5, "no-assert"), (6, "no-assert"), (12, "no-assert"), (19, "guarded-assert"),
        ])

    def test_failure_helpers_and_fast_check(self):
        # WHY: capture.test.ts:87,257 and vitest.ts:17 fail through distinct check APIs.
        # catches: counting only expect matchers and unconditional throws
        path = self.write("helpers.test.ts", """
            import nodeAssert from "node:assert";
            function must(result) {
              if (!result.ok) nodeAssert.fail("invalid result");
              return result.value;
            }
            function requireValue(result) {
              if (!result.ok) throw new Error("invalid result");
            }
            test("must", () => must(validateObjectGraph(fixture())));
            test("throwing helper", () => requireValue(validateObjectGraph(fixture())));
            test("throw outcome", async () => {
              const thrown = outcomeToThrow(await runExample());
              if (thrown !== undefined) throw thrown;
            });
            test("property check", async () => {
              const fc = await import("fast-check");
              fc.assert(fc.property(fc.integer(), x => check(x)));
            });
            test("arbitrary method", () => product.assert());
            import { must as unwrap } from "./helpers/must.ts";
            function importAt(input) {
              clock.set();
              unwrap(importCollection(input));
              clock.reset();
            }
            test("empty result skips the only output check", () => {
              importAt(input);
              for (const item of listViews().items) expect(item.lastCapture).toBeNull();
            });
        """)
        self.assertEqual(self.locations(path.name), [(19, "no-assert"), (26, "guarded-assert")])

    def test_try_failure_requires_a_check_in_the_catch(self):
        # WHY: CdpRuntimeRequests.test.ts:47 fails closed if no exception arrives.
        # catches: overlooking expect.fail or treating a swallowed failure as coverage
        path = self.write("failure.test.ts", """
            test("checks refusal", () => {
              try { requests.result(); expect.fail("expected refusal"); }
              catch (cause) { expect(cause).toBeInstanceOf(ConstraintError); }
            });
            test("swallows refusal", () => {
              try { requests.result(); expect.fail("expected refusal"); }
              catch (cause) { console.log(cause); }
            });
        """)
        self.assertEqual(self.locations(path.name), [(5, "guarded-assert")])

    def test_fixed_iterations_and_skippable_product_checks(self):
        # WHY: fidelity-round.test.ts:63, graph.test.ts:23, and matrix-view.test.ts:153 use fixtures.
        # catches: treating test-owned inputs as optional or crediting optional product outputs
        path = self.write("loops.test.ts", """
            const SCENARIOS = ["shared-child", "cycle"];
            const seeds = count => Array.from({length: count}, (_, i) => 4217 + i * 7919);
            test("named fixtures", () => {
              for (const scenario of SCENARIOS) expect(loadFixture(scenario)).toBeDefined();
            });
            test("seed list", () => {
              const inputs = Array.from({length: 40}, (_, i) => 4217 + i * 7919);
              for (const seed of inputs) expect(out(seed)).toBeDefined();
              for (const seed of seeds(40)) expect(out(seed)).toBeDefined();
            });
            test("inserted DOM", () => {
              document.body.innerHTML = '<div class="row">seed</div>';
              for (const row of document.body.querySelectorAll(".row")) expect(change(row)).toBe(true);
            });
            test("empty fallback", () => {
              for (const shift of mod.nudgeLabels?.(boxes, 12) ?? []) expect(shift.delta).toBeGreaterThan(0);
            });
            test("not visible", async () => {
              if (await toggle.isVisible()) await expect(toggle).toBeEnabled();
            });
            test("different DOM selector", () => {
              document.body.innerHTML = '<div class="row">seed</div>';
              for (const row of document.body.querySelectorAll(".other")) expect(change(row)).toBe(true);
            });
            test("seed helper", () => {
              for (const seed of seeds(40)) expect(out(seed)).toBeDefined();
            });
            test("empty seed list", () => {
              for (const seed of seeds(0)) expect(out(seed)).toBeDefined();
            });
        """)
        self.assertEqual(self.locations(path.name), [(line, "guarded-assert") for line in (15, 18, 21, 28)])

    def test_absence_controls_require_positive_evidence_on_the_recorder(self):
        # WHY: import-parser.test.ts:15, ElectronCaptureCleanup.test.ts:196 and staging.test.ts:66
        # exercise paired outputs, sibling spy coverage and a prototype spy with no witness.
        # catches: overlooking sibling checks or crediting an unrelated recorder's activity
        path = self.write("controls.test.ts", """
            import * as ownership from "./ownership.js";
            test("parses good text", () => {
              const result = parseImportText(good);
              expect(result.items.length).toBeGreaterThan(0);
              expect(result.errors).toHaveLength(0);
              const item = result.items[0];
              expect(item.name).toBe("good");
            });
            test("reports bad text", () => {
              const invalid = parseImportText(bad);
              expect(invalid.errors).toEqual([{kind: "bad"}]);
            });
            test("does not probe for blank text", () => {
              const blank = parseImportText(empty);
              expect(blank.errors).toHaveLength(0);
              expect(blank.items).toHaveLength(0);
            });
            test("observes live lineage", () => {
              const observe = vi.spyOn(ownership, "observe");
              release(live);
              expect(observe).toHaveBeenCalledOnce();
            });
            test("skips unknown lineage", () => {
              const observe = vi.spyOn(ownership, "observe");
              const cleanup = vi.spyOn(ownership, "cleanup");
              release(unknown);
              expect(observe).not.toHaveBeenCalled();
              expect(cleanup).not.toHaveBeenCalled();
            });
            test("spy can miss prototype read", () => {
              const read = vi.spyOn(FileHandle.prototype, "read");
              const stat = vi.spyOn(FileHandle.prototype, "stat");
              expect(stat).toHaveBeenCalled();
              expect(read).not.toHaveBeenCalled();
            });
        """)
        self.assertEqual(self.locations(path.name), [(28, "absence-no-control"), (34, "absence-no-control")])

    def test_literal_only_checks_and_manual_inspection(self):
        # WHY: treeView.test.ts:104 checks a fixture, and gutter-comments.spec.ts:68,140 check no result.
        # catches: treating literal data, logs or screenshots as verification of product output
        path = self.write("smoke.test.ts", """
            const makeGroup = overrides => ({files: [stub], ...overrides});
            test("checks a literal", () => {
              const group = makeGroup({files: []});
              expect(group.files).toHaveLength(0);
            });
            test("checks product output", () => {
              const group = buildGroup({files: []});
              expect(group.files).toHaveLength(0);
            });
            test("logs", async () => {
              console.log(await page.evaluate(readGlyph));
              await page.screenshot({path: "glyph.png"});
            });
            test("screenshot only", async () => {
              if (await glyph.isVisible()) await page.screenshot({path: "hover.png"});
            });
        """)
        self.assertEqual(self.locations(path.name), [
            (4, "const-truth"), (8, "absence-no-control"), (10, "no-assert"), (14, "no-assert"),
        ])

    def test_browser_element_assertions_must_finish(self):
        # WHY: ChatMarkdown.browser.test.tsx awaits the locator matcher.
        # catches: overlooking expect.element or treating the locator lookup as a check
        path = self.write("browser.test.ts", """
            import { expect as verify } from "vitest";
            test("checks the locator", async () => {
              await verify.element(screen.getByText("Saved")).toBeVisible();
            });
            test("looks up but never checks", () => verify.element(screen.getByText("Saved")));
            test("drops the check", () => {
              verify.element(screen.getByText("Saved")).toHaveAttribute("role", "status");
            });
        """)
        self.assertEqual(self.locations(path.name), [(5, "no-assert"), (6, "guarded-assert")])

    def test_recorder_controls_follow_factory_declarations_and_member_aliases(self):
        # WHY: stream.test.ts destructures setup events, and clearRuntime.test.ts aliases fake methods.
        # catches: comparing source spelling or accepting a different recorder's calls
        path = self.write("recorders.test.ts", """
            function setup() {
              const events = { unauthorized: vi.fn(), pending: vi.fn() };
              configure(events);
              return { events };
            }
            describe("fixture members", () => {
              let fixture;
              beforeEach(() => { fixture = setup(); });
              test("positive control", () => {
                const unauthorized = fixture.events.unauthorized;
                expect(unauthorized.mock.calls.length).toBeGreaterThan(0);
              });
              test("same factory, separately created", () => {
                const { events } = setup();
                expect(events.unauthorized).not.toHaveBeenCalled();
                expect(events.pending).not.toHaveBeenCalled();
              });
            });
            test("independent local with the same name", () => {
              const events = { unauthorized: vi.fn() };
              expect(events.unauthorized).not.toHaveBeenCalled();
            });
            test("control later in the same test", () => {
              const scroll = vi.fn();
              expect(scroll).not.toHaveBeenCalled();
              expect(scroll.mock.calls).toEqual([[616]]);
            });
            test("guarded control is insufficient", () => {
              const scroll = vi.fn();
              expect(scroll).not.toHaveBeenCalled();
              if (enabled()) expect(scroll.mock.calls).toEqual([[616]]);
            });
        """)
        self.assertEqual(self.locations(path.name), [
            (16, "absence-no-control"), (21, "absence-no-control"), (30, "absence-no-control"),
        ])

    def test_imported_assertions_require_repo_source(self):
        # WHY: Optic.test.ts and Record.test.ts call assertion wrappers from source.
        # catches: ignoring aliased helpers, trusting names, or treating a conditional wrapper as guaranteed
        self.write("assertions.ts", """
            import { strictEqual } from "node:assert";
            export function equalOutput(actual, expected) {
              strictEqual(actual, expected);
            }
            export function assertReady(value) { return value; }
            export function checkIfEnabled(value) {
              if (enabled()) strictEqual(value, 1);
            }
        """)
        self.write("packages/harness/package.json", '{"name":"@demo/harness","exports":{"./*":"./src/*.ts"}}')
        self.write("packages/harness/src/checks.ts", """
            import { expect } from "vitest";
            export const confirm = value => expect(value).toBeVisible();
        """)
        path = self.write("helpers.test.ts", """
            import { equalOutput as verify, assertReady, checkIfEnabled } from "./assertions.js";
            import { confirm } from "@demo/harness/checks";
            import { confirm as unavailable } from "@other/harness/checks";
            test("local source", () => verify(out(), 1));
            test("package source", () => confirm(locator()));
            test("name alone", () => assertReady(out()));
            test("source unavailable", () => unavailable(out()));
            test("conditional helper", () => checkIfEnabled(out()));
        """)
        self.assertEqual(self.locations(path.name), [(6, "no-assert"), (7, "no-assert"), (8, "guarded-assert")])

    def test_literal_arguments_make_helper_loops_nonempty(self):
        # WHY: Glob.test.ts passes fixed matching and nonmatching path lists to assertMatches.
        # catches: ignoring the caller's fixture or accepting an empty or optional input
        path = self.write("loops.test.ts", """
            function assertMatches(pattern, matching, nonmatching) {
              for (const path of matching) expect(matches(pattern, path)).toBe(true);
              for (const path of nonmatching) expect(matches(pattern, path)).toBe(false);
            }
            test("fixed lists", () => assertMatches("*.ts", ["test.ts"], ["test.js"]));
            test("fixed object keys", () => {
              const optics = { Iso: makeIso(), Lens: makeLens() };
              for (const key of Object.keys(optics) as Array<keyof typeof optics>) {
                expect(optics[key].get(1)).toBeDefined();
              }
            });
            test("both lists empty", () => assertMatches("*.ts", [], []));
            test("unknown and empty", () => assertMatches("*.ts", paths(), []));
            function checkAfterClear(paths) {
              paths.length = 0;
              for (const path of paths) expect(matches("*.ts", path)).toBe(true);
            }
            test("helper empties its input", () => checkAfterClear(["test.ts"]));
        """)
        self.assertEqual(self.locations(path.name), [
            (12, "guarded-assert"), (13, "guarded-assert"), (18, "guarded-assert"),
        ])

    def test_indexed_mock_observation_controls_the_factory_collection(self):
        # WHY: stream.test.ts records a decode on an indexed decoder from recoveryFixture.
        # catches: ignoring the collection proven present or crediting optional access
        path = self.write("recorders.test.ts", """
            const setup = () => ({ decoders: [], peers: [] });
            test("proves that setup can create a decoder", () => {
              const { decoders, peers } = setup();
              expect(decoders[0]!.decode).toHaveBeenCalledOnce();
              expect(peers[0]?.decode).toHaveBeenCalledOnce();
            });
            test("leaves the collections empty", () => {
              const { decoders, peers } = setup();
              expect(decoders).toHaveLength(0);
              expect(peers).toHaveLength(0);
            });
        """)
        self.assertEqual(self.locations(path.name), [(10, "absence-no-control")])

    def test_imported_fixture_checks_do_not_prove_the_test_output(self):
        # WHY: tools.last.test.ts checks an import during setup, and graph.evidence-buttons.test.ts mounts a fixture.
        # catches: letting setup preconditions stand in for the checks inside an optional output loop
        self.write("fixture.ts", """
            import { assert, expect } from "vitest";
            export function must(result) {
              if (!result.ok) assert.fail("setup failed");
              return result.value;
            }
            export async function mount() {
              const world = await open();
              expect(world).toBeDefined();
              return world;
            }
        """)
        path = self.write("outputs.test.ts", """
            import { mount, must } from "./fixture";
            async function importAt(input) {
              clock.set();
              must(await importFixture(input));
              clock.reset();
            }
            const openGraph = () => mount();
            test("import success does not guarantee a row", async () => {
              await importAt(example);
              for (const row of loadRows()) expect(row.lastCapture).toBeNull();
            });
            test("mount success does not guarantee a graph node", async () => {
              const graph = await openGraph();
              for (const node of graph.nodes()) expect(node.links()).toHaveLength(0);
            });
        """)
        self.assertEqual(self.locations(path.name), [(8, "guarded-assert"), (12, "guarded-assert"),
                                                    (14, "absence-no-control")])

    def test_package_namespace_export_resolves_a_static_assertion(self):
        # WHY: Tuple.test.ts reaches its assertions through effect/testing's namespace export.
        # catches: recognizing a package by name or skipping a declared static assertion
        self.write("packages/harness/package.json", '{"name":"@demo/harness","exports":{"./testing":"./src/index.ts"}}')
        self.write("packages/harness/src/index.ts", 'export * as Checks from "./checks.ts";')
        self.write("packages/harness/src/checks.ts", """
            import assert from "node:assert";
            export class Compare {
              static schema = { fields: { equal: (actual, expected) => assert.deepStrictEqual(actual, expected) } };
            }
        """)
        path = self.write("helpers.test.ts", """
            import { Checks } from "@demo/harness/testing";
            test("checks two fields", () => Checks.Compare.schema.fields.equal(out(), expected));
        """)
        self.assertEqual(self.locations(path.name), [])

    def test_helper_validates_both_sides_of_type_guard(self):
        # WHY: toStandardSchemaV1 passes either a callback or issue array through an assertion helper.
        # catches: treating a throw as optional or accepting an unchecked callback
        self.write("assertions.ts", """
            import { strictEqual } from "node:assert";
            export function check(value, expected) { strictEqual(value, expected); }
        """)
        path = self.write("branch.test.ts", """
            import { Predicate } from "effect";
            import { check } from "./assertions";
            function verify(result, expected) {
              if (result.ok) {
                if (Predicate.isFunction(expected)) expected(result.value);
                else check(result.value, expected);
              } else throw new Error("missing result");
            }
            function checkResult(expected) { verify(read(), expected); }
            it("compares a fixture", () => checkResult([3]));
            it("runs the comparison callback", () => checkResult(value => check(value, 3)));
            it("empty callback can pass", () => checkResult(() => {}));
        """)
        self.assertEqual(self.locations(path.name), [(12, "no-assert")])

    def test_effect_checks_require_the_generator_to_execute(self):
        # WHY: BetterAuth and PrerenderCache return Effect programs through runtime and pipe wrappers.
        # catches: ignoring a connected generator or crediting an unrun or replaced effect
        self.write("provide.ts", """
            import { Effect } from "effect";
            import { dual } from "effect/Function";
            export const provide = dual(2, (effect, fixture) => Effect.provide(effect, fixture));
        """)
        path = self.write("effect.test.ts", """
            import { Effect, pipe } from "effect";
            import { it } from "@effect/vitest";
            const run = program => Effect.runPromise(program.pipe(Effect.scoped));
            const build = Effect.fn("check value")(function* () {
              expect(yield* read()).toBe(3);
            });
            it.effect("runs a generator", () => Effect.gen(function* () { expect(yield* read()).toBe(3); }));
            it.scoped("runs the function's effect", () => pipe(build(), Effect.scoped));
            it.live("runs a named generator", () => {
              const program = Effect.gen(function* () { expect(yield* read()).toBe(3); });
              return pipe(program, Effect.scoped);
            });
            it("awaits a wrapper", async () => { await run(Effect.gen(function* () { expect(yield* read()).toBe(3); })); });
            it("discards an effect", () => { Effect.gen(function* () { expect(yield* read()).toBe(3); }); });
            it("discards a promise", () => { run(build()); });
            it.effect("replaces an effect", () => pipe(build(), () => Effect.succeed(3)));
            it.effect("can skip the check", () => Effect.gen(function* () {
              if (enabled()) expect(yield* read()).toBe(3);
            }));
            it.effect("runs Effect.fn directly", Effect.fn(function* () { expect(yield* read()).toBe(3); }));
            it.effect("runs a promise effect", () => Effect.promise(async () => {
              expect(await read()).toBe(3);
            }));
            it.effect("fails on the other branch", () => Effect.gen(function* () {
              if (enabled()) expect(yield* read()).toBe(3);
              else yield* Effect.fail(new Error("missing result"));
            }));
            it.effect("discarded failure does not cover the branch", () => Effect.gen(function* () {
              if (enabled()) expect(yield* read()).toBe(3);
              else Effect.fail(new Error("missing result"));
            }));
            it.effect("runs through a provider", () => Effect.gen(function* () {
              expect(yield* read()).toBe(3);
            }).pipe(provide(fixture)));
            it.effect("runs after a service resolves", () => Effect.gen(function* () {
              expect(yield* read()).toBe(3);
            }).pipe(effect => Effect.flatMap(service, env => effect.pipe(Effect.provide(env)))));
            import { provide } from "./provide";
        """)
        self.assertEqual(self.locations(path.name), [
            (14, "no-assert"), (15, "no-assert"), (16, "no-assert"), (17, "guarded-assert"), (28, "guarded-assert"),
        ])

    def test_sibling_controls_use_the_same_projected_recorder(self):
        # WHY: menuHandler checks prefixed calls, and stream.test checks a peer recorder selected by index.
        # catches: losing projections or equating distinct filter captures and indexes
        path = self.write("projected.test.ts", """
            const prefix = "main-";
            const records = vi.fn().mock.calls;
            const entries = () => records.map(([entry]) => entry);
            const ids = (rows, label) => rows.filter(row => row.id.startsWith(label)).map(row => row.id);
            const setup = () => ({ peers: [] });
            it("observes both detectors", () => {
              const { peers } = setup();
              expect(ids(entries(), prefix).sort()).toEqual(["main-a"]);
              expect(peers[0]?.close).toHaveBeenCalled();
            });
            it("no matching calls or closes", () => {
              const { peers } = setup();
              const selected = entries().filter(entry => entry.id.startsWith(prefix));
              expect(selected).toHaveLength(0);
              expect(peers[0]?.close).not.toHaveBeenCalled();
              expect(peers[1]?.close).not.toHaveBeenCalled();
              expect(ids(entries(), "spare-")).toHaveLength(0);
            });
        """)
        self.assertEqual(self.locations(path.name), [(16, "absence-no-control"), (17, "absence-no-control")])

    def test_later_assertion_observes_the_exact_mock_result(self):
        # WHY: CaptureShortcutConfig asserts the ID carried from a preview into a later write.
        # catches: crediting a configured return without a unique asserted downstream use
        path = self.write("result-control.test.ts", """
            const preview = { id: "unique-review" };
            const other = vi.fn().mockResolvedValue({ id: "different-review" });
            const bridge = { read: vi.fn().mockResolvedValue(preview), save: vi.fn(), unused: vi.fn() };
            it("shows and then saves preview", async () => {
              expect(bridge.read).not.toHaveBeenCalled();
              expect(bridge.unused).not.toHaveBeenCalled();
              await click("Review");
              await click("Save");
              expect(bridge.save).toHaveBeenCalledExactlyOnceWith(preview.id);
            });
            it("configuration alone", () => expect(other).not.toHaveBeenCalled());
        """)
        self.assertEqual(self.locations(path.name), [(6, "absence-no-control"), (11, "absence-no-control")])

    def test_expected_value_reaches_rendered_output(self):
        # WHY: v4-04 derives the countdown baseline from a helper that reads rendered text.
        # catches: crediting a weak bound, ignoring formatting, or ignoring a new literal definition
        path = self.write("countdown.test.tsx", """
            import { render } from "@testing-library/react";
            function chipAfter(time) {
              const { container } = render(<Timer time={time} />);
              return { caption: container.querySelector(".caption")?.textContent ?? "" };
            }
            it("reads its own baseline", () => {
              const fresh = chipAfter(0);
              const total = Number(/^(\\d+) still open$/.exec(fresh.caption ?? "")?.[1]);
              expect(total).toBeGreaterThanOrEqual(20);
              expect(chipAfter(10).caption).toBe(`${total - 10} still open`);
              assert.equal(chipAfter(10).caption, `${total - 10} still open`);
              expect(chipAfter(10).caption).toEqual(`${total - 10} still open`);
              let expected = fresh.caption;
              expected = "10 still open";
              expect(chipAfter(10).caption).toBe(expected);
            });
        """)
        self.assertEqual(self.locations(path.name), [(10, "self-derived-expected"), (11, "self-derived-expected"),
                                                    (12, "self-derived-expected")])

    def test_selector_advisory_requires_a_semantic_check_on_the_element(self):
        # WHY: v4-04 checks button styling and the presence of a class-selected chip.
        # catches: using an unrelated text check as evidence for a different element
        path = self.write("selectors.test.tsx", """
            import { screen } from "@testing-library/react";
            it("checks styling only", () => {
              const button = screen.queryByRole("button", { name: "Open" });
              expect(button).toBeTruthy();
              expect(button!.classList.contains("primary")).toBe(true);
              expect(button!.className).toBe("primary");
              expect(button!.className.split(" ")).toContain("primary");
              expect(document.querySelector(".caption")).toBeNull();
              expect(screen.getByText("Toolbar")).toBeVisible();
            });
            it("also checks its label", () => {
              const button = screen.getByRole("button");
              expect(button).toHaveAccessibleName("Open");
              expect(button.classList.contains("primary")).toBe(true);
            });
        """)
        self.assertEqual(self.locations(path.name), [(5, "implementation-selector"), (6, "implementation-selector"),
                                                    (7, "implementation-selector"), (8, "implementation-selector")])

    def test_navigation_advisory_tracks_waits_and_component_imports(self):
        # WHY: v4-01 and v4-02 triggered native Back before asserting the hash.
        # catches: skipping a click helper or an imported router, or treating a wait as synchronous
        self.write("router.ts", "export const close = () => window.history.back();")
        self.write("Panel.tsx", """
            import { close } from "./router";
            export const Panel = () => <button onClick={close}>Close</button>;
        """)
        path = self.write("navigation.test.tsx", """
            import { fireEvent, render, screen, waitFor } from "@testing-library/react";
            import { Panel } from "./Panel";
            const closePanel = () => fireEvent.click(screen.getByText("Close"));
            it("reads stale history", () => {
              render(<Panel />);
              closePanel();
              expect(window.location.hash).toBe("#home");
            });
            it("waits for history", async () => {
              render(<Panel />);
              closePanel();
              await waitFor(() => expect(location.hash).toBe("#home"));
              expect(history.state).toEqual(null);
            });
        """)
        self.assertEqual(self.locations(path.name), [(7, "unawaited-navigation")])

    def test_navigation_requires_the_selected_handler(self):
        # WHY: the v4 tab and scrim races share a router with synchronous and unresolved clicks.
        # catches: inferring Back from an unrelated handler or losing callbacks passed through props
        for name in ("navigation-panel.tsx", "navigation-routes.ts", "navigation-events.ts"):
            self.fixture(name)
        path = self.fixture("navigation-precision.test.tsx")
        self.assertEqual(self.locations(path.name), [
            (line, "unawaited-navigation") for line in (9, 16, 22, 77)
        ])

    def test_negative_equality_controls_only_the_same_render_capture(self):
        # WHY: a different render proves the captured preview can vary before restoring it.
        # catches: matching output origins or variable names rather than the exact captured operand
        path = self.fixture("expected-precision.test.tsx")
        self.assertEqual(self.locations(path.name), [
            (line, "self-derived-expected") for line in (35, 36, 45)
        ])

    def test_extended_absence_witnesses(self):
        # WHY: logging uses separate console.info spies, and httpLayer checks a later request.
        # catches: splitting a global target or overlooking the call needed for a checked property
        path = self.write("controls.test.ts", """
            it("does not log by default", () => {
              const info = vi.spyOn(console, "info").mockImplementation(() => {});
              expect(info).not.toHaveBeenCalled();
            });
            it("logs when enabled", () => {
              const info = vi.spyOn(console, "info").mockImplementation(() => {});
              expect(info).toHaveBeenCalled();
            });
            it("waits for the token", () => {
              const fetchMock = vi.fn();
              const unrelated = vi.fn();
              expect(fetchMock).not.toHaveBeenCalled();
              expect(unrelated).not.toHaveBeenCalled();
              const request = new Request(fetchMock.mock.calls[0]?.[0]);
              expect(request.headers.get("authorization")).toBe("Bearer token");
            });
        """)
        self.assertEqual(self.locations(path.name), [(13, "absence-no-control")])

    def test_effect_transaction_and_deferred_are_executed_checks(self):
        # WHY: SQLite checks run inside the transaction, and a continuation must release its latch.
        # catches: counting only top-level checks, or crediting an Effect that was never run
        path = self.write("effect-runtime.test.ts", """
            import { Deferred, Effect } from "effect";
            import { it } from "@effect/vitest";
            import { SqlClient } from "effect/unstable/sql/SqlClient";
            it.effect("checks the transaction", () => Effect.gen(function* () {
              const client = yield* SqlClient;
              yield* client.withTransaction(Effect.gen(function* () {
                expect(yield* read()).toBe(3);
              }));
            }));
            it.effect("waits for the continuation to clear", () => Effect.gen(function* () {
              const cleared = yield* Deferred.make();
              yield* start(() => Deferred.succeed(cleared, undefined));
              yield* Deferred.await(cleared);
            }));
            it.effect("does not wait", () => Effect.gen(function* () {
              const cleared = yield* Deferred.make();
              Deferred.await(cleared);
            }));
        """)
        self.assertEqual(self.locations(path.name), [(15, "no-assert")])

    def test_defined_find_proves_the_same_role_guard(self):
        # WHY: streamText asserts existence of the exact role it filtered before narrowing.
        # catches: crediting a different discriminant or leaving a proven guard conditional
        path = self.write("roles.test.ts", """
            import { assertDefined, strictEqual } from "@effect/vitest/utils";
            it("checks the assistant parts", () => {
              const assistant = captured.find(msg => msg.role === "assistant");
              assertDefined(assistant);
              if (assistant.role === "assistant") strictEqual(assistant.content.length, 1);
            });
            it("can omit the assistant", () => {
              const assistant = captured.find(msg => msg.role === "assistant");
              if (assistant?.role === "assistant") strictEqual(assistant.content.length, 1);
            });
        """)
        self.assertEqual(self.locations(path.name), [(7, "guarded-assert")])

    def test_class_checks_distinguish_state_readbacks_and_utility_absence(self):
        # WHY: the matrix exposes sort state and the preview uses a class as a product hook.
        # catches: missing either exemption or crediting a utility negative as a semantic assertion
        self.write("preview.ts", """
            export const scrollParent = (row) => row.closest(".file-preview-virtualizer");
        """)
        path = self.write("preview.test.ts", """
            it("sorts by force", () => {
              expect(header.classList.contains("sort-asc")).toBe(true);
            });
            it("checks preview scroll state", () => {
              expect(document.querySelector(".file-preview-virtualizer")).toBeTruthy();
              expect(document.querySelector(".scrollbar-gutter-both")).toBeNull();
            });
        """)
        self.assertEqual(self.locations(path.name), [(6, "implementation-selector")])

    def test_relative_dom_assertions_allow_deltas_and_render_identity(self):
        # WHY: forms adds one column and landscape preserves the same SVG during a filter change.
        # catches: treating the before observation as an implementation-derived baseline for an absolute check
        path = self.write("relative.test.ts", """
            it("adds a column", () => {
              const before = document.querySelectorAll("th").length;
              addComponentColumn();
              expect(document.querySelectorAll("th").length).toBe(before + 1);
            });
            it("updates the active view instead of recreating the svg", () => {
              const before = document.querySelector("svg");
              updateFilter();
              expect(document.querySelector("svg")).toBe(before);
            });
        """)
        self.assertEqual(self.locations(path.name), [])

    def test_effect_provider_checks_are_run_for_the_requested_operation(self):
        # WHY: LanguageModel.generateObject needs the fake generateText response to decode its result.
        # catches: losing checks run by the fake provider or crediting its unused stream callback
        self.write("provider.ts", """
            import { Effect } from "effect";
            import { LanguageModel } from "effect/ai";
            import { dual } from "effect/Function";
            export const withLanguageModel = dual(2, (effect, options) =>
              Effect.provideServiceEffect(effect, LanguageModel.LanguageModel, LanguageModel.make({
                generateText: opts => Effect.succeed(options.generateText(opts)),
                streamText: opts => options.streamText(opts)
              })));
        """)
        path = self.write("provider.test.ts", """
            import { it } from "@effect/vitest";
            import { LanguageModel } from "effect/ai";
            import { withLanguageModel } from "./provider";
            it.effect("checks the request", () =>
              LanguageModel.generateObject({ prompt: "hi", schema }).pipe(
                withLanguageModel({ generateText: opts => {
                  expect(opts.prompt).toBeDefined();
                  return [{ type: "text", text: "{}" }];
                } })
              ));
            it.effect("has a check in the wrong provider method", () =>
              LanguageModel.generateText({ prompt: "hi" }).pipe(
                withLanguageModel({ streamText: opts => {
                  expect(opts.prompt).toBeDefined();
                  return [];
                } })
              ));
        """)
        self.assertEqual(self.locations(path.name), [(11, "no-assert")])

    def test_prompt_absence_uses_the_parts_supplied_to_the_model(self):
        # WHY: the lifecycle test supplies approval parts and pins the incremental prompt.
        # catches: requiring a second call to prove the input, or crediting an unrelated part tag
        path = self.write("prompt.test.ts", """
            import { assert, it } from "@effect/vitest";
            import { Effect } from "effect";
            import { LanguageModel, Prompt } from "effect/ai";
            it.effect("drops approval parts", () => Effect.gen(function* () {
              const prompts = [Prompt.assistantMessage({
                content: [Prompt.makePart("tool-approval-request", { approvalId: "1" })]
              })];
              yield* LanguageModel.generateText({ prompt: Prompt.fromMessages(prompts) }).pipe(provideModel);
              const sent = providerOptions[0];
              assert.deepStrictEqual(sent.incrementalPrompt, expected);
              for (const message of sent.prompt.content) {
                assert.strictEqual(message.content.filter(part => part.type === "tool-approval-request").length, 0);
                assert.strictEqual(message.content.filter(part => part.type === "different-type").length, 0);
              }
            }));
        """)
        self.assertEqual(self.locations(path.name), [(13, "absence-no-control")])
