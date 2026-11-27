"""CLI entry points for sliced test gates and pinned fault replays."""

from contextlib import chdir
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from . import history, ledger, ledger_config, lint, pinned_mutants, repo, scratch
from .pinned_mutants import MUTANT_ID
from .source import note_owners, scan_paths
from .test_runner import DEFAULT, Runner


PIN_REFERENCE = re.compile(rf"\b{MUTANT_ID}\b")
REACHES = re.compile(r"reaches:\s*when\s+(.+?),\s*then\s+(.+)", re.IGNORECASE)
COMMAND_ERRORS = (OSError, ValueError, SyntaxError, subprocess.SubprocessError)


def add_commands(commands):
    mutants = commands.add_parser("mutants", help="Check pinned faulty implementations.")
    check_mutants = mutants.add_subparsers(required=True).add_parser("check")
    check_mutants.add_argument("--since")
    check_mutants.add_argument("--cmd", default=DEFAULT)
    check_mutants.set_defaults(slice_command=mutants_check)

    slices = commands.add_parser("slice", help="Check tests in a red or green slice.")
    check_slice = slices.add_subparsers(required=True).add_parser("check")
    check_slice.add_argument("number", type=int)
    check_slice.add_argument("--feature")
    check_slice.add_argument("--base", required=True)
    check_slice.add_argument("--cmd", required=True)
    check_slice.add_argument("--after-green", action="store_true")
    check_slice.set_defaults(slice_command=slice_check)

    revert = commands.add_parser("revert-check", help="Run one test with a fault and with HEAD restored.")
    revert.add_argument("test")
    fault = revert.add_mutually_exclusive_group(required=True)
    fault.add_argument("--mutant")
    fault.add_argument("--revert")
    revert.add_argument("--cmd", required=True)
    revert.set_defaults(slice_command=revert_check)


def ast_parser():
    try:
        from .ast_grep_lint import parse
    except ModuleNotFoundError as error:
        if error.name != "ast_grep_py":
            raise
        raise ValueError("Slice tools require the ast-grep-py package") from error
    return parse


def repository_ledger(repository):
    main = repo.main_worktree(repository)
    local, shared = ledger_config.resolve(main, None, None)
    return ledger.RepositoryLedger(local.path, shared.path, repository, main)


def error_text(error):
    if isinstance(error, subprocess.CalledProcessError) and error.stderr:
        detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
        return detail.strip() or str(error)
    return str(error)


@dataclass(frozen=True)
class TestCase:
    path: Path
    test: object
    notes: tuple[str, ...]

    @property
    def name(self):
        return f"{self.path}::{self.test.qualified}"

    @property
    def catches(self):
        return {pin for note in self.notes if note.startswith("catches:")
                for pin in PIN_REFERENCE.findall(note.removeprefix("catches:"))}

    def aliases(self, runner):
        return {self.name, self.test.qualified, self.test.identifier, runner.selector(self.path, self.test)}


def test_cases(directory, parser, select=None):
    errors = []
    paths = sorted(set(repo.tracked(directory)) if select is None else select.changed)
    with chdir(directory):
        paths = [path for path in paths if path.is_file()]
        cases = []
        for path, tests, comments in scan_paths(paths, errors, parser=parser, inventory=True):
            chosen = tests if select is None else select(path, tests)
            notes = {id(test): [] for test in chosen}
            for comment in comments:
                if comment.text.startswith(("catches:", "reaches:")):
                    for owner in note_owners(path, tests, comment, inventory=True):
                        if id(owner) in notes:
                            notes[id(owner)].append(comment.text)
            for test in chosen:
                cases.append(TestCase(path, test, tuple(notes[id(test)])))
    return cases, errors


def find_test(cases, runner, name):
    by_name = [case for case in cases if name in case.aliases(runner)]
    if not by_name:
        by_name = [case for case in cases if name == case.test.name]
    if len(by_name) != 1:
        raise ValueError(f"{'Ambiguous' if by_name else 'Unknown'} test: {name}")
    return by_name[0]


def run_test(directory, runner, case):
    return runner.run(directory, case.path, runner.selector(case.path, case.test))


def mutant_event(log, batch, commit, mutant, outcome, detail, runs=()):
    return ledger.event("mutant", (batch, mutant.id), repo=log.main.name, commit=commit,
                        mutant=mutant.id, assumption=mutant.assumes, flip=mutant.flip,
                        file=str(mutant.file), outcome=outcome, detail=detail, tests=list(runs))


