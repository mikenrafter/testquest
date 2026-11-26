import argparse
import json
import subprocess
import sys

from . import catches, lint, witness


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
    options = parser.parse_args()
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
        if options.command == "rules":
            for name, (message, allowed) in sorted(lint.RULES.items()):
                print(f"{name}  {message}\n  allowed-when: {allowed}")
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"tq: {error}", file=sys.stderr)
        return 2
    return 0
