import argparse
import json
import subprocess
import sys

from . import catches, history, ledger, ledger_config, ledger_report, lint, props, repo, risk, witness


DIAGNOSTIC_RULES = {
    "invalid-suppression": (
        "A lint suppression names an unknown rule or gives no reason.",
        "Name a supported lint rule and give a reason for the exception.",
    ),
    "missing-witness": (
        "A conditional test has no evidence that its condition is reached.",
        "Name another scanned test that reaches the condition, or measure coverage in this test.",
    ),
    "too-deep": (
        "The scan skipped a source file after exceeding parser limits.",
        "This advisory diagnostic does not require a suppression.",
    ),
    "parse-error": (
        "A source file could not be parsed.",
        "Fix the source or its encoding before scanning. This diagnostic cannot be suppressed.",
    ),
}


def print_findings(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            detail = f" [row {row['row']}]" if "row" in row else ""
            print("{path}:{line} {rule} {message}".format(**row) + detail)
    return int(any(row.get("level") != "advisory" for row in rows))


def main():
    parser = argparse.ArgumentParser(description="Check how tests observe failures.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("lint", "Find weak assertions and fixtures."),
                            ("witness", "Find conditional checks without reachability evidence.")):
        scan = commands.add_parser(name, help=help_text)
        scan.add_argument("paths", nargs="*" if name == "lint" else "+")
        if name == "lint":
            scan.add_argument("--since")
        scan.add_argument("--json", action="store_true")
    ledger.add_commands(commands.add_parser("log", help="Record and report test evidence."))
    commands.add_parser("rules", help="Show finding rules and exceptions.")
    replay = commands.add_parser("catches", help="Replay named faults in a temporary copy.")
    replay.add_argument("--repo", default=".")
    replay.add_argument("--worktree", action="store_true")
    replay.add_argument("--cmd", default="python3 -m unittest {test}",
                        help="Test command with optional {test} placeholder.")
    replay.add_argument("--json", action="store_true")
    map_risk = commands.add_parser("risk", help="Rank source files by change history and usage.")
    map_risk.add_argument("--repo", default=".")
    map_risk.add_argument("--html")
    map_risk.add_argument("--json", action="store_true")
    map_risk.add_argument("--budget", default=12, type=int)
    lookup = commands.add_parser("props", help="Retrieve cited test properties.")
    query = lookup.add_mutually_exclusive_group(required=True)
    query.add_argument("family", nargs="?")
    query.add_argument("--search")
    lookup.add_argument("--json", action="store_true")
    options = parser.parse_args()
    if options.command == "risk" and options.budget < 0:
        parser.error("--budget must be nonnegative")
    if options.command == "lint" and not (options.paths or options.since):
        parser.error("lint requires paths or --since")
    try:
        if options.command == "lint":
            select = history.ChangedTests(repo.root("."), options.since) if options.since else None
            return print_findings(lint.find(options.paths or ["."], select=select), options.json)
        if options.command == "log":
            return run_log(options)
        if options.command == "witness":
            return print_findings(witness.find(options.paths), options.json)
        if options.command == "catches":
            rows = catches.run(options.repo, options.worktree, options.cmd)
            return print_replays(rows, options.json)
        if options.command == "risk":
            snapshot = risk.map_risk(options.repo, options.budget)
            if options.html:
                risk.write_html(snapshot, options.html)
            if options.json:
                print(json.dumps(snapshot, indent=2))
            elif not options.html:
                print("Score  Fixes  Changes  Importers  Test refs  Test slots  File")
                for row in snapshot["files"]:
                    gap = " (gap)" if row["gap"] else ""
                    print("{score:>5}  {fixes:>5}  {churn:>7}  {importers:>9}  {test_refs:>9}  "
                          "{tests:>10}  {path}".format(**row) + gap)
        if options.command == "props":
            records = props.find(options.family, options.search)
            if options.json:
                print(json.dumps(records, indent=2))
            else:
                for record in records:
                    print("{id}  {variant}  {kind}\n  {law}".format(**record))
                    for citation in record["evidence"]:
                        print("  {source} ({section})".format(**citation))
        if options.command == "rules":
            for name, (message, allowed) in sorted((lint.RULES | DIAGNOSTIC_RULES).items()):
                level = "advisory" if name in lint.ADVISORY or name == "too-deep" else "error"
                print(f"{name}  {message}\n  level: {level}\n  allowed-when: {allowed}")
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"tq: {error}", file=sys.stderr)
        return 2
    return 0


def print_replays(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print("{path}:{line} {status} {test} {description}".format(**row))
            if row["detail"]:
                print(row["detail"])
    statuses = {row["status"] for row in rows}
    if "green" in statuses:
        return 1
    return 2 if statuses & {"error", "patch-failed"} else 0


def run_log(options):
    command = options.log_command
    try:
        repository = repo.root(getattr(options, "repo", "."))
    except subprocess.CalledProcessError as error:
        if command in {"record", "lint", "kill"}:
            raise ValueError(f"log {command} requires a Git worktree") from error
        repository = None
    main = repo.main_worktree(repository) if repository is not None else None
    local, shared = ledger_config.resolve(main, options.ledger, options.global_ledger)
    if command == "where":
        if options.json:
            print(json.dumps({"ledger": local.describe(), "global": shared.describe()}, indent=2))
        else:
            for label, location in (("Per-repository ledger", local), ("Global ledger", shared)):
                print(f"{label}: {location.path or 'none'} ({location.source})")
        return 0
    if command == "report":
        return report_log(options, local, shared)

    log = ledger.RepositoryLedger(local.path, shared.path, repository, main)
    if command == "record":
        log.update(lambda existing: ledger.record(repository, main.name, options.since, existing))
    elif command == "lint":
        owners = {}
        select = history.ChangedTests(repository, options.since)
        rows = lint.find(options.paths, select=select, owners=owners)
        entries = list(ledger.lint_events(repository, main.name, rows, owners))
        log.write(entries)
        return print_findings([
            row | {"row": entry["row"], "rule_version": entry["rule_version"]}
            for row, entry in zip(rows, entries)
        ], options.json)
    elif command == "verdict":
        log.update(lambda existing: [ledger.judgment(existing, options)])
    elif command == "import-verdicts":
        log.write(ledger.import_verdicts(options.file))
    elif command == "kill":
        rows, events = ledger.kill(repository, main.name, options.worktree, options.cmd)
        log.write(events)
        return print_replays(rows, False)
    return 0


def report_log(options, local, shared):
    if options.repository_filter is not None and not options.global_report:
        raise ValueError("log report --repo requires --global")
    location = shared if options.global_report else local
    if location.path is None:
        label = "global" if options.global_report else "per-repository"
        raise ValueError(f"{label} ledger path is unavailable ({location.source})")
    entries = ledger.Ledger(location.path).read()
    report = (ledger_report.summarize_global(entries, options.repository_filter)
              if options.global_report else ledger_report.summarize(entries))
    if options.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        ledger_report.print_report(report)
        for repository, breakdown in report.get("repositories", {}).items():
            print(f"\nRepository: {repository}")
            ledger_report.print_report(breakdown)
    return 0
