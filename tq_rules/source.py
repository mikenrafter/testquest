from collections import defaultdict
from dataclasses import dataclass, field
import os
from pathlib import Path
import tokenize


@dataclass(frozen=True)
class Comment:
    line: int
    column: int
    text: str
    standalone: bool


@dataclass
class Test:
    name: str
    identifier: str
    line: int
    start: int
    end: int
    column: int = 0
    issues: list = field(default_factory=list)
    guarded: bool = False
    conditional: bool = False
    measured: bool = False
    qualified: str = ""
    body_hash: str = ""
    checks: int = 0
    span_columns: tuple[int, int] | None = None

    def owns(self, comment):
        return self.start <= comment.line <= self.end and comment.column >= self.column

    def owns_inventory_comment(self, comment):
        if self.span_columns is None:
            return self.owns(comment)
        first, last = self.span_columns
        return (self.start, first) <= (comment.line, comment.column) < (self.end, last)


class CollectionControls:
    """Resolve exact copies of a detector among files in the same lint scan."""

    def __init__(self):
        self.witnesses = defaultdict(set)
        self.pending = []

    def record(self, path, scan_root, fingerprint, test=None, line=None):
        markers = ({"Cargo.toml", ".git"} if path.suffix == ".rs"
                   else {"pyproject.toml", "setup.py", "setup.cfg", ".git"})
        path = path.resolve()
        key = project_root(path, scan_root, markers), path.suffix, fingerprint
        if test is None:
            self.witnesses[key].add(path)
        else:
            self.pending.append((key, path, test, line))

    def finish(self):
        for key, path, test, line in self.pending:
            if not self.witnesses.get(key, set()) - {path}:
                test.issues.append((line, "absence-no-control"))


IGNORED = {".git", "__pycache__", ".venv", "node_modules", "target"}


def scan_paths(inputs, errors, *, refine_guards=False, collection_controls=None, inventory=False):
    """Yield parsed files; append a parse-error finding to `errors` for each unreadable one."""
    from . import python, rust

    paths = {}
    cwd = Path.cwd()
    for item in inputs:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir():
            directory_root = path.resolve() if refine_guards else None
            for directory, children, files in os.walk(path):
                children[:] = [child for child in children if child not in IGNORED]
                for name in files:
                    candidate = Path(directory, name)
                    if candidate.suffix in {".py", ".rs"} and not candidate.is_symlink():
                        paths[candidate] = directory_root
        elif path.suffix in {".py", ".rs"} and not path.is_symlink():
            paths.setdefault(path, cwd if refine_guards else None)
    for path in sorted(paths, key=str):
        if not path.is_file():
            continue
        try:
            if path.suffix == ".py":
                with tokenize.open(path) as stream:
                    parsed = python.parse(stream.read(), path, scan_root=paths[path],
                                          collection_controls=collection_controls, inventory=inventory)
            else:
                parsed = rust.parse(path.read_text(encoding="utf-8"), path, scan_root=paths[path],
                                    collection_controls=collection_controls, inventory=inventory)
        except (SyntaxError, ValueError, UnicodeDecodeError) as error:
            errors.append(finding(path, getattr(error, "lineno", None) or 1, "parse-error",
                                  f"Cannot parse: {error}"))
            continue
        yield path, *parsed


def finding(path, line, rule, message, level="error"):
    return dict(path=str(path), line=line, rule=rule, message=message, level=level)


def project_root(path, scan_root, markers):
    """Bound sibling resolution to a marked project or the scan directory."""
    path = path.resolve()
    for parent in path.parents:
        if any((parent / marker).exists() for marker in markers):
            return parent
        if parent == scan_root:
            return parent
    return path.parent
