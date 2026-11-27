# Rules reference

`tq lint` checks Python and Rust test files. The ast-grep engine also scans TypeScript and JavaScript. `tq witness` checks that conditional tests reach the case they claim to verify.

Rules have two levels:

- `error` (gating): causes `tq lint` or `tq witness` to exit with status 1.
- `advisory`: prints the finding prefixed with `advisory:` without causing a non-zero exit code on its own.

Run `tq rules` to list the rules and their levels. Each gating finding includes a `fix:` line. Run `tq rules RULE --lang py|rs|ts` to get the full recipe, examples for your language, and shortcuts to reject in review.

## Suppressing a finding

Add a suppression comment on the flagged line or on a standalone comment line immediately above it. Use `#` in Python and `//` in Rust or TypeScript.

```python
# tq: allow no-assert verifies parse_port accepts a valid string without raising
def test_parse_port_accepts_valid_input():
    parse_port("8080")
```

```rust
// tq: allow self-compare checks that Clone preserves equality on independently cloned values
assert_eq!(a.clone(), a.clone());
```

For function-level rules (`no-assert` and `guarded-assert`), `tq` reports the finding on the `def` or `fn` line, so place the comment on that line or the line directly above it. Use a supported lint rule and explain the exception in at least three words beyond the rule name. An invalid allow produces an `invalid-suppression` finding. `parse-error` cannot be suppressed.

An allow applies to one matching finding. It moves that row to `suppressed` in the JSON output, alongside the `findings` list. Each suppressed row includes its `reason`. Text output reports the count of suppressed rows. Only gating rows in `findings` affect the exit status.

## Gating rules

### `absence-no-control`

Flags a test that asserts a recorder, collector, or detector stayed empty (or writes to a mock or callback flag without reading it) when no check proves that the recorder can ever report a result.

**Why it can pass on broken code.** If the detector is wired to the wrong hook, never invoked, or silently disabled, the list stays empty and the test still passes.

Bad:

```python
def test_valid_config_reports_no_errors():
    errors = []
    validate({"port": "8080"}, errors)
    assert errors == []
```

Fix:

```python
def test_valid_config_reports_no_errors():
    errors = []
    validate({"port": "nope"}, errors)
    assert errors == ["port: not a number"]
    errors.clear()
    validate({"port": "8080"}, errors)
    assert errors == []
```

**Exemptions.** Exempt when the same test, or another test in the same scan calling the same detector, includes a positive observation showing that the recorder can fire. Suppress with `# tq: allow absence-no-control REASON` when an independent observation establishes that the recorder can fire.

### `const-truth`

Flags an assertion whose predicate always evaluates to true, such as `assert True`, `assert x == 1 or True`, or `assert!(true)`.

**Why it can pass on broken code.** The check cannot fail regardless of what the code under test returns.

Bad:

```python
def test_parse_port():
    result = parse_port("8080")
    assert result == 8080 or True
```

Fix:

```python
def test_parse_port():
    result = parse_port("8080")
    assert result == 8080
```

**Exemptions.** Suppress with `# tq: allow const-truth REASON` only when a deliberately trivial assertion checks test framework plumbing.

### `guarded-assert`

Flags a test where every assertion sits inside a conditional branch or loop that can execute zero times.

**Why it can pass on broken code.** If the production code returns an empty collection or takes the unasserted branch, every check is skipped and the test passes.

Bad:

```python
def test_positive_items():
    items = fetch_items()
    for item in items:
        assert item > 0
```

Fix:

```python
def test_positive_items():
    items = fetch_items()
    assert items
    for item in items:
        assert item > 0
```

**Exemptions.** Exempt when at least one assertion runs unconditionally, or when `tq` can prove the iterable is non-empty (such as a non-empty literal table). Suppress with `# tq: allow guarded-assert REASON` when another scanned test exercises the branch as a witness, or when zero matching items is explicitly a valid case.

### `no-assert`

Flags a test function that contains no assertion, failure helper, or expected-exception check verifying the outcome under test.

**Why it can pass on broken code.** Any wrong return value passes as long as the function does not crash.

Bad:

```python
def test_parse_port():
    parse_port("8080")
```

Fix:

