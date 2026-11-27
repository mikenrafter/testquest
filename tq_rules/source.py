from collections import defaultdict
from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import tokenize

from .ast_grep_languages import EXTENDED_LANGUAGES, ECMASCRIPT_OUTPUT


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


def note_owners(path, tests, comment, *, inventory=False):
    extended = path.suffix in EXTENDED_LANGUAGES
    if inventory or extended:
        owners = [test for test in tests if test.owns_inventory_comment(comment)]
    else:
        owners = [test for test in tests if test.owns(comment)]
    if extended and owners:
        return [max(owners, key=lambda test: (test.start, test.column, -test.end))]
    return owners


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

    def merge(self, other):
        for key, paths in other.witnesses.items():
            self.witnesses[key].update(paths)
        self.pending.extend(other.pending)

    def finish(self):
        for key, path, test, line in self.pending:
            if not self.witnesses.get(key, set()) - {path}:
                test.issues.append((line, "absence-no-control"))


IGNORED = {".git", "__pycache__", ".venv", "node_modules", "target"}


def scan_paths(inputs, errors, *, refine_guards=False, collection_controls=None, inventory=False, parser=None):
    """Yield parsed files and append a diagnostic for each skipped file."""
    if parser is None:
        from . import python, rust

    paths = {}
    cwd = Path.cwd()
    legacy_suffixes = {".py", ".rs"}
    suffixes = legacy_suffixes | EXTENDED_LANGUAGES.keys() if parser else legacy_suffixes
    for item in inputs:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir():
            directory_root = path.resolve() if refine_guards else None
            scripts = []
            for directory, children, files in os.walk(path):
                children[:] = [child for child in children if child not in IGNORED]
                for name in files:
                    candidate = Path(directory, name)
                    if candidate.suffix in legacy_suffixes and not candidate.is_symlink():
                        paths[candidate] = directory_root
                    elif (candidate.suffix in suffixes and not candidate.is_symlink()
                          and not ECMASCRIPT_OUTPUT.intersection(candidate.relative_to(path).parts[:-1])):
                        scripts.append(candidate)
            paths.update(dict.fromkeys(unignored_scripts(path, scripts), directory_root))
        elif path.suffix in suffixes and not path.is_symlink():
            paths.setdefault(path, cwd if refine_guards else None)
    try:
        for path in sorted(paths, key=str):
            if not path.is_file():
                continue
            controls = CollectionControls() if collection_controls is not None else None
            try:
                if path.suffix == ".py":
                    with tokenize.open(path) as stream:
                        source = stream.read()
                    parse = parser or python.parse
                else:
                    source = path.read_text(encoding="utf-8")
                    parse = parser or rust.parse
                parsed = parse(source, path, scan_root=paths[path],
                               collection_controls=controls, inventory=inventory)
            except (RecursionError, MemoryError):
                errors.append(finding(path, 1, "too-deep",
                                      "advisory: Scan skipped after exceeding parser limits.", "advisory"))
                continue
            except (SyntaxError, ValueError, UnicodeDecodeError) as error:
                errors.append(finding(path, getattr(error, "lineno", None) or 1, "parse-error",
                                      f"Cannot parse: {error}"))
                continue
            if controls is not None:
                collection_controls.merge(controls)
            yield path, *parsed
    finally:
        if parser is None:
            python.parse_imported_module.cache_clear()


def unignored_scripts(root, paths):
    """Apply the repository's Git ignore rules to discovered sources."""
    if not paths:
        return []
    relatives = {os.fsencode(path.relative_to(root)): path for path in paths}
    try:
        result = subprocess.run(["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
                                input=b"\0".join(relatives) + b"\0", capture_output=True)
    except FileNotFoundError:
        return paths
    ignored = set(result.stdout.split(b"\0")) if result.returncode == 0 else set()
    return [path for relative, path in relatives.items() if relative not in ignored]


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
