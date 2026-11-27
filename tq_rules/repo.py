import os
from pathlib import Path
import subprocess


def git(repository, *arguments):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, check=True,
        env=git_environment(),
    ).stdout


def git_environment():
    local = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True,
                           text=True, check=True).stdout.splitlines()
    return {key: value for key, value in os.environ.items() if key not in local}


def root(path):
    return Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel")).rstrip("\n"))


def tracked(repository):
    return [Path(os.fsdecode(name)) for name in git(repository, "ls-files", "-z").split(b"\0") if name]


def resolve(repository, ref):
    return git(repository, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def untracked(repository):
    return {Path(os.fsdecode(name)) for name in
            git(repository, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name}


def differences(repository, *refs, renames=False):
    """Yield old path, new path, and the regular-file blobs on each side, if present."""
    flags = ("--find-renames",) if renames else ("--no-renames",)
    fields = iter(git(repository, "diff", "--raw", "-z", "--no-abbrev", *flags, *refs, "--").split(b"\0"))
    for header in fields:
        if not header:
            break
        old_mode, new_mode, old_blob, new_blob, status = header[1:].decode().split()
        old_path = Path(os.fsdecode(next(fields)))
        new_path = Path(os.fsdecode(next(fields))) if status.startswith("R") else old_path
        yield (old_path, new_path, regular_blob(old_mode, old_blob), regular_blob(new_mode, new_blob))


def regular_blob(mode, blob):
    return blob if mode.startswith("100") and blob.strip("0") else None
