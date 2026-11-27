import os
import re
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit, urlunsplit


SCP_REMOTE = re.compile(r"(?P<user>[^/@:]+@)?(?P<host>\[[^\]]+\]|[^/:]+):(?P<path>.+)")


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


def main_worktree(repository):
    common_dir = os.fsdecode(git(
        repository, "rev-parse", "--path-format=absolute", "--git-common-dir",
    )).removesuffix("\n")
    if Path(common_dir) == repository / ".git":
        return repository
    first = os.fsdecode(git(repository, "worktree", "list", "--porcelain", "-z").split(b"\0", 1)[0])
    return Path(first.removeprefix("worktree "))


def identity(repository, main):
    """Return a normalized origin URL, or the main worktree path when origin is absent."""
    try:
        origin = os.fsdecode(git(repository, "remote", "get-url", "origin")).removesuffix("\n")
    except subprocess.CalledProcessError:
        return str(main)
    return normalize_remote(origin, main)


def normalize_remote(remote, base):
    if "://" in remote:
        parts = urlsplit(remote)
        if parts.scheme == "file" and parts.hostname in {None, "localhost"}:
            return str((base / unquote(parts.path)).resolve())
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None and port != {"http": 80, "https": 443, "ssh": 22, "git": 9418}.get(parts.scheme):
            host += f":{port}"
        if parts.scheme == "ssh" and parts.username:
            host = f"{parts.username}@{host}"
        path = parts.path.rstrip("/").removesuffix(".git")
        return urlunsplit((parts.scheme, host, path, "", ""))

    scp = SCP_REMOTE.fullmatch(remote)
    if scp:
        return (scp["user"] or "") + scp["host"].lower() + ":" + scp["path"].rstrip("/").removesuffix(".git")
    return str((base / remote).resolve())


def remote_name(repository):
    scp = SCP_REMOTE.fullmatch(repository)
    if "://" in repository:
        path = urlsplit(repository).path
    else:
        path = scp["path"] if scp else repository
    return path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


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
