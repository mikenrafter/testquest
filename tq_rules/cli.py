import argparse
import json
import subprocess
import sys

from . import catches, lint, props, risk, witness


def print_findings(rows, as_json):
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print("{path}:{line} {rule} {message}".format(**row))
    return int(bool(rows))


def main():
    parser = argparse.ArgumentParser(description="Check how tests observe failures.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("lint", "Find weak assertions and fixtures."),
                            ("witness", "Find conditional checks without reachability evidence.")):
        scan = commands.add_parser(name, help=help_text)
        scan.add_argument("paths", nargs="+")
        scan.add_argument("--json", action="store_true")
    commands.add_parser("rules", help="Show lint rules and exceptions.")
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
    try:
        if options.command == "lint":
            return print_findings(lint.find(options.paths), options.json)
        if options.command == "witness":
            return print_findings(witness.find(options.paths), options.json)
        if options.command == "catches":
            rows = catches.run(options.repo, options.worktree, options.cmd)
            if options.json:
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
            for name, (message, allowed) in sorted(lint.RULES.items()):
                print(f"{name}  {message}\n  allowed-when: {allowed}")
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"tq: {error}", file=sys.stderr)
        return 2
    return 0
