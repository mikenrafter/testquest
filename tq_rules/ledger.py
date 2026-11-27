import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
from uuid import uuid4

from . import catches, history, lint, repo


RULES_VERSION = "tq-rules-1"
CACHE_VERSION = "dd4d81b"
VERDICTS = ("real", "intended", "false")
DEFAULT_COMMAND = "python3 -m unittest {test}"


def add_commands(parser):
    parser.add_argument("--ledger", type=Path)
    commands = parser.add_subparsers(dest="log_command", required=True)
    record = commands.add_parser("record", help="Record test changes from Git history.")
    record.add_argument("--since", required=True)
    scan = commands.add_parser("lint", help="Lint changed tests and record their findings.")
    scan.add_argument("--since", required=True)
    scan.add_argument("paths", nargs="*", default=["."])
    scan.add_argument("--json", action="store_true")
    verdict = commands.add_parser("verdict", help="Record a judgment on a lint row.")
    verdict.add_argument("--row", required=True)
    verdict.add_argument("--verdict", required=True, choices=VERDICTS)
    verdict.add_argument("--why", required=True)
    verdict.add_argument("--pattern")
    verdict.add_argument("--judge")
    cache = commands.add_parser("import-verdicts", help="Import judgments from the rounds cache.")
    cache.add_argument("file", type=Path)
    kill = commands.add_parser("kill", help="Record the results of named-fault replays.")
    kill.add_argument("--repo", default=".")
    kill.add_argument("--worktree", action="store_true")
    kill.add_argument("--cmd", default=DEFAULT_COMMAND)
    report = commands.add_parser("report", help="Summarize test survival, precision and kills.")
    report.add_argument("--json", action="store_true")
    for command in (record, scan, verdict, cache, kill, report):
        command.add_argument("--ledger", type=Path, default=argparse.SUPPRESS)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def key(*values):
    return history.digest(encode(values))


def event(kind, identity, **fields):
    return dict(event=kind, id=key(kind, *identity), **fields)


def read_objects(stream, path):
    objects = []
    for number, line in enumerate(stream, 1):
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("expected a JSON object")
        except ValueError as error:
            raise ValueError(f"{path}:{number}: {error}") from error
        objects.append(item)
    return objects


