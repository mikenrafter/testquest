import ast
import builtins
from collections import defaultdict
import io
import sys
import tokenize

from .lint import UNKNOWN, helper, symmetric_input
from .source import Comment, Test


def name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ".".join(filter(None, (name_of(node.value), node.attr)))
    return ""


def literal(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        values = tuple(literal(item) for item in node.elts)
        if UNKNOWN not in values:
            return list(values) if isinstance(node, ast.List) else values
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = literal(node.operand)
        if type(value) in {int, float, complex}:
            return value if isinstance(node.op, ast.UAdd) else -value
    return UNKNOWN


def same(left, right):
    return ast.dump(left) == ast.dump(right)


def comparison(node):
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        return type(node.ops[0]), node.left, node.comparators[0]
    return None, None, None


def constant_truth(node):
    value = literal(node)
    if value is not UNKNOWN:
        return bool(value)
    if isinstance(node, ast.BoolOp):
        evaluate = all if isinstance(node.op, ast.And) else any
        return evaluate(map(constant_truth, node.values))
    operator, left, right = comparison(node)
    if operator in {ast.Eq, ast.NotEq}:
        first, second = literal(left), literal(right)
        return UNKNOWN not in (first, second) and (first == second) == (operator is ast.Eq)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = literal(node.operand)
        return value is not UNKNOWN and not value
    return False


def predicate(call, method):
    args = call.args
    operators = {"assertEqual": ast.Eq, "assertNotEqual": ast.NotEq,
                 "assertIs": ast.Is, "assertIsNot": ast.IsNot,
                 "assertListEqual": ast.Eq, "assertTupleEqual": ast.Eq}
    if method in operators and len(args) >= 2:
        return ast.Compare(left=args[0], ops=[operators[method]()], comparators=[args[1]])
    if method in {"assertTrue", "assertFalse"} and args:
        return args[0] if method == "assertTrue" else ast.UnaryOp(op=ast.Not(), operand=args[0])
    return None


RECORDING_METHODS = {"append", "extend", "insert"}
OBSERVERS = {"len", "set", "list", "sorted", "tuple", "str", "bool", "int", "bytes",
             "float", "dict", "repr", "iter", "reversed"}


class Inspect(ast.NodeVisitor):
    def __init__(self, imports, checking_helpers, local_helpers):
        self.imports = imports
        self.checking_helpers = checking_helpers
        self.local_helpers = local_helpers
        self.checks = []
        self.calls = []
        self.bindings = defaultdict(list)
        self.writes = {}
        self.reads = set()
        self.guards = []
        self.constraints = set()
        self.measured = False

    def visit_FunctionDef(self, node):
        # Closures can record effects, but their assertions need an actual call.
        self.record_uses(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def record_uses(self, node):
        recording_receivers = set()
        for child in ast.walk(node):
            if (isinstance(child, ast.Attribute) and child.attr in RECORDING_METHODS
                    and isinstance(child.value, ast.Name)):
                recording_receivers.add(id(child.value))
                self.writes.setdefault(child.value.id, child.lineno)
        self.reads.update(child.id for child in ast.walk(node)
                          if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                          and id(child) not in recording_receivers)

    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.bindings[node.targets[0].id].append((node.lineno, node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name):
            self.bindings[node.target.id].append((node.lineno, node.value))
        self.generic_visit(node)

    def visit_If(self, node):
        self.visit(node.test)
        self.guards.append(node.test)
        start = len(self.checks)
        covered = []
        for branch in (node.body, node.orelse):
            before = len(self.checks)
            for statement in branch:
                self.visit(statement)
            covered.append(any(len(guards) == len(self.guards)
                               for _, _, guards in self.checks[before:]))
        if all(covered):
            depth = len(self.guards) - 1
            for index in range(start, len(self.checks)):
                line, predicate, guards = self.checks[index]
                self.checks[index] = (line, predicate, guards[:depth] + guards[depth + 1:])
        self.guards.pop()

    def visit_Match(self, node):
        self.visit(node.subject)
        self.guards.append(node.subject)
        for arm in node.cases:
            self.visit(arm)
        self.guards.pop()

    def visit_Assert(self, node):
        self.checks.append((node.lineno, node.test, tuple(self.guards)))
        self.visit(node.test)

    def visit_Raise(self, node):
        value = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if name_of(value) == "AssertionError":
            self.checks.append((node.lineno, None, tuple(self.guards)))
        self.generic_visit(node)

    def visit_Call(self, node):
        name = name_of(node.func)
        method = name.rsplit(".", 1)[-1]
        if helper(method) or name in self.checking_helpers:
            self.checks.append((node.lineno, predicate(node, method), tuple(self.guards)))
        if method in {"assume", "filter"}:
            self.constraints.add(method)
        if method in {"event", "target"}:
            self.measured = True
        self.calls.append(node)
        self.generic_visit(node)

    def resolve(self, node, line):
        while isinstance(node, ast.Name):
            prior = [binding for binding in self.bindings[node.id] if binding[0] < line]
            if len(prior) != 1:
                break
            line, node = prior[0]
        return node

    def origin(self, value):
        if not isinstance(value, ast.Call):
            return "", ""
        name = name_of(value.func)
        root, dot, suffix = name.partition(".")
        if root in self.imports:
            name = self.imports[root] + dot + suffix
            module = name.rpartition(".")[0]
            if not name.rsplit(".", 1)[-1][:1].isupper() and name.split(".")[0] not in sys.stdlib_module_names:
                return name, module
        elif not dot and name not in OBSERVERS:
            return name, ""
        return "", ""

    def coupled(self, left, right, line):
        actual, expected = self.resolve(left, line), self.resolve(right, line)
        first, first_module = self.origin(actual)
        second, second_module = self.origin(expected)
        if not ((first and first == second) or (first_module and first_module == second_module)):
            return False
        # A second application to the first result tests idempotence.
        if any(same(arg, right) for arg in actual.args) or any(same(arg, left) for arg in expected.args):
            return False
        # An explicit before/after snapshot observes an intervening action.
        if (same(actual, expected) and not same(left, right)
                and any(word in first.rsplit(".", 1)[-1]
                        for word in ("read", "snapshot", "digest", "identity", "getstate"))):
            return False
        return True

    def recorder(self, node, recorders):
        if name_of(node) in recorders:
            return name_of(node)
        if (isinstance(node, ast.Call) and name_of(node.func) == "len" and len(node.args) == 1
                and name_of(node.args[0]) in recorders):
            return name_of(node.args[0])
        return None

    def issues(self):
        findings = []
        recorders = {name for name, bindings in self.bindings.items()
                     if isinstance(bindings[0][1], ast.List) and not bindings[0][1].elts}
        negative = defaultdict(list)
        positive = set()
        for line, node, _ in self.checks:
            if node is None:
                continue
            operator, left, right = comparison(node)
            if constant_truth(node):
                findings.append((line, "const-truth"))
            if operator in {ast.Eq, ast.Is, ast.NotEq, ast.IsNot} and same(left, right):
                findings.append((line, "self-compare"))
            if operator is ast.Eq and self.coupled(left, right, line):
                findings.append((line, "self-derived-expected"))

            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                recorder = self.recorder(node.operand, recorders)
                if recorder:
                    negative[recorder].append(line)
            if isinstance(node, ast.Name) and node.id in recorders:
                positive.add(node.id)
            for operand, other, ordered in ((left, right, operator), (right, left, {
                    ast.Lt: ast.Gt, ast.LtE: ast.GtE, ast.Gt: ast.Lt, ast.GtE: ast.LtE,
                }.get(operator, operator))):
                recorder = self.recorder(operand, recorders)
                if not recorder:
                    continue
                value = literal(other)
                if ordered is ast.Eq and value in ([], (), 0, False):
                    negative[recorder].append(line)
                elif ((ordered is ast.Eq and (
                           (value is not UNKNOWN and bool(value))
                           or (isinstance(other, (ast.List, ast.Tuple)) and other.elts)))
                      or (ordered is ast.NotEq and value in ([], (), 0, False))
                      or (ordered is ast.Gt and type(value) is int and value >= 0)
                      or (ordered is ast.GtE and type(value) is int and value > 0)):
                    positive.add(recorder)

        for name in recorders - positive:
            if name in negative:
                findings.extend((line, "absence-no-control") for line in negative[name])
            elif name in self.writes and name not in self.reads:
                findings.append((self.writes[name], "absence-no-control"))
        for call in self.calls:
            qualified = name_of(call.func)
            root, _, name = qualified.rpartition(".")
            module = self.imports.get(root.split(".")[0], root).split(".")[0]
            if (not name or name[0].isupper() or helper(name) or qualified in self.checking_helpers
                    or module in {"self", "tests"} or module in sys.stdlib_module_names
                    or (not root and (name in self.local_helpers or hasattr(builtins, name)))):
                continue
            args = [*call.args, *(kw.value for kw in call.keywords)]
            if symmetric_input(name, map(literal, args),
                               [False] * len(call.args) + [True] * len(call.keywords)):
                findings.append((call.lineno, "nondistinguishing-literal"))
        return findings


def test_functions(nodes, parents=()):
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node, parents
        elif isinstance(node, ast.ClassDef):
            yield from test_functions(node.body, (*parents, node.name))


def body_nodes(statements):
    for node in statements:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        yield from body_nodes(ast.iter_child_nodes(node))


def checking_helpers(tree):
    definitions = {}
    parents = {}

    def index(nodes, scope):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("test"):
                    definitions[scope, node.name] = node
            elif isinstance(node, ast.ClassDef):
                child = (*scope, node.name)
                parents[child] = [(*scope, name_of(base)) for base in node.bases]
                index(node.body, child)

    index(tree.body, ())
    checked = set()

    def available(scope):
        scopes = {scope}
        pending = [scope]
        while pending:
            for parent in parents.get(pending.pop(), ()):
                if parent not in scopes:
                    scopes.add(parent)
                    pending.append(parent)
        return {name for owner, name in checked if not owner} | {
            f"self.{name}" for owner, name in checked if owner and owner in scopes
        }

    nodes = {key: list(body_nodes(fn.body)) for key, fn in definitions.items()}
    while True:
        found = set()
        for key, inside in nodes.items():
            if key in checked:
                continue
            known = available(key[0])
            if any(isinstance(node, ast.Assert) or
                   (isinstance(node, ast.Call) and
                    (name_of(node.func) in known or helper(name_of(node.func).rsplit(".", 1)[-1])))
                   for node in inside):
                found.add(key)
        if not found:
            return available
        checked.update(found)


def parse(source, path):
    tree = ast.parse(source, filename=str(path))
    comments = [
        Comment(start[0], start[1], text[1:].strip(), not raw[:start[1]].strip())
        for kind, text, start, _, raw in tokenize.generate_tokens(io.StringIO(source).readline)
        if kind == tokenize.COMMENT
    ]
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                imports[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            for alias in node.names:
                imports[alias.asname or alias.name] = module + "." + alias.name

    checks_in = checking_helpers(tree)
    local_helpers = {node.name for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and not node.name.startswith("test")}
    tests = []
    for node, parents in test_functions(tree.body):
        inspector = Inspect(imports, checks_in(parents), local_helpers)
        for statement in node.body:
            inspector.record_uses(statement)
            inspector.visit(statement)
        for decorator in node.decorator_list:
            for call in (part for part in ast.walk(decorator) if isinstance(part, ast.Call)):
                if name_of(call.func).rsplit(".", 1)[-1] == "filter":
                    inspector.constraints.add("filter")

        start = min([node.lineno, *(dec.lineno for dec in node.decorator_list)])
        previous = {c.line for c in comments if c.standalone and c.column == node.col_offset}
        while start - 1 in previous:
            start -= 1
        end = node.end_lineno
        trailing = {c.line for c in comments if c.standalone and c.column > node.col_offset}
        while end + 1 in trailing:
            end += 1
        identifier = ".".join((*path.with_suffix("").parts, *parents, node.name))
        test = Test(node.name, identifier, node.lineno, start, end, node.col_offset)
        test.issues = inspector.issues()
        if not inspector.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(inspector.checks) and all(guards for _, _, guards in inspector.checks)
        test.conditional = bool(inspector.constraints)
        test.measured = inspector.measured
        tests.append(test)
    return tests, comments
