import ast
import builtins
from collections import ChainMap, Counter, defaultdict
import io
import sys
import tokenize

from .lint import (UNKNOWN, assertion, fixed_boundary, helper, loose_bounds, named_arguments,
                   symmetric_input, unvaried_arguments)
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


def call_arguments(call):
    arguments = {}
    for index, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            break
        arguments[index] = arg
    arguments.update((kw.arg, kw.value) for kw in call.keywords if kw.arg is not None)
    return arguments


def argument_literals(arguments):
    return {position: {} if isinstance(arg, ast.Dict) and not arg.keys else literal(arg)
            for position, arg in arguments.items()}


def parameter_names(definition, *, bound=False):
    args = definition.args
    positional = [*args.posonlyargs, *args.args][int(bound):]
    return {**{index: arg.arg for index, arg in enumerate(positional)},
            **{arg.arg: arg.arg for arg in [*args.args, *args.kwonlyargs]}}


def file_parameters(definitions):
    qualified_parameters = {}
    method_parameters = defaultdict(dict)
    for (owner, name), definition in definitions.items():
        if not owner:
            qualified_parameters[name] = parameter_names(definition)
            continue
        decorators = {name_of(decorator) for decorator in definition.decorator_list}
        qualified = ".".join((*owner, name))
        qualified_parameters[qualified] = parameter_names(definition, bound="classmethod" in decorators)
        bound = parameter_names(definition, bound="staticmethod" not in decorators)
        method_parameters[owner][f"self.{name}"] = bound
        if name == "__init__":
            qualified_parameters[".".join(owner)] = bound
    return qualified_parameters, method_parameters


def nonempty_literal(node):
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(not isinstance(item, ast.Starred) for item in node.elts)
    if isinstance(node, ast.Dict):
        return any(key is not None for key in node.keys)
    return False


def all_comprehension(node):
    if (isinstance(node, ast.Call) and name_of(node.func) == "all"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp))):
        return node.args[0]
    return None


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
                 "assertListEqual": ast.Eq, "assertTupleEqual": ast.Eq,
                 "assertGreater": ast.Gt, "assertGreaterEqual": ast.GtE,
                 "assertLess": ast.Lt, "assertLessEqual": ast.LtE}
    if method in operators and len(args) >= 2:
        return ast.Compare(left=args[0], ops=[operators[method]()], comparators=[args[1]])
    if method in {"assertTrue", "assertFalse"} and args:
        return args[0] if method == "assertTrue" else ast.UnaryOp(op=ast.Not(), operand=args[0])
    return None


RECORDING_METHODS = {"append", "extend", "insert"}
OBSERVERS = {"len", "set", "list", "sorted", "tuple", "str", "bool", "int", "bytes",
             "float", "dict", "repr", "iter", "reversed"}


