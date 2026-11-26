from dataclasses import dataclass
import re

from .source import finding, scan_paths


RULES = {
    "const-truth": (
        "Assertion contains a predicate that is always true.",
        "A deliberately trivial assertion checks framework plumbing.",
    ),
    "self-compare": (
        "Assertion compares the same expression on both sides.",
        "The stated property is reflexivity or repeatability, and both operands are independently evaluated.",
    ),
    "no-assert": (
        "Test has no assertion, expected exception, or recognized checking helper.",
        "The stated check is successful execution, such as passing output through a strict parser.",
    ),
    "guarded-assert": (
        "Every check is conditional and the test can skip them all.",
        "The branch is exercised by a named witness, or zero matching items is explicitly a valid case.",
    ),
    "absence-no-control": (
        "Recorder has no positive check showing it can observe an action.",
        "An independent observation in the same test establishes that the recorder can fire.",
    ),
    "self-derived-expected": (
        "The expected value reuses production code from the module under test.",
        "The test checks repeatability, a before-and-after observation, or a declared relation between different inputs or entry points.",
    ),
    "nondistinguishing-literal": (
        "Symmetric inputs or an unvaried argument can hide an argument handling error.",
        "The test's WHY explains the symmetric or fixed input. Examples include equal weights, a zero-write case, or a test that isolates another parameter.",
    ),
}
ADVISORY = {"nondistinguishing-literal", "self-derived-expected"}
ORDERED_NAME = re.compile(r"reverse|order|swap|endian|rotate|crc|encode", re.I)
UNKNOWN = object()


@dataclass(frozen=True)
class SuffixedNumber:
    value: int | float
    suffix: str


def literal_key(value, *, keep_suffix=True):
    if isinstance(value, (list, tuple)):
        return type(value), tuple(literal_key(item, keep_suffix=keep_suffix) for item in value)
    if isinstance(value, SuffixedNumber) and not keep_suffix:
        value = value.value
    return type(value), value


def distinguishes_pair(arguments, peer, first, second, repeated):
    left, right = peer.get(first, UNKNOWN), peer.get(second, UNKNOWN)
    if UNKNOWN in (left, right):
        return False
    left, right, repeated = (literal_key(value, keep_suffix=False)
                             for value in (left, right, repeated))
    # A one-slot change isolates the pair only when the rest of the input agrees.
    return left != right and (repeated not in (left, right) or arguments)


def symmetric_input(name, literals, arguments, peers, *, observed):
    """Two keyword duplicates are not a positional mix-up risk."""
    seen = []
    for position, value in literals.items():
        if value is UNKNOWN:
            continue
        identity = (type(value) in {int, float} and value == 0) or value in ("", b"", (), [], {})
        if not identity and value is not None and type(value) is not bool and not (
                isinstance(value, str) and value.startswith("--")):
            key = literal_key(value)
            for earlier, earlier_key in seen:
                if (key == earlier_key and (type(position) is int or type(earlier) is int)
                        and (observed or value not in (1, "0", "1"))
                        and not any(distinguishes_pair(
                            all(arguments.get(slot) == other.get(slot)
                                for slot in arguments.keys() | other.keys() if slot not in (earlier, position)),
                            peer, earlier, position, value,
                        ) for other, peer in peers)):
                    return True
            seen.append((position, key))
        if (isinstance(value, list) and len(value) >= 2
                and all(type(item) is type(value[0]) and item == value[0] for item in value[1:])):
            return True
        if (isinstance(value, (str, bytes, tuple, list)) and len(value) >= 2
                and ORDERED_NAME.search(name) and value == value[::-1]):
            return True
    return False


def numeric_leaves(value):
    if type(value) in {int, float, SuffixedNumber}:
        yield literal_key(value)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from numeric_leaves(child)


def weak_fixed_values(values):
    """Find a neutral literal or equal numeric coordinates in fixed arguments."""
    numbers = set()
    for value in values:
        number = value.value if isinstance(value, SuffixedNumber) else value
        if (type(number) in {int, float} and number in {-1, 0, 1}) or value in ([], (), {}):
            return True
        for number in numeric_leaves(value):
            if number in numbers:
                return True
            numbers.add(number)
    return False


def unvaried_arguments(calls):
    """Yield a first call line when checked calls vary a literal and all calls share another."""
    by_callee = {}
    for callee, location, literals, checked in calls:
        by_callee.setdefault(callee, []).append((location, literals, checked))
    for group in by_callee.values():
        checked = [literals for _, literals, checked in group if checked]
        if len(checked) < 2:
            continue

        varies = False
        fixed = []
        for position in {position for literals in checked for position in literals}:
            samples = [literals.get(position, UNKNOWN) for literals in checked]
            known = [value for value in samples
                     if value is not UNKNOWN and value is not None and type(value) is not bool]
            if not known:
                continue
            first = literal_key(known[0])
            varies |= any(literal_key(sample) != first for sample in known[1:])
            if all(literal_key(literals.get(position, UNKNOWN)) == first for _, literals, _ in group):
                fixed.append(known[0])
        if varies and weak_fixed_values(fixed):
            yield min(location for location, _, _ in group)[0]


