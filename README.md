# testquest

`tq` finds Python and Rust tests that cannot fail or that pass while the code is broken, replays the fault each test says it catches, and keeps a ledger (a log file) of what the suite catches over time.

> Early. The rules were tuned on one corpus of 13 repositories. `tq log` does not yet record test runs, escaped bugs, or cost, and `tq catches` runs `python3 -m unittest` unless you pass `--cmd`.

## Why

A green suite can hide tests that check nothing. A test may omit assertions, or hide its only check inside a branch or empty loop. It may compare a value with itself, compute the expected value with the helper it tests, or use a palindrome that passes even when byte order is reversed. Each one passes on broken code, so the bug ships with a green build. `tq` flags these patterns, and `tq catches` proves a test fails when you break the code it names.

Rule accuracy was measured on 13 Python and Rust repositories frozen on 2026-11-26. A model judge labelled every hit from every tuning round (1,003 in all) as real, intended, or false. The table shows the final rules. Strict accuracy is real hits divided by all hits, so intended hits count against the rule.

| Rule | Hits | Real | Strict accuracy | Seen blind by a model |
| --- | ---: | ---: | ---: | ---: |
| `guarded-assert` | 80 | 71 | 88.8% | 8/10 |
| `nondistinguishing-literal` | 54 | 28 | 51.9% | 1/10 |
| `no-assert` | 50 | 21 | 42.0% | 9/10 |
| `absence-no-control` | 15 | 5 | 33.3% | 0/3 |
| `self-derived-expected` | 24 | 8 | 33.3% | 1/4 |
| `self-compare` | 7 | 1 | 14.3% | 0/1 |

`const-truth` had 0 hits on that corpus. Each rule had to reach 15% strict accuracy. `self-compare` measured 14.3%, under the 15% bar: the corpus held one real case, and six hits were deliberate `Clone` or `PartialEq` checks. It stays gating because a self-comparison means the test cannot fail. Mark derive checks with `# tq: allow self-compare REASON`.

A separate blind test on 2026-11-26 showed a model 78 shuffled tests (38 real hits, 40 unflagged tests from the same files) with source and no `tq` hint. It caught most tests with no check or a skippable one, missed most subtler defects, and flagged 2 of 40 clean tests.

## Install

With Nix:

```sh
nix run github:mikenrafter/testquest -- lint tests/ # scan tests/
```

Flake input:

```nix
{
  inputs.testquest.url = "github:mikenrafter/testquest";
  # package: inputs.testquest.packages.<system>.tq
}
```

Without Nix (requires Python 3.11.4 or later and `git`):

```sh
git clone https://github.com/mikenrafter/testquest.git
alias tq='python3 -I "$PWD/testquest/tq"' # use tq as below
tq --help
```

## Usage

| Command | What it does |
| --- | --- |
| `tq lint [--since REF] [paths ...]` | Scan Python and Rust tests for weak checks and fixtures |
| `tq witness paths ...` | Find conditional checks without a witness test or coverage check |
| `tq rules` | Print every rule, its level, and when suppression is allowed |
| `tq catches [--repo PATH] [--worktree] [--cmd CMD]` | Apply each `# catches:` fault patch to a temporary copy of `HEAD` (`--worktree`: working tree) and run its test |
| `tq mutants check [--since BASE] [--cmd CMD]` | Replay pinned faults and record whether their named tests catch them |
| `tq slice check N [--feature NAME] --base BASE --cmd CMD [--after-green]` | Gate changed tests on lint, test notes, and assertion failures or mutant kills |
| `tq revert-check TEST (--mutant ID \| --revert COMMIT) --cmd CMD` | Verify one test fails with a fault and passes with HEAD restored |
| `tq risk [--repo PATH] [--html FILE] [--budget N]` | Rank source files by churn, fixes, importers, and test references |
| `tq props [family \| --search TEXT]` | Look up cited test properties in `props/` |
| `tq log <subcommand>` | Record test history, lint rows, verdicts, and fault kills; print a report |

`lint`, `witness`, `catches`, `risk`, `props`, `log lint`, and `log report` accept `--json`.

The slice commands require `ast-grep-py`. [Slice checks and pinned mutants](docs/slices.md)

### Walk-through