def replay_mutants(repository, commit, runner, mutants, cases, stale, log):
    outcomes = []
    batch = uuid4().hex
    for mutant in mutants:
        runs = []
        if mutant.id in stale:
            outcome, detail = "stale", "the replacement span changed after the pinned block"
        else:
            with scratch.worktrees(repository) as copies:
                broken = copies.add("broken", commit)
                outcome, detail = pinned_mutants.apply(broken, mutant)
                if outcome is None:
                    outcome = "killed"
                    for number, name in enumerate(mutant.tests, 1):
                        try:
                            case = find_test(cases, runner, name)
                        except ValueError as error:
                            outcome, detail = "survived", str(error)
                            break
                        restored = copies.add(f"restored-{number}", commit)
                        baseline = run_test(restored, runner, case)
                        mutated = run_test(broken, runner, case)
                        runs.append(dict(test=case.name, baseline=baseline.row(case.name),
                                         mutated=mutated.row(case.name)))
                        if baseline.outcome != "pass" or mutated.outcome != "assertion":
                            outcome, detail = "survived", f"{case.name}: expected pass then assertion"
                            detail += f", observed {baseline.outcome} then {mutated.outcome}"
                            if baseline.detail and baseline.outcome != "pass":
                                detail += "\n" + baseline.detail
                            if mutated.detail and mutated.outcome != "assertion":
                                detail += "\n" + mutated.detail
        row = mutant_event(log, batch, commit, mutant, outcome, detail, runs)
        log.write([row])
        outcomes.append(row)
        print(f"{mutant.id} {outcome} {mutant.file}" + (f": {detail}" if detail else ""))
    return outcomes


def mutants_check(options):
    repository = repo.root(".")
    commit = repo.resolve(repository, "HEAD")
    runner, parser = Runner(options.cmd), ast_parser()
    log = repository_ledger(repository)
    with scratch.worktrees(repository) as copies:
        directory = copies.add("inventory", commit)
        mutants = list(pinned_mutants.discover(directory).values())
        if options.since:
            base = repo.resolve(repository, options.since)
            mutants = list(pinned_mutants.changed_pins(repository, directory, base, commit, mutants))
        stale = pinned_mutants.stale_mutants(repository, commit, mutants, directory)
        cases, _ = test_cases(directory, parser) if set(mutant.id for mutant in mutants) - stale else ([], [])
    rows = replay_mutants(repository, commit, runner, mutants, cases, stale, log)
    return int(any(row["outcome"] != "killed" for row in rows))


def green_gate(repository, directory, commit, base, runner, cases, parser, reasons):
    pins = pinned_mutants.discover(directory)
    inventory, _ = test_cases(directory, parser)
    referenced = set().union(*(case.catches for case in cases))
    for name in sorted(referenced - pins.keys()):
        reasons.append(f"{name}: no pinned mutant in HEAD")
    slice_pins = {pin.id for pin in pinned_mutants.changed_pins(
        repository, directory, base, commit, list(pins.values()))}
    selected = []
    for name in sorted(slice_pins - referenced):
        reasons.append(f"{name}: no slice test cites the pinned mutant")
    for name in sorted((referenced & pins.keys()) | slice_pins):
        pin = pins[name]
        owners = set()
        for name in pin.tests:
            try:
                owners.add(find_test(inventory, runner, name).name)
            except ValueError as error:
                reasons.append(f"{pin.id}: {error}")
        if any(case.name not in owners for case in cases if pin.id in case.catches):
            reasons.append(f"{pin.id}: killed-by does not name every slice test that cites it")
        selected.append(pin)
    stale = pinned_mutants.stale_mutants(repository, commit, selected, directory)
    rows = replay_mutants(repository, commit, runner, selected, inventory, stale,
                          repository_ledger(repository))
    for row in rows:
        if row["outcome"] != "killed":
            reasons.append(f"{row['mutant']}: {row['outcome']}")
    return rows