def name_claims(test_name):
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+", test_name)
    words = [word.lower() for word in words]
    every = any(
        word in {"every", "each", "all", "any"}
        and any(noun in {"input", "inputs", "field", "fields", "argument", "arguments"}
                for noun in words[index + 1:index + 4])
        for index, word in enumerate(words)
    )
    absent = {word[:-4] for word in words if word.endswith("less") and len(word) > 4}
    general = any(word in {"empty", "missing", "none", "zero", "without", "no"} for word in words)
    return every, absent, general


def empty_input(value):
    if isinstance(value, SuffixedNumber):
        value = value.value
    return value is None or (type(value) is not bool and value in (0, "", b"", [], (), {}))


def named_arguments(test_name, calls, parameter_names):
    """Yield the first invocation of a callee when its examples miss the named case."""
    every, absent, general = name_claims(test_name)
    if not (every or absent or general):
        return
    by_callee = {}
    for callee, location, inputs, observed, feeds_check in calls:
        by_callee.setdefault(callee, []).append((location, inputs, observed, feeds_check))
    observed_callees = sum(any(observed for _, _, observed, _ in group)
                           for group in by_callee.values())
    for callee, group in by_callee.items():
        checked = [inputs for _, inputs, observed, _ in group if observed]
        fixtures = [inputs for _, inputs, _, feeds_check in group if feeds_check]
        samples = [inputs for _, inputs, _, _ in group]
        positions = set().union(*(inputs.keys() for inputs in samples))
        fixed = {position for position in positions
                 if samples[0].get(position, UNKNOWN) is not UNKNOWN
                 and all(literal_key(inputs.get(position, UNKNOWN)) == literal_key(samples[0][position])
                         for inputs in samples)}
        can_check_variety = len(checked) >= 2 or (len(fixtures) >= 2 and bool(positions - fixed))
        uncovered = every and can_check_variety and bool(fixed)
        if checked:
            empty = {position for position in positions
                     if any(empty_input(inputs.get(position, UNKNOWN)) for inputs in samples)}
            # A named parameter can show which empty argument belongs to a “...less” case.
            named = parameter_names(callee)
            specific = {position for position in positions if named.get(position) in absent}
            missing_specific = any(
                position not in empty
                and all(inputs.get(position, UNKNOWN) is not UNKNOWN for inputs in samples)
                for position in specific
            )
            all_literal = all(inputs and UNKNOWN not in inputs.values() for inputs in checked)
            missing_general = (bool(general or absent and not specific) and observed_callees == 1
                               and not empty and all_literal)
            uncovered |= missing_specific or missing_general
        if uncovered:
            yield min(location for location, _, _, _ in group)[0]


def loose_bounds(checks):
    by_callee = {}
    for callee, location, inputs, bounded in checks:
        by_callee.setdefault(callee, []).append((location, inputs, bounded))
    for group in by_callee.values():
        if all(bounded for _, _, bounded in group):
            for location, inputs, _ in group:
                if inputs and all(value is not UNKNOWN for value in inputs.values()):
                    yield location[0]


def fixed_boundary(calls):
    """Find a shared nonzero coordinate alongside coincident floating-point endpoints."""
    if len(calls) < 2:
        return False
    first = calls[0]
    positions = {position for position, value in first.items() if type(value) is float}
    fixed = {position for position in positions if first[position] != 0
             and all(literal_key(inputs.get(position, UNKNOWN)) == literal_key(first[position]) for inputs in calls)}
    moving = positions - fixed
    return (bool(fixed) and len(moving) >= 2
            and all(len(numbers := [inputs.get(position, UNKNOWN) for position in moving
                                    if type(inputs.get(position, UNKNOWN)) is float]) > len(set(numbers))
                    for inputs in calls))


def assertion(name):
    return name.startswith(("assert", "prop_assert", "debug_assert"))


def helper(name):
    return name.lower().startswith(("assert", "check", "expect")) or name in {"raises", "fail"}


def find(paths):
    rows = []
    for path, tests, comments in scan_paths(paths, rows):
        allows = {}
        for comment in comments:
            match = re.fullmatch(r"tq:\s*allow\s+([\w-]+)(?:\s+(.*))?", comment.text)
            if not match:
                continue
            rule, reason = match.groups()
            if not reason:
                rows.append(finding(path, comment.line, "invalid-suppression",
                                    f"Suppression for {rule} needs a reason."))
            elif rule not in RULES:
                rows.append(finding(path, comment.line, "invalid-suppression",
                                    f"Unknown rule {rule}."))
            else:
                allows[comment.line, rule] = comment.standalone

        issues = set()
        for test in tests:
            issues.update(test.issues)
            if test.guarded:
                issues.add((test.line, "guarded-assert"))
        for line, rule in sorted(issues):
            nearby = [(line, rule), (line - 1, rule)]
            for key in nearby:
                if key in allows and (key[0] == line or allows[key]):
                    del allows[key]
                    break
            else:
                advisory = rule in ADVISORY
                rows.append(finding(path, line, rule, ("advisory: " if advisory else "") + RULES[rule][0],
                                    "advisory" if advisory else "error"))
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["rule"]))


def different_callees(actual, expected):
    return (actual and expected and min(len(actual), len(expected)) == 1
            and actual.isdisjoint(expected)
            and len({path[:-1] for path in actual | expected}) == 1)
