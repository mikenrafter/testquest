import argparse
import json
import subprocess
import sys

from . import lint


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
    scan = commands.add_parser("lint", help="Find weak assertions and fixtures.")
    scan.add_argument("paths", nargs="+")
    scan.add_argument("--json", action="store_true")
    commands.add_parser("rules", help="Show lint rules and exceptions.")
    options = parser.parse_args()
    try:
        if options.command == "lint":
            return print_findings(lint.find(options.paths), options.json)
        if options.command == "rules":
            for name, (message, allowed) in sorted(lint.RULES.items()):
                print(f"{name}  {message}\n  allowed-when: {allowed}")
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"tq: {error}", file=sys.stderr)
        return 2
    return 0
