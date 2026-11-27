from collections import Counter, defaultdict
from pathlib import Path

from . import repo


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def precision(events):
    latest = {}
    for entry in events:
        if entry["event"] == "verdict":
            latest[entry["repo"], entry["rule_version"], entry["row"]] = entry
        elif entry["event"] == "lint":
            latest.setdefault((entry["repo"], entry["rule_version"], entry["row"]), entry)
    counts = defaultdict(Counter)
    for entry in latest.values():
        counts[entry["rule"], entry["rule_version"]][entry.get("verdict", "unjudged")] += 1
    result = defaultdict(dict)
    for (rule, version), count in sorted(counts.items()):
        real, false, intended = count["real"], count["false"], count["intended"]
        result[rule][version] = dict(real=real, false=false, intended=intended,
                                     unjudged=count["unjudged"], strict=ratio(real, sum(count.values())),
                                     loose=ratio(real, real + false))
    return dict(result)


def lifecycle(events):
    phases = defaultdict(lambda: dict(born=0, died=0, survived=0))
    aliases = {}
    current = {}
    versions = {}
    born = {}
    dead = set()
    changes = [entry for entry in events if entry["event"] in {"born", "changed", "died"}]
    for entry in sorted(changes, key=lambda entry: (entry["generation"], entry["commit"])):
        repository = entry["repo"]
        before, after = entry["before"], entry["after"]
        identity = repository, entry["test_id"]
        if before:
            identity = versions.get((repository, before["test"], before["hash"]), identity)
        if entry["event"] == "born":
            born[identity] = entry["phase"]
            phases[entry["phase"]]["born"] += 1
        if after:
            aliases[repository, after["test"]] = identity
            versions[repository, after["test"], after["hash"]] = identity
            current[identity] = after["test"]
        elif before:
            aliases[repository, before["test"]] = identity
            versions[repository, before["test"], before["hash"]] = identity
            current[identity] = before["test"]
        if entry["event"] == "died" and identity not in dead:
            phases[born.get(identity, "unknown")]["died"] += 1
            dead.add(identity)
    for identity, phase in born.items():
        if identity not in dead:
            phases[phase]["survived"] += 1
    return dict(phases), aliases, versions, current, dead


def kills(events, aliases, versions, current, dead):
    def identity(repository, test, body=None):
        return versions.get((repository, test, body), aliases.get((repository, test), (repository, test)))

    touched, failed = set(), set()
    killed, unique = defaultdict(set), defaultdict(set)
    for entry in events:
        repository = entry["repo"]
        if entry["event"] == "kill":
            bodies = entry.get("bodies", {})
            red = {identity(repository, test, bodies.get(test)) for test in entry["red"]}
            touched.update(red)
            touched.update(identity(repository, test, bodies.get(test)) for test in entry["green"])
            for test in red:
                killed[test].add(entry["mutant"])
            if len(red) == 1:
                unique[next(iter(red))].add(entry["mutant"])
        elif entry["event"] == "run" and entry.get("status") in {"failed", "flaky"}:
            failed.add(identity(repository, entry["test"]))

    names = {test: current.get(test, test[1]) for test in touched | failed | current.keys()}
    duplicates = Counter(names.values())
    within_repo = Counter((test[0], name) for test, name in names.items())
    tests = {}
    candidates = []
    for test, name in sorted(names.items(), key=lambda item: (item[1], item[0])):
        if within_repo[test[0], name] > 1:
            name = f"{test[0]}::{name} [{test[1][:12]}]"
        elif duplicates[name] > 1:
            name = f"{test[0]}::{name}"
        tests[name] = dict(kills=len(killed[test]), unique_kills=len(unique[test]),
                           observed=test in touched, failed=test in failed)
        if test in touched and test not in failed and test not in dead and not unique[test]:
            candidates.append(name)
    return tests, sorted(candidates)


def summarize(events):
    phases, aliases, versions, current, dead = lifecycle(events)
    tests, candidates = kills(events, aliases, versions, current, dead)
    allows = {(entry["repo"], entry["rule"], entry["path"], entry["hash"], entry["offset"])
              for entry in events if entry["event"] == "lint" and "reason" in entry}
    return dict(phases=phases, precision=precision(events), tests=tests, deletion_candidates=candidates,
                allows=dict(sorted(Counter(rule for _, rule, _, _, _ in allows).items())))


def summarize_global(events, selection):
    if selection is not None:
        normalized = repo.normalize_remote(selection, Path.cwd())
        matching = {entry["repo"] for entry in events
                    if selection in {entry["repo"], entry.get("repo_name"), repo.remote_name(entry["repo"])}
                    or normalized == entry["repo"]}
        events = [entry for entry in events if entry["repo"] in matching]
    grouped = defaultdict(list)
    for entry in events:
        grouped[entry["repo"]].append(entry)
    return summarize(events) | {"repositories": {name: summarize(grouped[name]) for name in sorted(grouped)}}


def print_report(report):
    def percent(fraction):
        return "n/a" if fraction is None else f"{fraction:.1%}"

    print("Phase              Born  Died  Survived")
    for phase, row in sorted(report["phases"].items()):
        print(f"{phase:<18} {row['born']:>4}  {row['died']:>4}  {row['survived']:>8}")
    print("\nRule  Version  Real  Intended  False  Unjudged  Strict  Loose")
    for rule, versions in report["precision"].items():
        for version, row in versions.items():
            print(f"{rule}  {version}  {row['real']}  {row['intended']}  {row['false']}"
                  f"  {row['unjudged']}  {percent(row['strict'])}  {percent(row['loose'])}")
    print("\nRule  Allows")
    for rule, count in report["allows"].items():
        print(f"{rule}  {count}")
    print("\nKills  Unique  Test")
    for test, row in report["tests"].items():
        print(f"{row['kills']:>5}  {row['unique_kills']:>6}  {test}")
    print("\nDeletion candidates")
    for test in report["deletion_candidates"]:
        print(test)
