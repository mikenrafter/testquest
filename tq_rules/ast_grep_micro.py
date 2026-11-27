"""Source identity and evidence for the micro-snapshot advisory."""

from collections import defaultdict
from dataclasses import dataclass
import re

from .ast_grep import CALLS, SCOPES, children, offset


BRANCHES = {
    "if_statement", "if_expression", "else_clause", "elif_clause", "ternary_expression",
    "match_arm", "case_clause", "switch_case", "switch_default", "switch_expression_entry",
    "when_entry", "catch_clause", "catch_block", "except_clause", "for_statement",
    "for_expression", "enhanced_for_statement", "for_in_statement", "while_statement",
    "while_expression", "do_statement", "loop_expression",
}
BLOCKS = {"block", "statement_block", "statements", "function_body", "constructor_body"}
DEFERRED = SCOPES | {
    "arrow_function", "function_expression", "function_declaration", "lambda_expression", "lambda_literal",
    "generator_function", "generator_function_declaration", "method_definition", "method_declaration",
    "local_function_declaration",
}
ASSIGNMENTS = {
    "assignment", "assignment_expression", "augmented_assignment", "augmented_assignment_expression",
    "compound_assignment_expr", "assignment_statement",
}
DIAGNOSTIC = {"err", "error", "errors", "exception", "stderr", "rejects", "log", "logs"}
SOURCE_FILE = re.compile(r"""\.(?:md|rst|py|rs|ts|tsx|js|java|kt|dart|xml|ya?ml|toml)['"]""", re.I)
READ = re.compile(r"\b(?:read|read_text|read_to_string|readText|readString|readFileSync)\(")
QUOTED = re.compile(r"""["'][^"'\n]*["']""")


def identity(node):
    span = node.range()
    return span.start.index, span.end.index


def words(value):
    return set(re.findall(r"[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z][a-z]*", value.replace("_", " ")))


def excluded(subject, initializer, context=""):
    producer = QUOTED.sub("", initializer)
    identifiers = {word.casefold() for word in words(subject) | words(producer)}
    context_words = {word.casefold() for word in words(context)}
    return (bool(identifiers & DIAGNOSTIC)
            or '"logs"' in initializer and "stdout" in identifiers
            or "sources" in identifiers and producer.rstrip().endswith(")")
            or bool(READ.search(producer) and (context_words & {"log", "logs"} or SOURCE_FILE.search(initializer))))


def branch_path(node):
    path = []
    for parent in node.ancestors():
        if parent.kind() in BRANCHES:
            path.append(identity(node))
        node = parent
    return tuple(reversed(path))


def statement(node):
    for parent in node.ancestors():
        if parent.kind() in BLOCKS:
            return node, parent
        node = parent
    return None, None


class Reads:
    """Identify checks in the same arm, binding and uninterrupted series of reads."""

    def __init__(self, assertions):
        self.checks = tuple(assertions)
        self.assertions = None
        self.blocks = {}
        self.writes = {}

    def assertion_statements(self):
        if self.assertions is None:
            self.assertions = {identity(entry) for node in self.checks
                               if (entry := statement(node)[0]) is not None}
        return self.assertions

    def interval(self, node):
        entry, block = statement(node)
        if block is None:
            return None
        key = identity(block)
        if key not in self.blocks:
            boundary = None
            intervals = {}
            assertions = self.assertion_statements()
            for entry_in_block in children(block):
                site = identity(entry_in_block)
                intervals[site] = boundary
                if site not in assertions:
                    boundary = site
            self.blocks[key] = intervals
        return key, self.blocks[key].get(identity(entry))

    def assignment(self, node, target, binding=-1):
        if not target:
            return binding
        site = offset(node)
        for block in node.ancestors():
            if block.kind() not in BLOCKS:
                continue
            key = identity(block)
            if key not in self.writes:
                writes = []
                assertions = self.assertion_statements()
                for entry in children(block):
                    if identity(entry) in assertions:
                        continue
                    for assignment in nodes_of(entry, ASSIGNMENTS, stop_at=DEFERRED):
                        left = assignment.field("left") or next(iter(children(assignment)), None)
                        if left is not None:
                            writes.append((offset(assignment), left.text()))
                self.writes[key] = writes
            for position, left in self.writes[key]:
                if binding < position < site and (left == target or target.startswith(left + ".")):
                    binding = position
        return binding

    def key(self, node, subject, binding=None, *, reread=False):
        return branch_path(node), binding, subject, self.interval(node) if reread else None


@dataclass(frozen=True)
class Fragment:
    site: tuple
    subject: str
    key: tuple
    needle: str
    negative: bool = False
    compact: bool = False
    line_count: bool = False
    echoed: bool = False


def report(test, fragments, whole, key):
    groups = defaultdict(dict)
    for fragment in fragments:
        groups[fragment.key][fragment.site] = fragment

    candidates = []
    for observation, group in groups.items():
        checks = sorted(group.values(), key=lambda fragment: fragment.site)
        positives = sum(not check.negative for check in checks)
        line_count = checks[0].line_count and len(checks) == positives == 2
        compact = checks[0].compact
        if (len(checks) == positives >= 3 or line_count
                or compact and len(checks) >= 3 and positives >= 2
                or any(check.echoed for check in checks)):
            candidates.append((checks[0].site, observation, checks, line_count))

    if not candidates:
        return
    covered = {key(check, subject) for check, subject in whole}
    candidate = min((entry for entry in candidates if entry[1] not in covered),
                    key=lambda entry: entry[0], default=None)
    if candidate is None:
        return
    site, _, checks, line_count = candidate
    issue = site[0], "micro-snapshot"
    test.issues.append(issue)
    if test.issue_messages is None:
        test.issue_messages = {}
    subject = " ".join(part.strip() for part in checks[0].subject.splitlines())
    noun = "check" if len(checks) == 1 else "checks"
    description = f"{len(checks)} substring {noun} on {subject}"
    if line_count:
        description += " and a line count"
    verb = "pins" if len(checks) == 1 else "pin"
    message = f"{description} {verb} only parts of the output."
    if echoed := next((check.needle for check in checks if check.echoed), None):
        message += f" {echoed!r} already occurs in a setup literal."
    test.issue_messages[issue] = message


def nodes_of(node, kinds, *, stop_at=frozenset()):
    pending = [node]
    while pending:
        node = pending.pop()
        if node.kind() in kinds:
            yield node
        if node.kind() not in stop_at:
            pending.extend(children(node))


def has_call(node):
    return node is not None and next(nodes_of(node, CALLS | {"method_invocation", "new_expression", "object_creation_expression"}), None) is not None