```python
def test_parse_port():
    assert parse_port("8080") == 8080
```

**Exemptions.** Recognizes standard assertions, `pytest.raises` / `assertRaises`, `#[should_panic]`, helpers whose names start with `assert`, `check`, or `expect`, and exact helper names `raises` and `fail`. Suppress with `# tq: allow no-assert REASON` when successful execution is the stated check, such as passing output through a strict parser.

### `self-compare`

Flags an assertion that compares the same expression on both sides, such as `assert port == port` or `assert_eq!(x, x)`.

**Why it can pass on broken code.** Any returned value equals itself, so a broken implementation still passes.

Bad:

```python
def test_parse_port():
    port = parse_port("8080")
    assert port == port
```

Fix:

```python
def test_parse_port():
    port = parse_port("8080")
    assert port == 8080
```

**Exemptions.** Suppress with `# tq: allow self-compare REASON` when the stated property is reflexivity or repeatability and both operands are evaluated independently (for example, checking a `Clone` or `PartialEq` derive).

## Advisory rules

### `nondistinguishing-literal`

Flags fixtures or assertions whose literal inputs cannot distinguish a right implementation from a common wrong one. Examples include palindromic inputs in order or encoding tests, repeated positional arguments that hide swapped parameters, neutral operands (`0`, `1`, `-1`, empty collections) held fixed across calls, and loose bounds when exact values are known.

**Why it can pass on broken code.** A reversed bit order passes on a palindrome (`b"abba"`), swapped arguments pass when both arguments have the same value, and an ignored offset passes when the offset is `0`.

Bad:

```python
def test_encode_order():
    assert encode_order(b"abba") == b"abba"
```

Fix:

```python
def test_encode_order():
    assert encode_order(b"abcd") == b"dcba"
```

**Exemptions.** Exempt when a sibling call in the same test varies the repeated pair, when duplicate values are keyword arguments rather than positional ones, or when the value is a boolean, `None`, or a `--flag` string. Suppress with `# tq: allow nondistinguishing-literal REASON` when the test explains why the symmetric or fixed input is needed (such as equal weights, a zero-write case, or isolating another parameter).

### `self-derived-expected`

Flags an equality check whose expected value is computed by calling production code from the same module under test.

**Why it can pass on broken code.** If the shared helper has a bug, both sides of the assertion produce the same wrong value and the test passes.

Bad:

```python
from billing import apply_tax, total_cents

def test_total_cents():
    assert total_cents(1000, 0.08) == apply_tax(1000, 0.08)
```

Fix:

```python
from billing import total_cents

def test_total_cents():
    assert total_cents(1000, 0.08) == 1080
```

**Exemptions.** Exempt when the test checks repeatability, a before-and-after observation, inverse codecs (such as `encode` / `decode` or `compress` / `decompress`), or a declared relation between different inputs or entry points. Suppress with `# tq: allow self-derived-expected REASON` when the relation is intentional and not recognized automatically.

## Witness and diagnostic rules

### `missing-witness`

Reported by `tq witness` (not `tq lint`). Flags a conditional or guarded test that neither measures coverage itself nor names another scanned test with a `# reaches: TEST_NAME` (or `// reaches: TEST_NAME`) comment proving that the condition is reached.

**Why it can pass on broken code.** Without a witness or coverage measurement, a conditional check can stay dormant while the suite stays green.

Bad:

```python
def test_nonzero_items():
    items = fetch_items()
    for item in items:
        assert item > 0
```

Fix:

```python
def test_nonzero_items():
    assert fetch_items() == [3, 7]
```

The expected result checks that the call returned items and pins which items it returned.

**Exemptions.** Exempt when the test measures coverage itself or names a different scanned test in a `reaches:` comment.

### `invalid-suppression`

Flags a `# tq: allow` (or `// tq: allow`) comment that names an unknown rule or lacks a reason of at least three words beyond the rule name.

**Fix.** Name a supported lint rule and explain why the exception is valid.

### `parse-error`

Flags a `.py` or `.rs` file that `tq` could not parse or decode as UTF-8.

**Fix.** Repair the syntax or file encoding before scanning. This diagnostic cannot be suppressed.
