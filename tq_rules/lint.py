from dataclasses import dataclass
import re

from .source import CollectionControls, finding, scan_paths


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
        "Test has no check that verifies the outcome under test.",
        "The stated check is successful execution, such as passing output through a strict parser.",
    ),
    "guarded-assert": (
        "Every check is conditional and the test can skip them all.",
        "The branch is exercised by a named witness, or zero matching items is explicitly a valid case.",
    ),
    "absence-no-control": (
        "No check shows that the recorder or detector can report a result.",
        "An independent observation establishes that the recorder can fire.",
    ),
    "self-derived-expected": (
        "The expected value reuses production code from the module under test.",
        "The test checks repeatability, a before-and-after observation, or a declared relation between different inputs or entry points.",
    ),
    "nondistinguishing-literal": (
        "The fixture or assertion can let a wrong implementation pass.",
        "The test's WHY explains the symmetric or fixed input. Examples include equal weights, a zero-write case, or a test that isolates another parameter.",
    ),
}
ADVISORY = {"nondistinguishing-literal", "self-derived-expected"}
ORDERED_NAME = re.compile(r"reverse|order|swap|endian|rotate|crc|encode", re.I)
REPEATABILITY = re.compile(r"(?<![a-z])(?:determin(?:ism|istic)|stabl(?:e|ity)|same|repeat\w*|reproducib\w*|idempotent|consistent)(?![a-z])", re.I)
DISTINCT_INPUTS = re.compile(
    r"(?<![a-z])(?:(?:both|two)[ _-]+(?:inputs?|lines?|keys?|paths?|requests?|files?|jobs?)"
    r"|(?:different|distinct)[ _-]+(?:inputs?|lines?|keys?|paths?|calls?|requests?|files?|jobs?))(?![a-z])",
    re.I,
)
OUTCOME_CLAIM = re.compile(
    r"(?:^|_)(?:returns_none|rejects|fails_when|errors_on|is_empty|truncat(?:es|ed)_before"
    r"|prints?_\w+?_to_(?:stderr|stdout))(?:_|$)")
EFFECT_CLAIM = re.compile(
    r"(?:^|_)(?:(?:prints|writes|sends|truncates|rejects|retries|records|emits|logs|deletes|creates)(?:_|$)"
    r"|(?:idempotent|request_format)$)"
)
EFFECT_CONTEXT = re.compile(r"_(?:after|before|when|while|without)_")
UNKNOWN = object()


