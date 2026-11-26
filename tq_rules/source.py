from dataclasses import dataclass, field
from pathlib import Path


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

    def owns(self, comment):
        return self.start <= comment.line <= self.end and comment.column >= self.column


IGNORED = {".git", "__pycache__", ".venv", "node_modules", "target"}


def scan_paths(inputs):
    from . import python, rust

    paths = set()
    for item in inputs:
        path = Path(item)
        if not path.exists():
            raise FileNotFoundError(path)
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if (candidate.suffix in {".py", ".rs"} and not candidate.is_symlink()
                    and candidate.is_file() and not IGNORED.intersection(candidate.parts)):
                paths.add(candidate)
    for path in sorted(paths, key=str):
        adapter = python if path.suffix == ".py" else rust
        yield path, *adapter.parse(path.read_text(encoding="utf-8"), path)


def finding(path, line, rule, message):
    return dict(path=str(path), line=line, rule=rule, message=message)
