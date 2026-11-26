from contextlib import chdir
import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import repo
from .source import scan_paths


NOTE = re.compile(r"catches:\s*(.+?)(?:\s+\[patch:\s*([^\]]+)\])?$")
TIMEOUT = 60


def snapshot(repository, destination, worktree):
    if not worktree:
        archive = repo.git(repository, "archive", "HEAD")
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            files.extractall(destination, filter="data")
        return

    for relative in repo.tracked(repository):
        source = repository / relative
        if source.parent.resolve() != source.parent:
            raise ValueError(f"Tracked file has a symlinked parent directory: {relative}")
        output = destination / relative
        if source.is_symlink():
            target = source.readlink()
            if target.is_absolute() or not (output.parent / target).resolve().is_relative_to(destination):
                raise ValueError(f"Tracked symlink leaves the replay copy: {relative}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(target)
        elif source.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)


def invoke(command, directory, environment):
    try:
        with subprocess.Popen(command, cwd=directory, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, errors="replace",
                              start_new_session=True) as process:
            try:
                out, err = process.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            return subprocess.CompletedProcess(command, process.returncode, out, err), ""
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)


def relative_patch(directory, patch):
    path = (directory / patch).resolve()
    if Path(patch).is_absolute() or not path.is_relative_to(directory) or not path.is_file():
        raise ValueError(f"Patch must name a file in the replay snapshot: {patch}")
    return path


def replay(base, patch, command, temp, environment):
    try:
        path = relative_patch(base, patch)
    except ValueError as error:
        return "patch-failed", str(error)

    with tempfile.TemporaryDirectory(dir=temp, prefix="fault-") as directory:
        mutant = Path(directory) / "mutant"
        shutil.copytree(base, mutant, symlinks=True)
        applied, error = invoke(["git", "apply", "--", str(path)], mutant, environment)
        if not applied or applied.returncode != 0:
            return "patch-failed", error or applied.stderr.strip()

        baseline = Path(directory) / "baseline"
        shutil.copytree(base, baseline, symlinks=True)
        clean, error = invoke(command, baseline, environment)
        if not clean or clean.returncode:
            detail = error or (clean.stderr or clean.stdout).strip()
            return "error", f"Baseline did not pass. {detail}"

        result, error = invoke(command, mutant, environment)
        if not result:
            return "error", error
        if result.returncode == 0:
            return "green", ""
        if result.returncode in {1, 101}:
            return "red", (result.stderr or result.stdout).strip()
        return "error", f"Test command exited {result.returncode}. {(result.stderr or result.stdout).strip()}"


def run(repository, worktree, command):
    repository = repo.root(repository)
    template = shlex.split(command)
    if not template:
        raise ValueError("--cmd requires a test command")
    environment = repo.git_environment()
    rows = []
    with tempfile.TemporaryDirectory(prefix="tq-catches-") as temp:
        base = Path(temp).resolve() / "source"
        environment["GIT_CEILING_DIRECTORIES"] = str(base.parent)
        base.mkdir()
        snapshot(repository, base, worktree)
        with chdir(base):
            notes = [
                (path, comment.line, test.identifier, *match.groups())
                for path, tests, comments in scan_paths([Path(".")], [])
                for comment in comments if (match := NOTE.fullmatch(comment.text))
                for test in tests if test.owns(comment)
            ]
        for path, line, test, description, patch in sorted(notes, key=lambda note: note[:4]):
            if patch:
                argv = [part.replace("{test}", test) for part in template]
                status, detail = replay(base, patch.strip(), argv, temp, environment)
            else:
                status, detail = "unreplayable", ""
            rows.append(dict(path=str(path), line=line, test=test, description=description,
                             status=status, detail=detail))
    return rows
