# Test ledger reference

The test ledger is an append-only JSONL file that records how tests change over time, what `tq lint` flags, how those hits are judged, and which faults each test catches.

By default, `tq log` writes to `../<repo-name>.research/tq/ledger.jsonl` beside the checkout. Pass `--ledger PATH` to put it anywhere. Writes take an exclusive file lock, deduplicate by event `id`, and flush to disk.

Each test is identified by its repository name, qualified name (`path::Class::method` or `path::module::fn`), and a SHA-256 hash of its body. When a commit moves a file or renames a test whose body hash matches uniquely, `tq log record` preserves the test's identity.

In `kill` events, a fault patch (the unified diff named in a `# catches:` comment) is stored in the `mutant` field as the SHA-256 hash of its contents.

## Events

Six event types are recorded today. Three more (`run`, `escape`, and `cost`) are part of the ledger design and are not yet recorded by `tq log`.

| Event | Status | Written by | Key fields |
| --- | --- | --- | --- |
| `born` | recorded | `tq log record` | `commit`, `date`, `phase`, `author`, `co_authors`, `test`, `test_id`, `hash`, `claim`, `catches` |
| `changed` | recorded | `tq log record` | `commit`, `date`, `phase`, `before`, `after`, `checks_before`, `checks_after`, `loosened` |
| `died` | recorded | `tq log record` | `commit`, `date`, `phase`, `reason`, `lifetime_seconds`, `kills` |
| `lint` | recorded | `tq log lint` | `run`, `row`, `path`, `line`, `rule`, `rule_version`, `level`, `test`, `hash`, `offset` |
| `verdict` | recorded | `tq log verdict`, `tq log import-verdicts` | `row`, `rule`, `rule_version`, `verdict` (`real`, `intended`, `false`), `why`, `pattern`, `judge` |
| `kill` | recorded | `tq log kill` | `run`, `commit`, `mutant`, `operator`, `worktree`, `red`, `green`, `bodies` |
| `run` | not yet recorded | none yet | per-test duration, peak memory, status (`passed`, `failed`, `flaky`) |
| `escape` | not yet recorded | none yet | fix commit, file and line, covering tests, phase that should have caught it |
| `cost` | not yet recorded | none yet | phase, tokens, wall time, review rounds |

`tq log report` already reads `run` events with `status` set to `failed` or `flaky` when filtering deletion candidates, even though no `tq log` subcommand writes `run` events yet.

## The four measures

The ledger tracks four properties of a test suite and of `tq` itself:

| Measure | How it is computed | Current status |
| --- | --- | --- |
| Efficacy | Faults caught (`kill` events), unique kills (faults caught by only one test), and escapes caught | Faults and unique kills from `# catches:` replays are reported; `escape` events are not yet recorded |
| Necessity | Deletion candidates: living tests observed in a `kill` event that have zero unique kills and have never failed in a `run` event | Reported under `Deletion candidates` in `tq log report` |
| Accuracy | Per-rule precision by rule version: `strict = real / (real + intended + false + unjudged)` and `loose = real / (real + false)` | Reported in `tq log report` from `lint` and `verdict` events |
| Cost | Runtime, memory, flaky rate, and authoring cost per phase | Not yet recorded |

## The `Test-Phase:` commit trailer

`tq log record` reads the `Test-Phase:` git trailer from each commit so survival and kills can be grouped by the phase that introduced a test.

Supported values:

- `red`
- `green`
- `white-box`
- `re-derivation`
- `hardening`
- `review-fix`
- `bug-fix`

Example commit message:

```text
Add byte-order test for u16 encoding

Test-Phase: red
```

Commits without a `Test-Phase:` trailer (or with an unrecognized value) are recorded under phase `unknown`.

## Commands

All `tq log` subcommands accept `--ledger PATH` to override the default ledger path.

### `tq log record`

```sh
tq log record --since REF [--ledger PATH]
```

Walks git history from `REF` to `HEAD` in topological order and appends `born`, `changed`, and `died` events. Re-running over an overlapping range is idempotent because events are deduplicated by `id`.

### `tq log lint`

```sh
tq log lint --since REF [--json] [--ledger PATH] [paths ...]
```

Runs `tq lint --since REF` on changed tests, appends one `lint` event per finding (tagged with the current `rule_version` and a stable `row` id), and prints the findings with `[row <id>]`. Exits with status 1 if any gating (`error`) finding is reported.

### `tq log verdict`

```sh
tq log verdict --row ID --verdict {real,intended,false} --why REASON [--pattern TAG] [--judge NAME] [--ledger PATH]
```

Records a judgment on a `lint` row already in the ledger. When multiple verdicts exist for the same `(rule_version, row)`, `tq log report` uses the latest one.

### `tq log import-verdicts`

```sh
tq log import-verdicts FILE [--ledger PATH]
```

Imports judgments from a JSONL file into `verdict` events. Each line must include `repo`, `path`, `rule`, `hash`, `offset` (integer), `verdict`, and `why`.

### `tq log kill`

```sh
tq log kill [--repo PATH] [--worktree] [--cmd CMD] [--ledger PATH]
```

Replays every `# catches: ... [patch: PATH]` fault patch in the repository and appends one `kill` event per fault patch. `--cmd` defaults to `python3 -m unittest {test}` and must include `{test}` so each replay result can be attributed to its test. Exits with status 1 if any replay stays green, or 2 if a patch fails or the baseline errors.

### `tq log report`

```sh
tq log report [--json] [--ledger PATH]
```

Summarizes the ledger into four sections:

1. Phase survival (`Born`, `Died`, `Survived` by `Test-Phase`).
2. Rule precision (`Real`, `Intended`, `False`, `Unjudged`, `Strict`, `Loose` by rule and rule version).
3. Test kills (`Kills` and `Unique` kills per test).
4. Deletion candidates (living tests observed in `kill` events with zero unique kills and no recorded failure).