class Inspect(ast.NodeVisitor):
    def __init__(self, imports, checking_helpers, local_helpers, iterables, parameters):
        self.imports = imports
        self.checking_helpers = checking_helpers
        self.local_helpers = local_helpers
        self.iterables = iterables
        self.parameters = parameters
        self.checks = []
        self.calls = []
        self.check_arguments = []
        self.expected_arguments = []
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

    def visit_loop(self, body, orelse, guard):
        if guard is not None:
            self.guards.append(guard)
        for statement in body:
            self.visit(statement)
        if guard is not None:
            self.guards.pop()
        for statement in orelse:
            self.visit(statement)

    def visit_For(self, node):
        self.visit(node.iter)
        runs = isinstance(node, ast.For) and self.iterables.nonempty(node.iter)
        self.visit_loop((node.target, *node.body), node.orelse, None if runs else node.iter)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self.visit(node.test)
        runs = isinstance(node.test, ast.Constant) and node.test.value is True
        self.visit_loop(node.body, node.orelse, None if runs else node.test)

    def record_check(self, line, predicate):
        guards = tuple(self.guards)
        comprehension = all_comprehension(predicate)
        if comprehension is not None and any(
                generator.is_async or generator.ifs or not self.iterables.nonempty(generator.iter)
                for generator in comprehension.generators):
            guards += (comprehension,)
        self.checks.append((line, predicate, guards))

    def visit_Assert(self, node):
        self.record_check(node.lineno, node.test)
        self.check_arguments.append(node.test)
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
            self.record_check(node.lineno, predicate(node, method))
        if method == "assertEqual":
            self.expected_arguments.extend(node.args[1:2])
            self.expected_arguments.extend(keyword.value for keyword in node.keywords if keyword.arg == "second")
        if assertion(method):
            self.check_arguments.extend(node.args)
            self.check_arguments.extend(keyword.value for keyword in node.keywords)
        if method in {"assume", "filter"}:
            self.constraints.add(method)
        if method in {"event", "target"}:
            self.measured = True
        self.calls.append(node)
        comprehension = all_comprehension(node)
        if comprehension is None:
            self.generic_visit(node)
        else:
            self.visit_all_comprehension(comprehension)

    def visit_all_comprehension(self, comprehension):
        depth = len(self.guards)
        for generator in comprehension.generators:
            self.visit(generator.iter)
            if generator.is_async or not self.iterables.nonempty(generator.iter):
                self.guards.append(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
                self.guards.append(condition)
        if isinstance(comprehension, ast.DictComp):
            self.visit(comprehension.key)
            self.visit(comprehension.value)
        else:
            self.visit(comprehension.elt)
        del self.guards[depth:]

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

    def observed_calls(self, node, line, *, follow_receivers=True):
        node = self.resolve(node, line)
        if isinstance(node, ast.Call):
            yield node
            if follow_receivers:
                if isinstance(node.func, ast.Attribute):
                    yield from self.observed_calls(node.func.value, line)
                elif name_of(node.func) in OBSERVERS and len(node.args) == 1:
                    yield from self.observed_calls(node.args[0], line)
        elif isinstance(node, (ast.Attribute, ast.Subscript, ast.Await)):
            yield from self.observed_calls(node.value, line, follow_receivers=follow_receivers)
        elif isinstance(node, ast.UnaryOp):
            yield from self.observed_calls(node.operand, line, follow_receivers=follow_receivers)
        elif isinstance(node, (ast.Compare, ast.BoolOp)):
            operands = [node.left, *node.comparators] if isinstance(node, ast.Compare) else node.values
            for operand in operands:
                yield from self.observed_calls(operand, line, follow_receivers=follow_receivers)

    def zero_origin(self, actual, expected, line):
        if not isinstance(actual, ast.Call) or not isinstance(expected, (ast.List, ast.Tuple)):
            return False

        def times(sequence):
            if not isinstance(sequence, (ast.List, ast.Tuple)):
                return ()
            calls = sequence.elts
            if not all(isinstance(call, ast.Call) and call.args for call in calls):
                return ()
            return tuple(literal(call.args[0]) for call in calls)

        outputs = times(expected)
        if (len(outputs) < 2 or not all(type(value) in {int, float} and value > 0 for value in outputs)
                or not all(len(call.args) > 1 and literal(call.args[1]) == value
                           for call, value in zip(expected.elts, outputs))):
            return False
        return any(times(self.resolve(arg, line)) == (0, *outputs) for arg in actual.args)

    def literal_issues(self, test_name):
        checked, observed, direct, fixtures = set(), set(), set(), set()
        expected = {call for value in self.expected_arguments for call in self.constructor_calls(value)}
        for argument in self.check_arguments:
            line = argument.lineno
            direct.update(id(call) for call in self.observed_calls(argument, line, follow_receivers=False))
            for call in self.observed_calls(argument, line):
                observed.add(id(call))
                for value in call_arguments(call).values():
                    resolved = self.resolve(value, line)
                    if isinstance(resolved, ast.Call):
                        fixtures.add(id(resolved))
            for node in body_nodes((argument,)):
                if isinstance(node, ast.Call):
                    checked.add(id(node))
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    resolved = self.resolve(node, node.lineno)
                    if isinstance(resolved, ast.Await):
                        resolved = resolved.value
                    if isinstance(resolved, ast.Call):
                        checked.add(id(resolved))
                elif isinstance(node, ast.Compare):
                    for operator, right in zip(node.ops, node.comparators):
                        if isinstance(operator, ast.Eq):
                            expected.update(self.constructor_calls(right))

        calls = defaultdict(list)
        unvaried, coverage = [], []
        for call in self.calls:
            qualified = name_of(call.func)
            method = qualified.rsplit(".", 1)[-1]
            callee = ast.unparse(call.func)
            arguments = call_arguments(call)
            literals = argument_literals(arguments)
            calls[callee].append((call, literals, {key: ast.dump(arg) for key, arg in arguments.items()}))
            if not assertion(method) and id(call) not in expected:
                names = self.parameters.get(callee, {})
                inputs = {position: value for position, value in literals.items()
                          if not names.get(position, "").startswith("_")}
                location = (call.lineno, call.col_offset)
                unvaried.append((callee, location, inputs, id(call) in observed))
                coverage.append((callee, location, inputs, id(call) in observed, id(call) in fixtures))

        for callee, group in calls.items():
            peers = [(arguments, literals) for call, literals, arguments in group
                     if id(call) in checked and id(call) not in expected]
            nested = [(call, literals) for call, literals, _ in group
                      if id(call) in fixtures and id(call) not in expected]
            if fixed_boundary([literals for _, literals in nested]):
                yield min(call.lineno for call, _ in nested)
            for call, literals, arguments in group:
                qualified = name_of(call.func)
                root, _, name = qualified.rpartition(".")
                module = self.imports.get(root.split(".")[0], root).split(".")[0]
                if (not name or name[0].isupper() or helper(name) or qualified in self.checking_helpers
                        or module in {"self", "tests"} or module in sys.stdlib_module_names
                        or (not root and (name in self.local_helpers or hasattr(builtins, name)))):
                    continue
                if symmetric_input(name, literals, arguments, peers, observed=id(call) in direct):
                    yield call.lineno
        yield from unvaried_arguments(unvaried)
        yield from named_arguments(test_name, coverage, lambda callee: self.parameters.get(callee, {}))
        yield from loose_bounds(self.bound_checks())

    def bound_checks(self):
        for line, predicate, _ in self.checks:
            if predicate is None:
                continue
            for node in body_nodes((predicate,)):
                if not isinstance(node, ast.Compare):
                    continue
                for left, operator, right in zip(
                        (node.left, *node.comparators), node.ops, node.comparators):
                    for operand, other in ((left, right), (right, left)):
                        call = self.resolve(operand, line)
                        if not isinstance(call, ast.Call):
                            continue
                        bounded = (isinstance(operator, (ast.Gt, ast.GtE, ast.Lt, ast.LtE))
                                   and type(literal(other)) in {int, float})
                        yield (ast.unparse(call.func), (call.lineno, call.col_offset),
                               argument_literals(call_arguments(call)), bounded)

    @staticmethod
    def constructor_calls(value):
        for node in body_nodes((value,)):
            if isinstance(node, ast.Call) and name_of(node.func).rsplit(".", 1)[-1][:1].isupper():
                yield id(node)

    def issues(self, test_name):
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
            if operator is ast.Eq and self.zero_origin(self.resolve(left, line), right, line):
                findings.append((line, "nondistinguishing-literal"))

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
        findings.extend((line, "nondistinguishing-literal") for line in self.literal_issues(test_name))
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


SHRINKING_METHODS = {"clear", "discard", "difference_update", "intersection_update",
                     "pop", "popitem", "remove", "symmetric_difference_update", "__delitem__"}


def assignments(statements):
    for node in statements:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            yield node, node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node, node.target, node.value


def changed_names(statements):
    for node in body_nodes(statements):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield name_of(node)
        elif (isinstance(node, ast.Subscript)
              and (isinstance(node.ctx, ast.Del)
                   or isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Slice))):
            yield name_of(node.value)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            yield from (alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in SHRINKING_METHODS):
            yield name_of(node.func.value)