Lint tests changed since `HEAD`:

```sh
tq lint --since HEAD # check tests touched in the working tree
```

```text
tests/test_codec.py:9 guarded-assert Every check is conditional and the test can skip them all.
```

Fix the guarded assertion, then write a fault patch (a unified diff relative to the repository root) breaking `codec.py` (`value.to_bytes(2, "big")`) and name it in a `# catches:` comment above the test:

```python
    # catches: byte order swapped to little-endian [patch: patches/swap-endian.patch]
    def test_encode_order(self):
        self.assertEqual(encode_u16(0x1234), b"\x12\x34")
```

Save the fault patch, commit with a `Test-Phase:` trailer, and replay the fault on `HEAD`:

```sh
mkdir -p patches
sed 's/"big"/"little"/' codec.py > /tmp/codec.py
diff -u codec.py /tmp/codec.py --label a/codec.py --label b/codec.py > patches/swap-endian.patch
git add -A && git commit -m "Add byte-order test" -m "Test-Phase: red"
tq lint --since HEAD~1 # confirm the changed test is clean
tq catches             # replay the fault patch on HEAD
```

```text
tests/test_codec.py:9 red tests.test_codec.CodecTest.test_encode_order byte order swapped to little-endian
...
FAILED (failures=1)
```

Record history and fault kills in the ledger, `.tq/ledger.jsonl` in the main worktree, then report. A config file can move it or add a global ledger across repositories ([docs/ledger.md](docs/ledger.md)).

```sh
tq log record --since HEAD~1 # record born, changed, and died tests
tq log kill                  # replay fault patches and log kills
tq log report                # print survival, precision, and kills
```

```text
Phase              Born  Died  Survived
red                   1     0         1

Rule  Version  Real  Intended  False  Unjudged  Strict  Loose

Kills  Unique  Test
    1       1  tests/test_codec.py::CodecTest::test_encode_order

Deletion candidates
```

See [docs/ledger.md](docs/ledger.md) for the ledger format and `tq log` subcommands.

## Rules

Gating rules (`error`) exit 1; advisory rules (`advisory`) print without failing the command.

| Rule | Level | What it flags |
| --- | --- | --- |
| `absence-no-control` | error | Absence check with no proof the detector can fire |
| `const-truth` | error | Assertion predicate that is always true |
| `guarded-assert` | error | Every check is conditional and can be skipped |
| `no-assert` | error | Test has no check verifying the outcome |
| `self-compare` | error | Assertion compares the same expression on both sides |
| `nondistinguishing-literal` | advisory | Fixture or assertion can let broken code pass |
| `self-derived-expected` | advisory | Expected value reuses production code under test |

`tq` also reports `missing-witness` (`tq witness`), `invalid-suppression`, and `parse-error`. See [docs/rules.md](docs/rules.md) for examples, exemptions, and `# tq: allow RULE REASON`.

## Limits

- Python and Rust only.
- Static checks can miss dynamic assertions behind indirect calls and can flag intended patterns until suppressed with `# tq: allow`.
- Tuned on one corpus (13 repositories, frozen 2026-11-26, model-judged). `self-compare` is below the 15% bar (one real case), `const-truth` had no hits so its accuracy is unmeasured, and the blind perception samples for `absence-no-control` (3), `self-compare` (1), and `self-derived-expected` (4) are small.
- `tq log` does not yet record `run`, `escape`, or `cost` events.
- `tq catches` and `tq log` require a `git` repository.

## For agents and contributors

```sh
python3 -I -m unittest discover -s tests # run all unit tests
```

| Path | Contents |
| --- | --- |
| `tq` | Executable entry point |
| `tq_rules/` | CLI, parsers, rules, witness, fault replay, risk ranking, ledger |
| `props/` | Cited test properties for `tq props` |
| `tests/` | Unit tests and fixtures |
| `docs/` | [docs/rules.md](docs/rules.md) and [docs/ledger.md](docs/ledger.md) |

## Credits

Built on Dan Luu's [study of how coding agents test](https://danluu.com/agentic-testing/) and [notes on software testing](https://danluu.com/testing/), which documented the failure modes (including palindromic inputs hiding reversed bit order) behind these rules.

## License

MIT; see LICENSE.
