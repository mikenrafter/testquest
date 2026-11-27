# Test ledger reference

The test ledger is an append-only JSONL file that records how tests change over time, what `tq lint` flags, how those hits are judged, and which faults each test catches.

By default, `tq log` writes to `.tq/ledger.jsonl` in the main worktree. Linked worktrees share that file and the main worktree's configuration. Commands that inspect commits or tests still use the worktree you run them against.

## Configuration

Put shared settings in `$XDG_CONFIG_HOME/tq/config.toml` (`~/.config/tq/config.toml` by default). A repository can override either setting in `.tq/config.toml` in its main worktree.

```toml
[ledger]
path = "{parent}/{name}.research/tq/ledger.jsonl"
global = "~/notes/tq/ledger.jsonl"
```

This example keeps each repository's ledger in its sibling research directory and copies new events to one global ledger. Without a `global` setting, only the repository ledger is written.

| Ledger | Settings from highest to lowest precedence |
| --- | --- |
| Per repository | `--ledger PATH`, `TQ_LEDGER`, repository config, global config, default |
| Global | `--global-ledger PATH`, `TQ_GLOBAL_LEDGER`, repository config, global config |

TOML paths accept `{root}` (the absolute path of the main worktree), `{name}` (its directory name), `{parent}` (its parent directory), and `{home}` (your home directory). Use `{{` and `}}` for literal braces in TOML paths. Relative TOML paths start at the main worktree, or the current directory when reporting from outside Git. Relative flag and environment paths start at the current directory. All paths support `~`. Unknown config keys and template fields are errors.

Set `path = "none"` to write only to the global ledger. The value `"none"` also disables either path when passed in its flag or environment variable, or set in its TOML key. Use distinct files for the two ledgers.

Run `tq log where` to see both paths and the config file and key, environment variable, flag, or default that supplied each one. Use `tq log where --json` for the same information as JSON, with `null` for disabled or unavailable paths.

Each append takes an exclusive file lock, deduplicates by repository and event ID, and flushes to disk. When both ledgers are configured, the repository append happens first. A global failure exits with status 2 and leaves the repository append in place. Repeating a history record or cache import can finish the global append without duplicating the local records.

Global records store a normalized `origin` URL in `repo`, or the absolute main worktree path if there is no `origin`. For network origins, normalization lowercases the host, strips a trailing slash and `.git` suffix, and omits default ports, query strings, and fragments. SSH usernames are retained. Global records also store the main worktree directory name in `repo_name`. Global cache imports are grouped under the repository that ran the import.

To keep using a ledger at the old sibling research path, set `path` as shown above. To switch to the new default, stop ledger writes, create `.tq` in the main worktree, and move the old `ledger.jsonl` there if the destination is empty. Setting up a global ledger starts collecting events on subsequent commands. To include past test changes, rerun `tq log record --since REF` from the desired starting point.

Each test is identified by its repository name, qualified name (`path::Class::method` or `path::module::fn`), and a SHA-256 hash of its body. When a commit moves a file or renames a test whose body hash matches uniquely, `tq log record` preserves the test's identity.

In `kill` events, a fault patch (the unified diff named in a `# catches:` comment) is stored in the `mutant` field as the SHA-256 hash of its contents.

## Events

Six event types are recorded today. Three more (`run`, `escape`, and `cost`) are part of the ledger design and are not yet recorded by `tq log`.

| Event | Status | Written by | Key fields |
| --- | --- | --- | --- |
| `born` | recorded | `tq log record` | `commit`, `date`, `phase`, `author`, `co_authors`, `test`, `test_id`, `hash`, `claim`, `catches` |
| `changed` | recorded | `tq log record` | `commit`, `date`, `phase`, `before`, `after`, `checks_before`, `checks_after`, `loosened` |
| `died` | recorded | `tq log record` | `commit`, `date`, `phase`, `reason`, `lifetime_seconds`, `kills` |
| `lint` | recorded | `tq log lint` | `run`, `row`, `path`, `line`, `rule`, `rule_version`, `level`, `test`, `hash`, `offset`, optional `reason` |
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

All `tq log` subcommands accept `--ledger PATH` and `--global-ledger PATH` to override the configured paths. Either flag can also appear before the subcommand.

### `tq log record`

```sh
tq log record --since REF [--ledger PATH]
```

Walks git history from `REF` to `HEAD` in topological order and appends `born`, `changed`, and `died` events. Re-running over an overlapping range is idempotent because events are deduplicated by repository and event ID.

### `tq log lint`

```sh
tq log lint --since REF [--engine legacy|ast-grep] [--json] [--ledger PATH] [paths ...]
```

Scans changed tests and records one `lint` event per finding with the current `rule_version` and a stable `row` ID. It records suppressed findings too, with the allow comment's `reason`. JSON returns `findings` and `suppressed` lists with row IDs. Text output shows the IDs on visible findings and counts suppressed rows. A visible gating finding gives exit status 1.

### `tq log verdict`

```sh
tq log verdict --row ID --verdict {real,intended,false} --why REASON [--pattern TAG] [--judge NAME] [--ledger PATH]
```

Records a judgment on a `lint` row already in the ledger. When multiple verdicts exist for the same `(repo, rule_version, row)`, `tq log report` uses the latest one.

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
tq log report [--global [--repo NAME|URL]] [--json]
```

The report covers five kinds of evidence.

1. Phase survival (`Born`, `Died`, `Survived` by `Test-Phase`).
2. Rule precision (`Real`, `Intended`, `False`, `Unjudged`, `Strict`, `Loose` by rule and rule version).
3. Allows per rule. The count covers distinct suppressed findings recorded in the ledger. Rerunning lint on the same test body and finding location leaves the count unchanged.
4. Test kills (`Kills` and `Unique` kills per test).
5. Deletion candidates (living tests observed in `kill` events with zero unique kills and no recorded failure).

With `--global`, the report includes the combined summary and a breakdown for each repository. In JSON, the breakdown is under `repositories`, keyed by the global `repo` identifier. Use `--repo` with `--global` to select by identifier, checkout directory name, or remote repository name. A name shared by several repositories includes all matches. A global report works outside a Git worktree when the global path is available there.