class Ledger:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def edit(self):
        """Lock reading, deduplication and durable append as one operation."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            events = self.read_events(stream)
            yield events, lambda additions: self.append(stream, events, additions)

    def read_events(self, stream):
        events = read_objects(stream, self.path)
        for number, entry in enumerate(events, 1):
            if not all(isinstance(entry.get(field), str) for field in ("id", "event", "repo")):
                raise ValueError(f"{self.path}:{number}: ledger event requires id, event and repo")
        return events

    def append(self, stream, events, additions):
        seen = {entry["id"] for entry in events}
        new = []
        for entry in additions:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                new.append(encode(entry).encode())
        if not new:
            return
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                new.insert(0, b"")
        stream.write(b"\n".join(new) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())

    def write(self, additions):
        with self.edit() as (_, append):
            append(additions)

    def read(self):
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return []
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            return self.read_events(stream)


def age(birth, death):
    return max(0, int((datetime.fromisoformat(death) - datetime.fromisoformat(birth)).total_seconds()))


def record(repository, since, existing):
    base = repo.resolve(repository, since)
    head = repo.resolve(repository, "HEAD")
    previous = defaultdict(list)
    observed_kills = defaultdict(list)
    for entry in existing:
        if entry["repo"] != repository.name:
            continue
        if entry["event"] in {"born", "changed", "died"}:
            previous[entry["commit"]].append(entry)
        elif entry["event"] == "kill":
            observed_kills[entry["commit"]].append(entry)

    pending = {entry[0]: entry[1:] for entry in history.commits(repository, base, head)}
    states = {}
    kills_by_test = defaultdict(set)
    additions = []
    for commit, parents, generation in history.commit_generations(repository, base, head):
        inherited = [states[parent] for parent in parents]
        first = inherited[0] if inherited else {}
        if len(inherited) > 1:
            active = {}
            for state in reversed(inherited):
                active.update(state)
        else:
            active = first
        if commit in pending:
            changes = record_commit(repository, commit, parents, generation, pending[commit],
                                    inherited[1:], first, kills_by_test)
            additions.extend(changes)
        else:
            changes = previous.get(commit, ())
        if changes:
            active = active.copy()
        for entry in changes:
            old, new = entry["before"], entry["after"]
            identity = entry["test_id"]
            birth = entry["date"] if entry["event"] == "born" else None
            if old and (known := active.pop(old["test"], None)) and known[0] == old["hash"]:
                _, identity, birth = known
            if new:
                active[new["test"]] = new["hash"], identity, birth
        states[commit] = active
        for observation in observed_kills.get(commit, ()):
            for test in observation["red"]:
                body = observation.get("bodies", {}).get(test)
                known = active.get(test)
                identity = (known[1] if known and (body is None or known[0] == body)
                            else key(repository.name, test, body))
                kills_by_test[identity].add(observation["mutant"])
    return additions


def record_commit(repository, commit, parents, generation, metadata, other_parents, active, kills_by_test):
    date, author, phase, co_authors, build_loop = metadata
    before, after, moves = history.versions_in_commit(repository, commit, parents)
    additions = []
    for old, new in history.compare(before, after, moves):
        if old == new:
            continue
        current = new or old
        old_ref = old.ref() if old else None
        new_ref = new.ref() if new else None
        known = active.get(old.test) if old else None
        if old is None and any(
            (inherited := state.get(new.test)) and inherited[0] == new.hash for state in other_parents
        ):
            continue
        if known and known[0] == old.hash:
            _, identity, birth = known
        elif old:
            identity = key(repository.name, old.test, old.hash)
            birth = None
        else:
            identity = key(repository.name, new.test, new.hash, commit)
            birth = date
        fields = dict(repo=repository.name, test=current.test, test_id=identity, hash=current.hash,
                      commit=commit, generation=generation, date=date, phase=phase,
                      author=author, co_authors=co_authors, build_loop=build_loop, claim=current.claim,
                      catches=list(current.catches), before=old_ref, after=new_ref)
        if old is None:
            kind = "born"
        elif new is None:
            kind = "died"
            fields.update(reason="deleted", lifetime_seconds=age(birth, date) if birth else None,
                          kills=len(kills_by_test.get(identity, ())))
        else:
            kind = "changed"
            fields.update(checks_before=old.checks, checks_after=new.checks, loosened=new.checks < old.checks)
        additions.append(event(kind, (repository.name, commit, old_ref, new_ref), **fields))
    return additions


def rules_version():
    source = Path(__file__).resolve().parent
    try:
        repository = repo.root(source)
        relative = source.relative_to(repository)
        return repo.git(repository, "rev-parse", "--verify", f"HEAD:{relative}").decode().strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return RULES_VERSION


def lint_events(repository, rows, owners):
    run = uuid4().hex
    version = rules_version()
    lines_by_path = {}
    for row in rows:
        path = row["path"]
        if path not in lines_by_path:
            lines_by_path[path] = history.read_source(Path(path)).splitlines()
        owner = owners[path, row["line"]]
        relative = os.path.relpath(path, repository)
        lines = lines_by_path[path]
        hash_ = history.digest("\n".join(lines[owner.line - 1:owner.end]))[:16]
        offset = row["line"] - owner.line
        row_id = key(version, repository.name, relative, row["rule"], hash_, offset)
        yield event("lint", (run, row_id), **(row | dict(
            repo=repository.name, path=relative, rule_version=version,
            row=row_id, hash=hash_, offset=offset, test=f"{relative}::{owner.qualified}", run=run,
        )))


def judgment(entries, options):
    row = next((entry for entry in reversed(entries)
                if entry["event"] == "lint" and entry["row"] == options.row), None)
    if row is None:
        raise ValueError(f"No lint row {options.row} in the ledger")
    if not options.why.strip():
        raise ValueError("--why requires a reason")
    fields = {name: row[name] for name in ("repo", "row", "rule", "rule_version")}
    return event("verdict", (uuid4().hex,), **fields, verdict=options.verdict,
                 why=options.why, pattern=options.pattern, judge=options.judge)


def import_verdicts(path):
    with path.open("rb") as stream:
        entries = read_objects(stream, path)
    additions = []
    for number, entry in enumerate(entries, 1):
        required = ("repo", "path", "rule", "hash", "verdict", "why")
        if (not all(isinstance(entry.get(field), str) and entry[field].strip() for field in required)
                or type(entry.get("offset")) is not int or entry["verdict"] not in VERDICTS):
            raise ValueError(f"{path}:{number}: invalid rounds cache verdict")
        fields = {field: entry.get(field) for field in (*required, "offset", "line", "test",
                                                        "pattern", "judge", "round")}
        row = key(CACHE_VERSION, *(fields[field] for field in ("repo", "path", "rule", "hash", "offset")))
        additions.append(event("verdict", (CACHE_VERSION, fields), **fields, row=row,
                               rule_version=CACHE_VERSION))
    return additions


def kill(repository, worktree, command):
    if not any("{test}" in part for part in shlex.split(command)):
        raise ValueError("log kill --cmd requires a {test} placeholder to attribute results")
    commit = repo.resolve(repository, "HEAD")
    rows = catches.run(repository, worktree, command, inventory=True, revision=commit)
    outcomes = defaultdict(lambda: {"red": set(), "green": set(), "bodies": {}})
    for row in rows:
        if "mutant" in row:
            outcomes[row["mutant"]][row["status"]].add(row["test"])
            outcomes[row["mutant"]]["bodies"][row["test"]] = row["body_hash"]
    run = uuid4().hex
    events = [
        event("kill", (run, mutant), repo=repository.name, run=run, mutant=mutant,
              operator="catches", commit=commit, worktree=worktree,
              red=sorted(tests["red"]), green=sorted(tests["green"] - tests["red"]), bodies=tests["bodies"])
        for mutant, tests in sorted(outcomes.items())
    ]
    return rows, events
