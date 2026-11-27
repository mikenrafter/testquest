"""Manage detached worktrees and compare live-tree contents around a replay."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from . import repo


@contextmanager
def worktrees(repository, *, keep_on_failure=False):
    directory = Path(tempfile.gettempdir()).resolve()
    if directory.is_relative_to(repository):
        directory = Path("/tmp").resolve()
    if directory.is_relative_to(repository):
        raise ValueError("No scratch directory outside the repository")
    root = Path(tempfile.mkdtemp(prefix="tq-replay-", dir=directory))
    scratch = Scratch(repository, root)
    try:
        yield scratch
    finally:
        if keep_on_failure and not scratch.passed:
            print(f"Scratch kept at {root}")
        else:
            scratch.remove()


class Scratch:
    def __init__(self, repository, root):
        self.repository = repository
        self.root = root
        self.added = []
        self.passed = False

    def add(self, name, revision):
        path = self.root / name
        # Disable checkout hooks for scratch copies. Hook execution would precede the runner.
        repo.git(self.repository, "-c", "core.hooksPath=/dev/null",
                 "worktree", "add", "--detach", str(path), revision)
        self.added.append(path)
        return path

    def remove(self):
        for path in reversed(self.added):
            repo.git(self.repository, "worktree", "remove", "--force", str(path))
        shutil.rmtree(self.root)


@dataclass(frozen=True)
class Fingerprint:
    status: bytes
    head: str
    index: bytes
    files: dict

    @classmethod
    def read(cls, repository):
        status = repo.git(repository, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all")
        index = repo.git(repository, "ls-files", "--stage", "-z")
        # Including ignored files also detects writes that never appear in porcelain.
        names = repo.git(repository, "ls-files", "--cached", "--others", "-z").split(b"\0")
        files = {}
        for name in {os.fsdecode(raw) for raw in names if raw}:
            path = repository / name
            if path.parent.resolve() == path.parent:
                files[name] = file_hash(path)
            else:
                files[name] = ("symlinked-parent",)
        return cls(status, repo.resolve(repository, "HEAD"), index, files)

    def report(self):
        files = repr(sorted(self.files.items())).encode("utf-8", "surrogateescape")
        return dict(status=os.fsdecode(self.status), head=self.head, files=len(self.files),
                    files_sha256=hashlib.sha256(files).hexdigest(),
                    index_sha256=hashlib.sha256(self.index).hexdigest())

    def changes(self, after):
        changes = []
        if self.status != after.status:
            changes.append("git status --porcelain changed")
        if self.head != after.head:
            changes.append("HEAD changed")
        if self.index != after.index:
            changes.append("index entries changed")
        names = sorted(name for name in self.files.keys() | after.files.keys()
                       if self.files.get(name) != after.files.get(name))
        if names:
            changes.append("file hashes changed: " + ", ".join(repr(name) for name in names))
        return changes


def file_hash(path):
    try:
        status = path.lstat()
    except FileNotFoundError:
        return ("missing",)
    mode = status.st_mode
    if stat.S_ISLNK(mode):
        return mode, hashlib.sha256(os.fsencode(path.readlink())).hexdigest()
    if stat.S_ISREG(mode):
        with path.open("rb") as stream:
            return mode, hashlib.file_digest(stream, "sha256").hexdigest()
    if stat.S_ISDIR(mode):
        # A tracked gitlink appears as a directory. Include its files without Git metadata.
        return mode, tuple((child.name, file_hash(child)) for child in sorted(path.iterdir())
                           if child.name != ".git")
    return mode, status.st_rdev
