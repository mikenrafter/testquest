import re

from .source import finding, scan_paths


REACHES = re.compile(r"reaches:\s*([\w.:/-]+)")


def find(paths):
    files = list(scan_paths(paths))
    names = {}
    for path, tests, _ in files:
        for test in tests:
            for name in {test.name, test.identifier}:
                names.setdefault(name, set()).add((path, test.line))

    rows = []
    for path, tests, comments in files:
        for test in tests:
            if test.measured or not (test.guarded or test.conditional):
                continue
            locations = [
                names.get(match[1], set())
                for comment in comments if test.owns(comment)
                if (match := REACHES.fullmatch(comment.text))
            ]
            if not any(candidates - {(path, test.line)} for candidates in locations):
                rows.append(finding(path, test.line, "missing-witness",
                                    "Name a scanned test that reaches the condition, or measure coverage here."))
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["rule"]))
