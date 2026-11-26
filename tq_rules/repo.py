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
