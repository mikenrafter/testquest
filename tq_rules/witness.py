import re

from .ast_grep_languages import ECMASCRIPT_LANGUAGES
from .source import finding, note_owners, scan_paths


REACHES = re.compile(r"reaches:\s*([\w.:/-]+)")


def find(paths, *, parser=None):
    rows = []
    files = list(scan_paths(paths, rows, parser=parser))
    names = {}
    for path, tests, _ in files:
        for test in tests:
            labels = {test.name, test.identifier}
            if path.suffix in ECMASCRIPT_LANGUAGES:
                labels.add(test.qualified)
            for label in labels:
                names.setdefault(label, set()).add(id(test))

    for path, tests, comments in files:
        ecmascript = path.suffix in ECMASCRIPT_LANGUAGES
        witnessed = set()
        for comment in comments:
            if ecmascript and comment.text.startswith("reaches:"):
                label = comment.text.removeprefix("reaches:").strip()
            elif match := REACHES.fullmatch(comment.text):
                label = match[1]
            else:
                continue
            targets = names.get(label, set())
            if targets:
                witnessed.update(id(test) for test in note_owners(path, tests, comment) if targets - {id(test)})
        for test in tests:
            if test.measured or not (test.guarded or test.conditional):
                continue
            if id(test) not in witnessed:
                rows.append(finding(path, test.line, "missing-witness",
                                    "Name a scanned test that reaches the condition, or measure coverage here."))
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["rule"]))