def claims_effect(name):
    claim = EFFECT_CONTEXT.split(name, maxsplit=1)[0]
    return bool(EFFECT_CLAIM.search(claim))


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
        if (observed and isinstance(value, list) and len(value) >= 2
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


def fixed_operand(value, operators):
    """A neutral arithmetic operand or a coordinate whose role could be swapped."""
    if isinstance(value, SuffixedNumber):
        value = value.value
    if type(value) in {int, float}:
        return ((value == 0 and bool(operators & {"+", "-"}))
                or (value == 1 and bool(operators & {"*", "/"})))
    return isinstance(value, (list, tuple)) and bool(operators)


def unvaried_arguments(calls, relevant):
    """Yield a first call line when checked calls vary a literal and all calls share another."""
    by_callee = {}
    for callee, location, literals, checked in calls:
        by_callee.setdefault(callee, []).append((location, literals, checked))
    for callee, group in by_callee.items():
        checked = [literals for _, literals, checked in group if checked]
        if len(checked) < 2:
            continue

        varies = False
        fixed = {}
        for position in {position for literals in checked for position in literals}:
            samples = [literals.get(position, UNKNOWN) for literals in checked]
            known = [value for value in samples
                     if value is not UNKNOWN and value is not None and type(value) is not bool]
            if not known:
                continue
            first = literal_key(known[0])
            varies |= any(literal_key(sample) != first for sample in known[1:])
            if all(literal_key(literals.get(position, UNKNOWN)) == first for _, literals, _ in group):
                fixed[position] = known[0]
        if varies and weak_fixed_values(fixed.values()):
            line = min(location for location, _, _ in group)[0]
            if weak_fixed_values(relevant(callee, line, fixed)):
                yield line


def argument_claims(test_name):
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+", test_name)
    words = [word.lower() for word in words]
    every = any(
        word in {"every", "each", "all", "any"}
        and any(noun in {"input", "inputs", "field", "fields", "argument", "arguments"}
                for noun in words[index + 1:index + 4])
        for index, word in enumerate(words)
    )
    absent = {word[:-4] for word in words if word.endswith("less") and len(word) > 4}
    return every, absent


def named_arguments(calls, claims, parameter_names):
    """Check explicit coverage claims, including absence tied to a named parameter."""
    every, absent = claims
    by_callee = {}
    for callee, location, inputs, observed, feeds_check in calls:
        by_callee.setdefault(callee, []).append((location, inputs, observed, feeds_check))
    for group in by_callee.values():
        checked = [inputs for _, inputs, observed, _ in group if observed]
        fixtures = sum(feeds_check for _, _, _, feeds_check in group)
        samples = [inputs for _, inputs, _, _ in group]
        positions = set().union(*(inputs.keys() for inputs in samples))
        fixed = {position for position in positions
                 if samples[0].get(position, UNKNOWN) is not UNKNOWN
                 and all(literal_key(inputs.get(position, UNKNOWN)) == literal_key(samples[0][position])
                         for inputs in samples)}
        missing_input = every and bool(fixed) and (len(checked) >= 2 or (fixtures >= 2 and positions - fixed))
        if absent and len(checked) >= 2:
            names = parameter_names(callee)
            # CLAIM(tq-named-absence): an exact parameter and variation in a separate empty slot
            # identify which input cases the test claims [test_rust_negative_case_names].
            # ↳ CLAIM(tq-named-absence)
            for position in fixed:
                if names.get(position) not in absent:
                    continue
                value = samples[0][position]
                missing_input |= (not empty_input(value) and any(
                    any(empty_input(inputs.get(other, UNKNOWN)) for inputs in checked)
                    and any(inputs.get(other, UNKNOWN) is not UNKNOWN and
                            not empty_input(inputs[other]) for inputs in checked)
                    for other in positions - fixed))
        if missing_input:
            yield min(location for location, _, _, _ in group)[0]


def empty_input(value):
    if isinstance(value, SuffixedNumber):
        value = value.value
    return value is None or (type(value) is not bool and value in (0, "", b"", [], (), {}))


def loose_bounds(checks):
    by_callee = {}
    for callee, location, inputs, bounded, exempt in checks:
        by_callee.setdefault(callee, []).append((location, inputs, bounded, exempt))
    for group in by_callee.values():
        if all(bounded for _, _, bounded, _ in group):
            exempt_locations = {location for location, _, _, exempt in group if exempt}
            for location, inputs, _, _ in group:
                if location not in exempt_locations and inputs and UNKNOWN not in inputs.values():
                    yield location[0]


def prefix_satisfies(needles, prefix, body):
    """One alternative matches the fixed prefix while another requires the dynamic body."""
    return (any(needle in text for text in prefix for needle in needles)
            and any(any(needle in text for text in body) and not any(needle in text for text in prefix)
                    for needle in needles))


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


def find(paths, *, select=None, owners=None):
    rows = []
    controls = CollectionControls()
    files = list(scan_paths(paths, rows, refine_guards=True, collection_controls=controls,
                            inventory=select is not None))
    if select is not None:
        rows.clear()
    controls.finish()
    for path, tests, comments in files:
        file_rows = []
        allows = {}
        for comment in comments:
            match = re.fullmatch(r"tq:\s*allow\s+([\w-]+)(?:\s+(.*))?", comment.text)
            if not match:
                continue
            rule, reason = match.groups()
            if not reason:
                file_rows.append(finding(path, comment.line, "invalid-suppression",
                                    f"Suppression for {rule} needs a reason."))
            elif rule not in RULES:
                file_rows.append(finding(path, comment.line, "invalid-suppression",
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
                file_rows.append(finding(path, line, rule, ("advisory: " if advisory else "") + RULES[rule][0],
                                    "advisory" if advisory else "error"))
        if select is None:
            rows.extend(file_rows)
        elif file_rows:
            selected = select(path, tests)
            for row in file_rows:
                owner = next((test for test in selected if test.start <= row["line"] <= test.end), None)
                if owner is not None:
                    rows.append(row)
                    if owners is not None:
                        owners[row["path"], row["line"]] = owner
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["rule"]))


def different_callees(actual, expected):
    return (actual and expected and min(len(actual), len(expected)) == 1
            and actual.isdisjoint(expected)
            and len({path[:-1] for path in actual | expected}) == 1)


def inverse_codecs(first, second):
    if not first or not second or first[:-1] != second[:-1]:
        return False
    names = {first[-1], second[-1]}
    return (names in ({"encode", "decode"}, {"compress", "decompress"})
            or any(name.startswith("encode_") and "decode_" + name[7:] in names for name in names))


IMPORT_FIELDS = {"imports", "frameworks_detected"}
IMPORT_TOKEN = re.compile(r"\b(?:import|from|require|use|extern)\b")


def path_directories(segments):
    pieces = tuple(part for segment in segments for part in segment.replace("\\", "/").split("/") if part)
    return pieces[:-1] if pieces and pieces[-1].rpartition(".")[0] else pieces


def pins_path(directories, strings):
    named = [value.replace("\\", "/").split("/") for value in strings]
    return all(any(piece in parts for parts in named) for piece in directories)
