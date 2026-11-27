from collections import defaultdict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tokenize

from . import repo
from .source import scan_paths


PHASES = {"red", "green", "white-box", "re-derivation", "hardening", "review-fix", "bug-fix"}
HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
CLAIM = re.compile(r"\bCLAIM\(([^)]+)\)")


def source_path(path):
    return path.suffix in {".py", ".rs"}


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def read_source(path):
    if path.suffix == ".py":
        with tokenize.open(path) as stream:
            return stream.read()
    return path.read_text(encoding="utf-8")


def read_blobs(repository, blobs):
    if not blobs:
        return {}
    stream = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "--batch"],
        input="".join(f"{blob}\n" for blob in sorted(set(blobs))).encode(),
        capture_output=True, check=True, env=repo.git_environment(),
    ).stdout
    result = {}
    offset = 0
    while offset < len(stream):
        end = stream.index(b"\n", offset)
        blob, kind, size = stream[offset:end].decode().split()
        if kind != "blob":
            raise ValueError(f"Expected a Git blob, got {kind} for {blob}")
        offset = end + 1
        end = offset + int(size)
        result[blob] = stream[offset:end]
        offset = end + 1
    return result


def scan_blobs(repository, files, *, require_parse=True, parser=None):
    """Read Git blobs into an isolated scan directory and return tests with their source."""
    blobs = read_blobs(repository, list(files.values()))
    with tempfile.TemporaryDirectory(prefix="tq-history-") as directory:
        base = Path(directory)
        for path, blob in files.items():
            output = base / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(blobs[blob])
        errors = []
        for path, tests, comments in scan_paths([base / path for path in files], errors, inventory=True, parser=parser):
            if tests:
                yield path.relative_to(base), tests, comments, read_source(path).splitlines()
        if errors and require_parse:
            row = errors[0]
            relative = Path(row["path"]).relative_to(base)
            raise ValueError(f'Unable to inventory {relative}:{row["line"]}: {row["message"]}')


def overlaps(test, start, count):
    return count > 0 and test.start < start + count and start <= test.end


class ChangedTests:
    """Select current tests intersected by Git hunks, including removals in the old span."""

    def __init__(self, repository, ref, *, parser=None):
        self.repository = repository
        self.parser = parser
        self.ref = repo.resolve(repository, ref)
        self.untracked = repo.untracked(repository)
        self.changed = {
            new: (old, blob) for old, new, blob, _ in
            repo.differences(repository, self.ref, renames=True) if source_path(new)
        }

    def __call__(self, path, tests):
        relative = Path(os.path.relpath(path, self.repository))
        if relative in self.untracked:
            return tests
        if not tests or relative not in self.changed:
            return []
        old, blob = self.changed[relative]
        paths = [relative] if old == relative else [old, relative]
        patch = repo.git(self.repository, "--literal-pathspecs", "diff", "--find-renames", "--no-color",
                         "--no-ext-diff", "--no-textconv", "--no-indent-heuristic", "-U0",
                         self.ref, "--", *map(str, paths))
        hunks = [tuple(int(value or b"1") for value in groups) for groups in HUNK.findall(patch)]
        selected = [test for test in tests if any(overlaps(test, start, count)
                                                 for _, _, start, count in hunks)]
        old_hunks = [(start, count) for start, count, _, _ in hunks if count]
        if old_hunks and blob and len(selected) < len(tests):
            previous_files = scan_blobs(self.repository, {old: blob}, require_parse=False, parser=self.parser)
            previous = {test.qualified for _, old_tests, _, _ in previous_files for test in old_tests
                        if any(overlaps(test, start, count) for start, count in old_hunks)}
            selected.extend(test for test in tests if test not in selected and test.qualified in previous)
        return selected


@dataclass(frozen=True)
class TestVersion:
    path: Path
    qualified: str
    hash: str
    source: str
    checks: int
    catches: tuple
    claim: str | None

    @property
    def test(self):
        return f"{self.path}::{self.qualified}"

    def ref(self):
        return {"test": self.test, "hash": self.hash}