def append_gate(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [entry]
    if entry["outcome"] == "pass":
        records.append({key: entry[key] for key in ("feature", "slice", "commit", "after_green")}
                       | {"event": "gate-ok"})
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
        stream.write("".join(ledger.encode(record) + "\n" for record in records).encode())
        stream.flush()
        os.fsync(stream.fileno())


def slice_check(options):
    repository = repo.root(".")
    feature = options.feature or repo.main_worktree(repository).name
    if options.number < 1 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", feature):
        raise ValueError("Use a positive slice number and a feature name without path separators")
    path = pinned_mutants.safe_path(repository, Path("slices") / feature / f"{options.number:02d}.jsonl")
    commit = repo.resolve(repository, "HEAD")
    reasons, tests, mutants = [], [], []
    try:
        base = repo.resolve(repository, options.base)
        runner, parser = Runner(options.cmd), ast_parser()
        with scratch.worktrees(repository) as copies:
            directory = copies.add("slice", commit)
            select = history.ChangedTests(directory, base, parser=parser)
            cases, errors = test_cases(directory, parser, select)
            reasons.extend(f"{row['path']}:{row['line']}: {row['message']}" for row in errors)
            with chdir(directory):
                found = lint.find(["."], select=select, parser=parser)
            reasons.extend(f"{row['path']}:{row['line']} {row['rule']}: {row['message']}"
                           for row in found.findings if row["level"] != "advisory")
            if not cases:
                reasons.append("No tests changed since base")
            for case in cases:
                if not case.catches:
                    reasons.append(f"{case.name}: catches: must name a mutant ID")
                reaches = [note for note in case.notes if note.startswith("reaches:")]
                if ((case.test.guarded or case.test.conditional or reaches)
                        and (not reaches or not all(REACHES.fullmatch(note) for note in reaches))):
                    reasons.append(f"{case.name}: reaches: must say 'when X, then Y'")
            if options.after_green:
                mutants = green_gate(repository, directory, commit, base, runner, cases, parser, reasons)
            else:
                for case in cases:
                    run = run_test(directory, runner, case)
                    tests.append(run.row(case.name))
                    if run.outcome != "assertion":
                        reasons.append(f"{case.name}: expected assertion failure, observed {run.outcome}"
                                       + (f"\n{run.detail}" if run.detail else ""))
    except COMMAND_ERRORS as error:
        reasons.append(error_text(error))
    outcome = "fail" if reasons else "pass"
    append_gate(path, dict(event="gate", feature=feature, slice=options.number, commit=commit,
                           base=options.base, after_green=options.after_green, outcome=outcome,
                           reasons=reasons, tests=tests, mutants=mutants))
    print(f"Slice {feature}/{options.number:02d}: {outcome}")
    for reason in reasons:
        print(reason)
    return int(bool(reasons))


def revert_check(options):
    repository = repo.root(".")
    runner, parser = Runner(options.cmd), ast_parser()
    log = repository_ledger(repository)
    before = scratch.Fingerprint.read(repository)
    commit = before.head
    reasons, tests = [], []
    before_restore, after = [], None
    name = options.test
    bodies = {}
    mutant = options.mutant or options.revert
    with scratch.worktrees(repository, keep_on_failure=True) as copies:
        try:
            broken = copies.add("broken", commit)
            cases, _ = test_cases(broken, parser)
            case = find_test(cases, runner, name)
            name = case.name
            bodies[name] = ledger_body(broken, case)
            if options.mutant:
                pins = pinned_mutants.discover(broken)
                if mutant not in pins:
                    raise ValueError(f"Unknown pinned mutant: {mutant}")
                pin = pins[mutant]
                if pinned_mutants.stale_mutants(repository, commit, [pin], broken):
                    raise ValueError(f"{mutant}: stale")
                outcome, detail = pinned_mutants.apply(broken, pin)
                if outcome:
                    raise ValueError(f"{mutant}: {outcome}: {detail}")
            else:
                mutant = repo.resolve(repository, options.revert)
                repo.git(broken, "-c", "core.hooksPath=/dev/null", "-c", "rerere.enabled=false",
                         "revert", "--no-commit", mutant)
            red = run_test(broken, runner, case)
            tests.append(red.row(name))
            check_run(red, "Broken", "assertion", reasons)
            before_restore = before.changes(scratch.Fingerprint.read(repository))
            if not before_restore:
                restored = copies.add("restored", commit)
                green = run_test(restored, runner, case)
                tests.append(green.row(name))
                check_run(green, "Restored", "pass", reasons)
        except COMMAND_ERRORS as error:
            reasons.append(error_text(error))
        finally:
            try:
                after = scratch.Fingerprint.read(repository)
                changes = before.changes(after)
            except COMMAND_ERRORS as error:
                changes = [f"comparison failed: {error_text(error)}"]
            changes = list(dict.fromkeys(before_restore + changes))
            reasons.extend(f"LIVE WORKTREE CHANGED: {change}" for change in changes)
            for reason in reasons:
                print(reason)
            run = uuid4().hex
            outcomes = [test["outcome"] for test in tests]
            passed = not reasons and outcomes == ["assertion", "pass"]
            log.write([ledger.event(
                "kill", (run, mutant), repo=log.main.name, run=run, mutant=mutant,
                operator="revert-check", commit=commit, worktree=False, outcome="pass" if passed else "fail",
                red=[name] if passed else [],
                green=[name] if not changes and outcomes == ["pass", "pass"] else [],
                bodies=bodies, tests=tests, reasons=reasons,
                live_before=before.report(), live_after=after.report() if after is not None else None,
            )])
            copies.passed = passed
    return int(not copies.passed)


def check_run(run, label, expected, reasons):
    print(f"{label}: {run.outcome} ({run.classifier})")
    if run.outcome != expected:
        reasons.append(f"{label}: expected {expected}, observed {run.outcome}\n{run.detail}")


def ledger_body(directory, case):
    """Use the history ledger's legacy body hash where it inventories this test."""
    if history.source_path(case.path):
        with chdir(directory):
            for _, tests, _ in scan_paths([case.path], [], inventory=True):
                for test in tests:
                    if test.qualified == case.test.qualified:
                        return test.body_hash
    return case.test.body_hash