def literal_return(node):
    body = node.body
    if node.decorator_list:
        return False
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return len(body) == 1 and isinstance(body[0], ast.Return) and nonempty_literal(body[0].value)


def scope_changes(statements):
    changed = Counter(changed_names(statements))
    changed.update(node.name for node in statements
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return changed


def file_iterables(statements, changed, scope=()):
    for _, target, value in assignments(statements):
        if (isinstance(target, ast.Name) and target.id.isupper()
                and changed[target.id] == 1 and nonempty_literal(value)):
            yield (*scope, target.id), value
    for node in statements:
        if isinstance(node, ast.FunctionDef) and changed[node.name] == 1 and literal_return(node):
            yield (*scope, node.name), node
        elif isinstance(node, ast.ClassDef) and changed[node.name] == 1:
            yield from file_iterables(node.body, scope_changes(node.body), (*scope, node.name))


class Iterables:
    def __init__(self, test, scope, tables, module_names):
        self.scope = scope
        self.tables = tables
        self.module_names = module_names
        positional = test.args.posonlyargs + test.args.args
        self.receiver = positional[0].arg if scope and positional else None
        self.parents = {
            child: (node, field)
            for node in body_nodes(test.body)
            for field, value in ast.iter_fields(node)
            for child in (value if isinstance(value, list) else (value,))
            if isinstance(child, ast.AST)
        }
        self.parents.update((node, (test, "body")) for node in test.body)
        self.changed = Counter(changed_names(test.body))
        self.changed.update(node.arg for node in ast.walk(test.args) if isinstance(node, ast.arg))
        self.changed.update(node.name for node in self.parents
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        self.locals = {
            target.id: node for node, target, value in assignments(self.parents)
            if (isinstance(target, ast.Name) and self.changed[target.id] == 1
                and nonempty_literal(value))
        }
        self.locals.update(
            (node.name, node) for node in self.parents
            if isinstance(node, ast.FunctionDef) and self.changed[node.name] == 1 and literal_return(node)
        )

    def definition(self, node):
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return None
        name = name_of(node)
        if name in self.changed:
            binding = self.locals.get(name)
            if binding is not None:
                block = self.parents[binding]
                while node in self.parents:
                    if self.parents[node] == block:
                        if (binding.lineno, binding.col_offset) < (node.lineno, node.col_offset):
                            return binding if isinstance(binding, ast.FunctionDef) else binding.value
                        break
                    node = self.parents[node][0]
            return None

        path = tuple(name.split("."))
        if root.id == self.receiver and self.changed[root.id] == 1:
            return self.tables.get((*self.scope, *path[1:]))
        if root.id in self.changed:
            return None
        return self.tables.get(path)

    def nonempty(self, node):
        if nonempty_literal(node):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            return nonempty_literal(self.definition(node))
        if not isinstance(node, ast.Call):
            return False
        if isinstance(self.definition(node.func), ast.FunctionDef):
            return True
        name = name_of(node.func)
        if (isinstance(node.func, ast.Attribute) and node.func.attr in {"items", "keys", "values"}
                and not node.args and not node.keywords):
            receiver = node.func.value
            table = receiver if isinstance(receiver, ast.Dict) else self.definition(receiver)
            return isinstance(table, ast.Dict) and nonempty_literal(table)
        if (not isinstance(node.func, ast.Name) or name in self.changed
                or name in self.module_names):
            return False
        if name == "range" and 1 <= len(node.args) <= 3 and not node.keywords:
            bounds = [literal(arg) for arg in node.args]
            return (all(type(bound) is int for bound in bounds)
                    and (len(bounds) < 3 or bounds[2] != 0) and bool(range(*bounds)))
        keywords = {keyword.arg for keyword in node.keywords}
        if name == "zip" and keywords <= {"strict"}:
            return bool(node.args) and all(self.nonempty(arg) for arg in node.args)
        if name == "enumerate" and 1 <= len(node.args) <= 2 and keywords <= {"start"}:
            return self.nonempty(node.args[0])
        return (name == "sorted" and len(node.args) == 1 and keywords <= {"key", "reverse"}
                and self.nonempty(node.args[0]))


def checking_helpers(tree):
    definitions = {}
    parents = {}

    def index(nodes, scope):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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

    nodes = {key: list(body_nodes(fn.body)) for key, fn in definitions.items()
             if not fn.name.startswith("test")}
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
            return available, definitions
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

    checks_in, definitions = checking_helpers(tree)
    parameters, methods = file_parameters(definitions)
    local_helpers = {node.name for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and not node.name.startswith("test")}
    module_names = scope_changes(tree.body)
    tables = dict(file_iterables(tree.body, module_names))
    tests = []
    for node, parents in test_functions(tree.body):
        iterables = Iterables(node, parents, tables, module_names)
        local_parameters = {local.name: parameter_names(local) for local in node.body
                          if isinstance(local, (ast.FunctionDef, ast.AsyncFunctionDef))}
        test_parameters = ChainMap(local_parameters, methods.get(parents, {}), parameters)
        inspector = Inspect(imports, checks_in(parents), local_helpers, iterables, test_parameters)
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
        test.issues = inspector.issues(node.name)
        if not inspector.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(inspector.checks) and all(guards for _, _, guards in inspector.checks)
        test.conditional = bool(inspector.constraints)
        test.measured = inspector.measured
        tests.append(test)
    return tests, comments
