import re

from .source import finding, scan_paths


RULES = {
    "const-truth": (
        "Assertion contains a predicate that is always true.",
        "A deliberately trivial assertion checks framework plumbing.",
    ),
    "self-compare": (
        "Assertion compares the same expression on both sides.",
        "Reflexivity or repeated-call behavior is the stated property.",
    ),
    "no-assert": (
        "Test has no assertion, expected exception, or recognized checking helper.",
        "Successful execution is the explicit smoke-test contract, or a helper enforces the outcome.",
    ),
    "guarded-assert": (
        "Every check is conditional and the test can skip them all.",
        "The test or a named witness independently proves that the checked branch is reached.",
    ),
    "absence-no-control": (
        "Recorder has no positive check showing it can observe an action.",
        "An independent observation in the same test establishes that the recorder can fire.",
    ),
    "self-derived-expected": (
        "Actual and expected values come from the same function or module.",
        "The test checks repeatability, a before-and-after observation, or a relation anchored by an independent oracle.",
    ),
    "nondistinguishing-literal": (
        "Symmetric input can hide an ordering or argument mix-up.",
        "The test's WHY explains the symmetry, such as an equal-input boundary, independent flags, or an intentional repeated option.",
    ),
}
ORDERED_NAME = re.compile(r"reverse|order|swap|endian|rotate|crc|encode", re.I)
UNKNOWN = object()


def symmetric_input(name, literals):
    seen = []
    for value in literals:
        if value is UNKNOWN:
            continue
        if value is not None and type(value) is not bool and not (
                isinstance(value, str) and value.startswith("--")):
            key = (type(value), value)
            if key in seen:
                return True
            seen.append(key)
        if (isinstance(value, list) and len(value) >= 2
                and all(type(item) is type(value[0]) and item == value[0] for item in value[1:])):
            return True
        if (isinstance(value, (str, bytes, tuple, list)) and len(value) >= 2
                and ORDERED_NAME.search(name) and value == value[::-1]):
            return True
    return False


def helper(name):
    return name.lower().startswith(("assert", "check", "expect")) or name in {"raises", "fail"}


def find(paths):
    rows = []
    for path, tests, comments in scan_paths(paths):
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
                rows.append(finding(path, line, rule, RULES[rule][0]))
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["rule"]))
