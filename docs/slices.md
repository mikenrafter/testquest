# Slice checks and pinned mutants

A pinned mutant is a named change that reintroduces a plausible bug. A slice
check verifies the tests changed since a base commit. Run it first with the
bug present, then again after the fix to prove the tests catch the pinned
mutants.

Each command reads committed `HEAD` in detached scratch worktrees. Install
`ast-grep-py` in the Python environment that runs `tq`. For Dart, set
`TQ_DART_GRAMMAR` to the tree-sitter Dart grammar library.

## Pin a fault

Put the header and swap directly above the code to replace:

```python
def window(items, n):
    # tq-mutant M-012 boundary assumes=A-07 killed-by=test_window.WindowTest.test_last_item_kept
    # | return items[:n - 1]
    return items[:n]
```

Use `//` for Rust, JavaScript, TypeScript, Dart, Kotlin, and Java. Each
`| ` line supplies one replacement line. tq adds the first replaced line's
indentation to the swap. Keep any extra indentation after `| `. The swap
replaces the same number of lines by default. Set `replaces=N` in the header
for a different count. Separate multiple `killed-by` test names with commas.

In the test or its leading comment, name the pin:

```python
# catches: M-012
# reaches: when n equals the input size, then the window includes the final item
def test_last_item_kept(self):
    self.assertEqual(window(["a", "b"], 2), ["a", "b"])
```

For sidecars, commit `.tq/config` with `mutants = sidecar` and a JSON file
under `.tq/mutants/` per mutant:

```json
{
  "id": "M-012",
  "file": "window.py",
  "anchor": "return items[:n]",
  "swap": "return items[:n - 1]",
  "assumes": "A-07",
  "flip": "boundary",
  "killed_by": ["test_window.WindowTest.test_last_item_kept"]
}
```

A sidecar anchor must match one whole-line span after removing the first
line's indentation. Use newline escapes for a multiline anchor or swap.
The swap uses indentation relative to the first replaced line. An empty
sidecar swap deletes the anchor.

## Run the checks

```sh
tq slice check 1 --feature windows --base BASE --cmd 'python3 -m unittest {test}'
tq slice check 1 --feature windows --base BASE --cmd 'python3 -m unittest {test}' --after-green
tq mutants check --since BASE
tq revert-check test_window.py::WindowTest::test_last_item_kept --mutant M-012 --cmd 'python3 -m unittest {test}'
```

- `slice check` runs the ast-grep lint gate and checks the notes on changed
  tests. A `catches:` note can cite a planned ID before the implementation
  exists. Conditional tests and tests with a `reaches:` note need the form
  `when X, then Y`. The red run requires every selected test to fail on an
  assertion. The green run checks that each cited pin exists and that its
  named tests catch it. Each pin introduced or changed in the slice must
  have a citation from a slice test.
- `mutants check` replays each pin in isolation. When given `--since`, it
  selects pins whose definition or replacement span changed since the
  specified commit. It uses unittest by default. `killed` requires every
  named test to pass at HEAD and fail on an assertion with the swap.
  `survived` means the runs failed to prove a kill. `anchor-missing` means the
  replacement span is missing or ambiguous. `unparseable` means the swapped
  code has a syntax error. An inline pin is `stale` when a first-parent
  commit changed its replacement lines and left the block identical.
- `revert-check` tests one mutant or applies `git revert --no-commit` to
  `--revert COMMIT` in a scratch worktree. It runs the restored test in a
  fresh copy of HEAD. It compares the live worktree's porcelain status,
  HEAD, index entries, and all tracked and untracked file hashes between
  runs and at the end, including ignored files. It stops on a difference and
  prints the changed paths. A failed check prints the retained scratch path.
  A successful check removes its scratch worktrees.

Use a `--cmd` with `{test}` and configure the runner to select that one test.
tq splits the command into arguments and substitutes the name without a
shell. Test references can use `path::qualified_name`, the runner name, or
an unambiguous short name. Wrappers should preserve the runner's output and
exit status. Commands run with your permissions. The worktree comparison
detects changes present at a comparison point.

| Runner | Value substituted for `{test}` | Evidence read |
| --- | --- | --- |
| unittest | Dotted test name | Failure and error reports, and the run summary |
| pytest | `path::Class::test` or `path::test` | Assertion and collection, setup, and teardown reports |
| Rust libtest | `module::test` | Assertion panic and test-result summaries |
| Node test, Jest, Vitest | Full test title | Assertion diagnostics and test totals |
| Dart package:test | Test name | Expected and actual values, load errors, and expanded summary |
| JUnit | Test or method name | Assertion exceptions, build errors, and test totals |

A successful process with zero tests or missing runner evidence fails the check.
For a custom assertion message, keep the runner's assertion marker in the
output so tq can distinguish it from another error.

## Read the results

Slice checks append a `gate` event with a pass or fail outcome and reasons
to `slices/<feature>/NN.jsonl`. The feature defaults to the main worktree's
directory name. A pass also appends `gate-ok` for a watcher.

Pinned replays append `mutant` events and revert-check appends a `kill`
event to the configured test ledger. Revert-check writes the ledger after
comparing live worktree contents. [Test ledger reference](ledger.md)
