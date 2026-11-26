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
        "Actual and expected values come from the same function or module.",
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


def symmetric_input(name, literals, keywords=()):
    """`keywords` flags, per literal, a Python keyword argument; two keyword duplicates are not a mix-up risk."""
    seen = []
    keywords = list(keywords) + [False] * (len(literals := list(literals)) - len(keywords))
    for value, keyword in zip(literals, keywords):
        if value is UNKNOWN:
            continue
        identity = (type(value) in {int, float} and value == 0) or value in ("", b"", (), [])
        if not identity and value is not None and type(value) is not bool and not (
                isinstance(value, str) and value.startswith("--")):
            key = (type(value), value)
            if any(key == earlier and not (keyword and earlier_keyword)
                   for earlier, earlier_keyword in seen):
                return True
            seen.append((key, keyword))
        if (isinstance(value, list) and len(value) >= 2
                and all(type(item) is type(value[0]) and item == value[0] for item in value[1:])):
            return True
        if (isinstance(value, (str, bytes, tuple, list)) and len(value) >= 2
                and ORDERED_NAME.search(name) and value == value[::-1]):
            return True
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
        fixed = False
        for position in {position for literals in checked for position in literals}:
            samples = [literals.get(position, UNKNOWN) for literals in checked]
            known = [(type(value), value) for value in samples
                     if value is not UNKNOWN and value is not None and type(value) is not bool]
            if not known:
                continue
            first = known[0]
            varies |= any(sample != first for sample in known[1:])
            all_samples = [literals.get(position, UNKNOWN) for _, literals, _ in group]
            fixed |= all((type(value), value) == first for value in all_samples)
        if varies and fixed:
            yield min(location for location, _, _ in group)[0]


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