def inventory(repository, files):
    versions = {}
    for path, tests, comments, lines in scan_blobs(repository, files):
        for test in tests:
            name = f"{path}::{test.qualified}"
            catches = tuple(comment.text.partition(":")[2].strip()
                            for comment in comments if test.owns_inventory_comment(comment)
                            and comment.text.startswith("catches:"))
            claim = next((match.group(1) for comment in comments if test.owns_inventory_comment(comment)
                          and (match := CLAIM.search(comment.text))), None)
            span = lines[test.start - 1:test.end]
            if test.span_columns is not None:
                first, last = test.span_columns
                span[-1] = span[-1][:last]
                span[0] = span[0][first:]
            source = "\n".join(span)
            versions[name] = TestVersion(path, test.qualified, test.body_hash, digest(source), test.checks, catches, claim)
    return versions


def compare(before, after, moves):
    """Match names first, then exact bodies with one unmatched old and new owner."""
    for name in sorted(before.keys() & after.keys()):
        yield before.pop(name), after.pop(name)
    for name in sorted(before):
        old = before[name]
        if old.path not in moves:
            continue
        moved = f"{moves[old.path]}::{old.qualified}"
        if moved in after and old.hash == after[moved].hash:
            yield before.pop(name), after.pop(moved)
    old_bodies, new_bodies = defaultdict(list), defaultdict(list)
    for version in before.values():
        old_bodies[version.hash].append(version)
    for version in after.values():
        new_bodies[version.hash].append(version)
    for body in sorted(old_bodies.keys() & new_bodies.keys()):
        if len(old_bodies[body]) == len(new_bodies[body]) == 1:
            old, = old_bodies[body]
            new, = new_bodies[body]
            yield before.pop(old.test), after.pop(new.test)
    for old in before.values():
        yield old, None
    for new in after.values():
        yield None, new


def commits(repository, base, head):
    records = repo.git(
        repository, "log", "--reverse", "--topo-order", "-z",
        "--format=%H%x00%aI%x00%an%x00%(trailers:key=Test-Phase,valueonly,separator=%x1f)"
        "%x00%(trailers:key=Co-Authored-By,valueonly,separator=%x1f)"
        "%x00%(trailers:key=Build-Loop-Ledger,valueonly,separator=%x1f)",
        f"{base}..{head}", "--",
    ).decode(errors="replace").split("\0")
    for index in range(0, len(records) - 1, 6):
        commit, date, author, phases, authors, build_loop = records[index:index + 6]
        phase = phases.split("\x1f")[-1].strip()
        yield (commit, date, author, phase if phase in PHASES else "unknown",
               [name.split("<", 1)[0].strip() for name in authors.split("\x1f") if name.strip()],
               build_loop.strip() or None)


def versions_in_commit(repository, commit, parents):
    if not parents:
        files = {}
        for entry in repo.git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if entry:
                header, name = entry.split(b"\t", 1)
                mode, _, blob = header.decode().split()
                path = Path(os.fsdecode(name))
                if source_path(path) and (blob := repo.regular_blob(mode, blob)):
                    files[path] = blob
        return {}, inventory(repository, files), {}
    before, after, moves = {}, {}, {}
    for old, new, old_blob, new_blob in repo.differences(repository, parents[0], commit, renames=True):
        if old_blob and source_path(old):
            before[old] = old_blob
        if new_blob and source_path(new):
            after[new] = new_blob
        if old != new:
            moves[old] = new
    return inventory(repository, before), inventory(repository, after), moves


def commit_generations(repository, base, head):
    """Topological depth orders lifecycle transitions even when an earlier range is backfilled."""
    depths = {}
    graph = repo.git(repository, "rev-list", "--parents", "--reverse", "--topo-order", base, head, "--")
    for line in graph.decode().splitlines():
        commit, *parents = line.split()
        depth = 1 + max((depths[parent] for parent in parents), default=0)
        depths[commit] = depth
        yield commit, parents, depth
