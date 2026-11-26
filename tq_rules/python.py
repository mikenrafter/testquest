import ast
import builtins
from bisect import bisect_left
from collections import ChainMap, Counter, defaultdict
import io
from pathlib import Path
import sys
import tokenize

from .lint import (UNKNOWN, assertion, different_callees, fixed_boundary, fixed_operand, argument_claims, helper, loose_bounds, named_arguments,
                   prefix_satisfies, DISTINCT_INPUTS, claims_effect, OUTCOME_CLAIM, REPEATABILITY, symmetric_input, unvaried_arguments, IMPORT_FIELDS, IMPORT_TOKEN,
                   inverse_codecs, path_directories, pins_path)
from .source import Comment, Test, project_root


def name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ".".join(filter(None, (name_of(node.value), node.attr)))
    return ""


def binding_path(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (receiver := binding_path(node.value)):
        return receiver + "." + node.attr
    return ""


def raises_assertion(node):
    value = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return name_of(value) == "AssertionError"


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


ORACLE_METHODS = {"to_json", "tobytes", "to_string", "clone", "read_bytes", "read_text",
                  "read_to_string"}
ORACLE_FUNCTIONS = {"json.dumps", "json.loads"}
STRING_TRANSFORMS = {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}


class Inspect(ast.NodeVisitor):
    def __init__(self, imports, checking_helpers, local_helpers, iterables, parameters,
                 oracle_imports, module_constants, definitions, owner):
        self.imports = imports
        self.checking_helpers = checking_helpers
        self.local_helpers = local_helpers
        self.iterables = iterables
        self.parameters = parameters
        self.oracle_imports = oracle_imports
        self.module_constants = module_constants
        self.definitions = definitions
        self.owner = owner
        self.checks = []
        self.calls = []
        self.check_arguments = []
        self.expected_arguments = []
        self.bindings = defaultdict(list)
        self.writes = {}
        self.reads = set()
        self.recorder_sites = defaultdict(set)
        self.callback_writes = {}
        self.guards = []
        self.output_loops = {}
        self.constraints = set()
        self.measured = False

    def visit_FunctionDef(self, node):
        # Closures can record effects, but their assertions need an actual call.
        self.record_uses(node)
        parts = tuple(body_nodes(node.body))
        captured = {name for part in parts if isinstance(part, ast.Nonlocal) for name in part.names}
        for part in parts:
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store) and part.id in captured:
                self.callback_writes.setdefault(part.id, part.lineno)

    visit_AsyncFunctionDef = visit_FunctionDef

    def record_uses(self, node):
        recording_receivers = set()
        for child in ast.walk(node):
            if (isinstance(child, ast.Attribute) and child.attr in RECORDING_METHODS
                    and isinstance(child.value, ast.Name)):
                recording_receivers.add(id(child.value))
                self.writes.setdefault(child.value.id, child.lineno)
                self.recorder_sites[child.value.id].add(child)
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
        if not runs and isinstance(node.iter, ast.Attribute) and isinstance(node.target, ast.Name):
            self.output_loops[node.iter] = node.target.id
        if isinstance(self.iterables, GuardIterables) and runs:
            previous = self.iterables.loop_rows.copy()
            self.iterables.enter_loop(node)
            self.visit_loop((node.target, *node.body), (), None)
            self.iterables.loop_rows = previous
            for statement in node.orelse:
                self.visit(statement)
        else:
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
        if raises_assertion(node):
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
            prior = [binding for binding in self.bindings.get(node.id, ()) if binding[0] < line]
            if len(prior) != 1:
                break
            line, node = prior[0]
        return node

    def imported_name(self, value):
        root, dot, suffix = name_of(value).partition(".")
        imported = self.oracle_imports.get(root)
        return imported + dot + suffix if imported else ""

    def oracle_calls(self, value, line):
        value = self.resolve(value, line)
        if isinstance(value, ast.Call):
            name = name_of(value.func)
            imported = self.imported_name(value.func)
            builtin = (isinstance(value.func, ast.Name) and name in OBSERVERS
                       and name not in self.iterables.changed and name not in self.local_helpers
                       and name not in self.oracle_imports)
            if builtin or imported in ORACLE_FUNCTIONS or imported.endswith(".to_json"):
                return self.oracle_calls(value.args[0], value.lineno) if value.args else None
            if isinstance(value.func, ast.Attribute) and value.func.attr in ORACLE_METHODS:
                return self.oracle_calls(value.func.value, value.lineno)
            path = tuple(imported.split("."))
            if (len(path) >= 2 and path[0] not in sys.stdlib_module_names
                    and not path[-1][:1].isupper()
                    and not any(part in {"test", "tests", "conftest"} or part.startswith("test_")
                                for part in path[:-1])):
                return {path}
        elif isinstance(value, ast.Constant):
            return set()
        elif isinstance(value, ast.BinOp):
            return self.composed_calls((value.left, value.right), value.lineno)
        elif isinstance(value, ast.JoinedStr):
            return self.composed_calls(value.values, value.lineno)
        elif isinstance(value, ast.FormattedValue):
            return self.oracle_calls(value.value, value.lineno)
        elif isinstance(value, (ast.Subscript, ast.Attribute)):
            if self.imported_name(value).rsplit(".", 1)[-1].isupper():
                return set()
            return self.oracle_calls(value.value, value.lineno)
        return None

    def composed_calls(self, values, line):
        paths = set()
        for value in values:
            calls = self.oracle_calls(value, line)
            if calls is None:
                return None
            paths.update(calls)
        return paths

    def oracle_constant(self, value, line):
        value = self.resolve(value, line)
        while isinstance(value, ast.Subscript):
            value = self.resolve(value.value, value.lineno)
        return self.imported_name(value)

    def uses_operand(self, value, other, line):
        while isinstance(other, (ast.Attribute, ast.Subscript)):
            other = other.value
        value = self.resolve(value, line)
        while isinstance(value, (ast.Attribute, ast.Subscript)):
            value = self.resolve(value.value, value.lineno)
        return isinstance(other, ast.Name) and any(
            isinstance(node, ast.Name) and node.id == other.id
            for call in body_nodes((value,))
            if isinstance(call, ast.Call)
            for part in call_arguments(call).values()
            for node in body_nodes((part,))
        )

    def literal_pins(self):
        for line, node, guards in self.checks:
            operator, left, right = comparison(node)
            if operator is ast.Eq and not guards:
                for operand, other in ((left, right), (right, left)):
                    if literal(self.resolve(other, line)) is UNKNOWN:
                        continue
                    resolved = self.resolve(operand, line)
                    if (not isinstance(resolved, ast.Subscript)
                            and (constant := self.oracle_constant(resolved, line)).rsplit(".", 1)[-1].isupper()):
                        yield tuple(constant.split("."))
                    if isinstance(resolved, ast.Call):
                        path = tuple(self.imported_name(resolved.func).split("."))
                        if self.oracle_calls(resolved, line) == {path}:
                            yield path

        unguarded = {line for line, _, guards in self.checks if not guards}
        for call in self.calls:
            if (call.lineno in unguarded and name_of(call.func).rsplit(".", 1)[-1] in {"assertIn", "assertNotIn"}
                    and len(call.args) >= 2 and isinstance(literal(call.args[0]), str)):
                yield from self.oracle_calls(call.args[1], call.lineno) or ()

    def sized_from(self, node, constant, line):
        node = self.resolve(node, line)
        if (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult))
                and any(self.oracle_constant(operand, node.lineno) == constant
                        and type(literal(other)) is int
                        for operand, other in ((node.left, node.right), (node.right, node.left)))):
            return True
        return node is not None and any(self.sized_from(child, constant, getattr(node, "lineno", line))
                                        for child in ast.iter_child_nodes(node))

    def shared_constant(self, calls, actual, expected, line, pins):
        constant = self.oracle_constant(expected, line)
        if (not calls or len(calls) != 1 or tuple(constant.split(".")) in pins
                or not self.module_constants.shared(next(iter(calls)), constant)):
            return False
        actual = self.resolve(actual, line)
        while isinstance(actual, (ast.Attribute, ast.Subscript)):
            actual = self.resolve(actual.value, actual.lineno)
        return not isinstance(actual, ast.Call) or not self.sized_from(actual, constant, actual.lineno)

    def composition(self, actual, expected, pins, strings):
        if not different_callees(actual, expected):
            return False
        paths = actual | expected
        directories = [parts for path in paths if (parts := self.module_constants.directories(path))]
        if directories:
            return any(not pins_path(parts, strings) for parts in directories)
        oracle = expected if len(actual) == 1 else actual
        return bool(oracle - pins)

    def observed_result(self, operand, line):
        operand = self.resolve(operand, line)
        projection = ()
        if (isinstance(operand, ast.Call) and name_of(operand.func) == "len"
                and "len" not in self.iterables.changed and "len" not in self.oracle_imports
                and "len" not in self.local_helpers and len(operand.args) == 1 and not operand.keywords):
            projection = ("len",)
            operand = self.resolve(operand.args[0], operand.lineno)
        while isinstance(operand, ast.Attribute):
            projection = (operand.attr, *projection)
            operand = self.resolve(operand.value, operand.lineno)
        return projection, operand

    def fixture(self, operand, line):
        while isinstance(operand, ast.Name):
            if any(isinstance(other.func, ast.Attribute) and name_of(other.func.value) == operand.id
                   and other.lineno < line for other in self.calls):
                return None
            prior = [binding for binding in self.bindings.get(operand.id, ()) if binding[0] < line]
            if len(prior) != 1:
                break
            _, operand = prior[0]
        return operand

    def empty_parity(self, left, right, line):
        projection, first = self.observed_result(left, line)
        other_projection, second = self.observed_result(right, line)
        if (projection != other_projection or not all(isinstance(call, ast.Call) for call in (first, second))
                or not all(len(call.args) == 1 and not call.keywords for call in (first, second))):
            return False
        calls = self.oracle_calls(first, line), self.oracle_calls(second, line)
        if not all(calls) or calls[0] == calls[1]:
            return False

        value, other = (literal(self.fixture(call.args[0], call.lineno)) for call in (first, second))
        if type(value) is not type(other) or value != other:
            return False
        if value in ([], (), "", b""):
            return True
        if not IMPORT_FIELDS.intersection(projection) or not isinstance(value, str) or IMPORT_TOKEN.search(value):
            return False
        try:
            ast.parse(value)
        except (SyntaxError, ValueError):
            return False
        return True

    def result_key(self, observation):
        projection, call = observation
        if not isinstance(call, ast.Call) or call.keywords:
            return None
        path = tuple(self.imported_name(call.func).split("."))
        if self.oracle_calls(call, call.lineno) != {path}:
            return None
        arguments = tuple(ast.dump(self.resolve(arg, call.lineno)) for arg in call.args)
        return path, projection, arguments

    def result_pins(self):
        for line, check, guarded in self.checks:
            operator, left, right = comparison(check)
            if operator is ast.Eq and not guarded:
                for operand, other in ((left, right), (right, left)):
                    if literal(self.resolve(other, line)) is not UNKNOWN:
                        if result := self.result_key(self.observed_result(operand, line)):
                            yield result

    def wrapper_parity(self, left, right, line, strings, result_pins):
        first, second = (self.result_key(self.observed_result(operand, line)) for operand in (left, right))
        if not first or not second or first[0] == second[0]:
            return None
        for wrapper, base in ((first, second), (second, first)):
            if self.module_constants.delegate(wrapper[0]) != base[0]:
                continue
            if wrapper[1:] != base[1:] or first in result_pins or second in result_pins:
                return False
            if directories := self.module_constants.directories(base[0]):
                return not pins_path(directories, strings)
            return True
        return None

    def literal_identity(self, left, right, line):
        first, second = (self.resolve(value, line) for value in (left, right))
        if not all(isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords
                   for call in (first, second)):
            return False
        calls = self.oracle_calls(first, line)
        if not calls or len(calls) != 1 or calls != self.oracle_calls(second, line):
            return False
        value = literal(self.resolve(first.args[0], first.lineno))
        transformed = self.resolve(second.args[0], second.lineno)
        if not isinstance(value, str) or not isinstance(transformed, ast.Call) or transformed.keywords:
            return False
        if (isinstance(transformed.func, ast.Attribute) and not transformed.args
                and literal(self.resolve(transformed.func.value, transformed.lineno)) == value):
            method = transformed.func.attr
        elif (len(transformed.args) == 1 and self.oracle_calls(transformed, line) == calls
              and literal(self.resolve(transformed.args[0], transformed.lineno)) == value):
            method = self.module_constants.string_transform(next(iter(calls)))
        else:
            return False
        return method in STRING_TRANSFORMS and getattr(value, method)() == value

    def codec_parity(self, outer, expected, line, result_pins):
        result = self.result_key(outer)
        projection, call = outer
        if not result or projection or len(call.args) != 1:
            return False
        inner = self.resolve(call.args[0], call.lineno)
        source = self.result_key(((), inner))
        if (not source or source in result_pins or not inverse_codecs(result[0], source[0])
                or not all(self.module_constants.definition(path) for path in (result[0], source[0]))):
            return False
        return len(source[2]) == 1 and source[2][0] == ast.dump(self.resolve(expected, line))

    def coupled(self, left, right, line, pins, strings, result_pins):
        if (self.empty_parity(left, right, line)
                or self.codec_parity(self.observed_result(left, line), right, line, result_pins)
                or self.codec_parity(self.observed_result(right, line), left, line, result_pins)):
            return True
        delegated = self.wrapper_parity(left, right, line, strings, result_pins)
        if delegated is not None:
            return delegated
        actual = self.oracle_calls(left, line)
        expected = self.oracle_calls(right, line)
        return (self.composition(actual, expected, pins, strings)
                and not (self.uses_operand(left, right, line) or self.uses_operand(right, left, line))
                or self.shared_constant(actual, left, right, line, pins)
                or self.shared_constant(expected, right, left, line, pins)
                or self.literal_identity(left, right, line)
                or self.literal_identity(right, left, line))

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
        claims = argument_claims(test_name)
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
                if any(claims):
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
        yield from unvaried_arguments(
            unvaried, lambda callee, _, fixed: self.fixed_operands(calls[callee][0][0].func, fixed))
        yield from named_arguments(coverage, claims, lambda callee: self.parameters.get(callee, {}))
        yield from loose_bounds(self.bound_checks())
        yield from self.unchecked_item_order()

    def fixed_operands(self, expression, fixed):
        callee = binding_path(expression)
        if not callee:
            return fixed.values()
        definition = self.definition_for(expression)
        imported = self.imported_name(expression)
        if definition is None and imported:
            definition = self.module_constants.definition(tuple(imported.split(".")))
        if definition is None:
            return fixed.values()
        names = parameter_names(definition, bound=callee.startswith("self."))
        operations = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "/"}
        operands = defaultdict(set)
        for node in body_nodes(definition.body):
            if isinstance(node, ast.BinOp) and type(node.op) in operations:
                for operand in (node.left, node.right):
                    while isinstance(operand, (ast.Attribute, ast.Subscript)):
                        operand = operand.value
                    if isinstance(operand, ast.Name):
                        operands[operand.id].add(operations[type(node.op)])
        return [value for position, value in fixed.items()
                if fixed_operand(value, operands[names.get(position)])]

    def definition_for(self, callee):
        root, *parts = name_of(callee).split(".")
        if root == "self" and len(parts) == 1:
            return self.definitions.get((self.owner, parts[0]))
        if root and root not in self.iterables.changed and root not in self.imports:
            return self.definitions.get((tuple([root, *parts[:-1]]) if parts else (),
                                         parts[-1] if parts else root))
        return None

    def reads_file_text(self, source, line):
        source = self.resolve(source, line)
        if (isinstance(source, ast.Attribute) and name_of(source.value) == "self"
                and name_of(source) not in self.iterables.changed):
            sources = []
            for setup in ("setUp", "setUpClass"):
                definition = self.definitions.get((self.owner, setup))
                if definition:
                    receiver = definition.args.args[0].arg if definition.args.args else ""
                    sources.extend(value for _, target, value in assignments(definition.body)
                                       if name_of(target) == f"{receiver}.{source.attr}")
            source = sources[0] if len(sources) == 1 else source
        if not isinstance(source, ast.Call):
            return False
        name = name_of(source.func).split(".")[-1]
        return name == "read_text" or (
            name == "read" and isinstance(source.func, ast.Attribute)
            and isinstance(source.func.value, ast.Call) and name_of(source.func.value.func) == "open")

    def counts_file_text(self, call, line):
        if isinstance(call.func, ast.Attribute) and call.func.attr == "count":
            return self.reads_file_text(call.func.value, line)
        if name_of(call.func) == "len" and len(call.args) == 1:
            matches = self.resolve(call.args[0], line)
            if isinstance(matches, ast.Call) and self.imported_name(matches.func) == "re.findall":
                text = call_arguments(matches).get("string", call_arguments(matches).get(1))
                return self.reads_file_text(text, line)
        return False

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
                        threshold = literal(other)
                        bounded = (isinstance(operator, (ast.Gt, ast.GtE, ast.Lt, ast.LtE))
                                   and type(threshold) in {int, float})
                        exempt = False
                        if bounded:
                            definition = self.definition_for(call.func)
                            exempt = self.counts_file_text(call, line) or (
                                definition is not None and any(
                                    isinstance(part, ast.Return)
                                    and type(value := literal(part.value)) in {int, float}
                                    and value == threshold for part in body_nodes(definition.body)))
                        yield (ast.unparse(call.func), (call.lineno, call.col_offset),
                               argument_literals(call_arguments(call)), bounded, exempt)

    def fixed_output_check(self, node, line):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            return False
        alternatives = [comparison(part) for part in node.values]
        if not all(op is ast.In and isinstance(literal(left), str) and literal(left)
                   for op, left, _ in alternatives):
            return False
        receivers = [self.resolve(right, line) for _, _, right in alternatives]
        call = receivers[0]
        if not isinstance(call, ast.Call) or not all(same(call, other) for other in receivers[1:]):
            return False
        definition = self.definition_for(call.func)
        if definition is None or not isinstance(definition.body[-1], ast.Return):
            return False
        result = definition.body[-1].value
        if not isinstance(result, ast.Name):
            return False
        initializers = [literal(value) for _, target, value in assignments(definition.body)
                        if name_of(target) == result.id]
        if len(initializers) != 1 or not isinstance(initializers[0], str):
            return False
        # CLAIM(tq-python-prefix): only an initial assignment followed by appends is a fixed prefix
        # [test_python_fixed_output].
        prefix = initializers[0]
        body = []
        stores, appends = 0, 0
        for part in body_nodes(definition.body):
            if isinstance(part, ast.Return) and part is not definition.body[-1]:
                return False
            if isinstance(part, ast.Name) and part.id == result.id and isinstance(part.ctx, ast.Store):
                stores += 1
            if isinstance(part, ast.AugAssign) and name_of(part.target) == result.id:
                if not isinstance(part.op, ast.Add):
                    return False
                appends += 1
                values = part.value.values if isinstance(part.value, ast.JoinedStr) else [part.value]
                body.extend(value for item in values if isinstance(value := literal(item), str))
        return (stores == 1 + appends
                # ↳ CLAIM(tq-python-prefix)
                and prefix_satisfies([literal(left) for _, left, _ in alternatives], [prefix], body))

    def order_without_cardinality(self, node, line, check_guards):
        operator, left, right = comparison(node)
        if operator is not ast.Eq or "sorted" in self.iterables.changed or "sorted" in self.local_helpers:
            return False
        for subject, sorted_call in ((left, right), (right, left)):
            sorted_call = self.resolve(sorted_call, line)
            if (not isinstance(subject, ast.Name) or not isinstance(sorted_call, ast.Call)
                    or name_of(sorted_call.func) != "sorted" or len(sorted_call.args) != 1
                    or not same(subject, sorted_call.args[0])):
                continue
            source = self.resolve(subject, line)
            if not isinstance(source, (ast.Call, ast.ListComp, ast.Attribute, ast.Subscript)):
                continue
            projection = source.generators[0].iter if isinstance(source, ast.ListComp) else None

            def mentions_subject(node):
                return any(isinstance(part, ast.Name) and part.id == subject.id
                           for part in body_nodes((node,)))

            def independent_check(other, other_line):
                op, first, second = comparison(other)
                if op is ast.Eq and all(isinstance(part, ast.Call) and name_of(part.func) == "len"
                                       and len(part.args) == 1 for part in (first, second)):
                    return any(mentions_subject(part) for part in (first, second)) and not all(
                        mentions_subject(part.args[0]) or mentions_subject(self.resolve(part.args[0], other_line))
                        for part in (first, second))
                if op is ast.Eq and isinstance(projection, (ast.Attribute, ast.Subscript)):
                    for item, peer in ((first, second), (second, first)):
                        if (isinstance(item, ast.Subscript) and same(item.value, subject)
                                and literal(item.slice) == 0
                                and isinstance(peer, (ast.Attribute, ast.Subscript))
                                and same(peer.value, projection.value)):
                            return False
                return mentions_subject(other)

            if not any(other_line != line and independent_check(other, other_line)
                       for other_line, other, guards in self.checks
                       if other is not None and (not guards or guards == check_guards)):
                return True
        return False

    def unchecked_item_order(self):
        """Yield one finding when aggregate checks pass even if no per-item checks run."""
        unguarded = [node for _, node, guards in self.checks if node is not None and not guards]
        for source, item in self.output_loops.items():
            owner = source.value
            if not isinstance(owner, ast.Name) or not isinstance(self.resolve(owner, source.lineno), ast.Call):
                continue
            if not any(isinstance(part, ast.Name) and part.id == owner.id
                       for part in body_nodes(unguarded)):
                continue
            if any(same(part, source) for _, node, guards in self.checks
                   if node is not None and source not in guards for part in body_nodes((node,))):
                continue
            for line, node, guards in self.checks:
                if guards != (source,):
                    continue
                op, left, right = comparison(node)
                if (op in {ast.Lt, ast.LtE, ast.Gt, ast.GtE}
                        and all(isinstance(part, ast.Attribute) and name_of(part.value) == item
                                for part in (left, right))):
                    yield line
                    break

    @staticmethod
    def constructor_calls(value):
        for node in body_nodes((value,)):
            if isinstance(node, ast.Call) and name_of(node.func).rsplit(".", 1)[-1][:1].isupper():
                yield id(node)

    def alias_path(self, operand, line):
        path = binding_path(operand)
        if not path:
            return ""
        root, _, suffix = path.partition(".")
        prior = [(pos, value) for pos, value in self.bindings.get(root, ()) if pos < line]
        if len(prior) == 1 and prior[0][1] in self.alias_stores:
            start, value = prior[0]
            if target := binding_path(value):
                source = self.alias_path(value, start)
                if source and not any(
                        start < pos <= line and name in {root, target.split(".")[0],
                                                       source.split(".")[0]}
                        for pos, name in self.alias_changes):
                    return ".".join(filter(None, (source, suffix)))
        return path

    def self_comparison(self, op, left, right, line, test_name, line_comments):
        if op not in {ast.Eq, ast.Is, ast.NotEq, ast.IsNot}:
            return False
        if not same(left, right):
            path = self.alias_path(left, line)
            return bool(path) and path == self.alias_path(right, line)
        if op is not ast.Eq or not any(isinstance(part, ast.Call) for part in ast.walk(left)):
            return True
        comment = " ".join(line_comments.get(loc, "") for loc in (line - 1, line))
        if DISTINCT_INPUTS.search(test_name) or DISTINCT_INPUTS.search(comment):
            return True
        if REPEATABILITY.search(test_name) or REPEATABILITY.search(comment):
            return False
        for other_line, node, _ in self.checks:
            if other_line == line:
                continue
            operator, first, second = comparison(node)
            for call, other in ((first, second), (second, first)):
                if call is not None and same(call, left) and (
                        operator is ast.NotEq and not same(call, other) or
                        operator is ast.Eq and literal(self.resolve(other, other_line)) is not UNKNOWN):
                    return False
        return True

    def collection_observations(self):
        for line, predicate, guards in self.checks:
            if guards or predicate is None:
                continue
            op, left, right = comparison(predicate)
            candidates = []
            if op is None:
                negated = isinstance(predicate, ast.UnaryOp) and isinstance(predicate.op, ast.Not)
                candidates.append((predicate.operand if negated else predicate, not negated))
            reverse = {ast.Gt: ast.Lt, ast.GtE: ast.LtE, ast.Lt: ast.Gt, ast.LtE: ast.GtE}
            for operand, other, ordered in ((left, right, op), (right, left, reverse.get(op, op))):
                if isinstance(operand, ast.Call) and name_of(operand.func) == "len" and len(operand.args) == 1:
                    size = literal(other)
                    if type(size) is not int:
                        continue
                    operand = operand.args[0]
                elif isinstance(other, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
                    size = len(other.keys if isinstance(other, ast.Dict) else other.elts)
                else:
                    continue
                if ordered is ast.Eq or ordered in {ast.NotEq, ast.Gt, ast.GtE} and (
                        size == 0 and ordered is not ast.GtE or size > 0 and ordered is ast.GtE):
                    candidates.append((operand, ordered is not ast.Eq or size > 0))

            for operand, present in candidates:
                if isinstance(operand, ast.Name) and operand.id in self.writes:
                    continue
                yield self.resolve(operand, line), present, line

    def collection_checks(self, producers, observations):
        for value, present, line in observations:
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                name = value.func.id
                if name in producers and name not in self.iterables.changed:
                    yield name, present, line

    def runtime_sweep_checks(self, observations):
        witnessed = {value for value, present, _ in observations if present}
        for value, present, line in observations:
            if present:
                continue
            value = materialized_iterator(value)
            if not isinstance(value, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                continue
            for iterator in value.generators:
                source = self.resolve(iterator.iter, iterator.iter.lineno)
                if source not in witnessed and runtime_inventory(source):
                    yield line
                    break

    def unchecked_mocks(self):
        uses = defaultdict(list)
        for part in self.iterables.parents:
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load):
                uses[part.id].append(part)
        for name, bindings in self.bindings.items():
            if len(bindings) != 1:
                continue
            _, value = bindings[0]
            if not isinstance(value, ast.Call) or value.args or value.keywords:
                continue
            root, dot, tail = name_of(value.func).partition(".")
            module = self.imports.get(root, root) + dot + tail
            if module not in {"unittest.mock.Mock", "unittest.mock.MagicMock", "unittest.mock.AsyncMock"}:
                continue
            if len(uses[name]) != 1:
                continue
            usage, = uses[name]
            parent, field = self.iterables.parents[usage]
            if isinstance(parent, ast.Call) and field == "args":
                yield parent.lineno

    def failure_collector(self, name):
        sites = self.recorder_sites[name]
        if not sites:
            return False
        for site in sites:
            comparison_branch = False
            while site in self.iterables.parents:
                site, field = self.iterables.parents[site]
                if isinstance(site, ast.If) and field in {"body", "orelse"}:
                    if comparison(site.test)[0] not in {ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE}:
                        return False
                    comparison_branch = True
                elif isinstance(site, (ast.For, ast.AsyncFor)):
                    if isinstance(site, ast.AsyncFor) or not self.iterables.nonempty(site.iter):
                        return False
                elif isinstance(site, (ast.While, ast.Match, ast.Try, ast.TryStar)):
                    return False
            if not comparison_branch:
                return False
        return True

    def recorder_producers(self, recorders):
        producers = defaultdict(set)
        for call in self.calls:
            callee = binding_path(call.func)
            if not callee:
                continue
            for slot, argument in call_arguments(call).items():
                if (isinstance(argument, ast.Attribute) and argument.attr in RECORDING_METHODS
                        and name_of(argument.value) in recorders):
                    producers[name_of(argument.value)].add(((callee, slot, argument.attr), call.lineno))
        return producers

    def issues(self, test_name, pins, strings, line_comments):
        findings = []
        statements = self.iterables.parents
        self.alias_stores = {value for node, _, value in assignments(statements)
                             if isinstance(statements[node][0], (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.alias_changes = set()
        for part in statements:
            if isinstance(part, (ast.Name, ast.Attribute, ast.Subscript)) and isinstance(part.ctx, (ast.Store, ast.Del)):
                path = binding_path(part.value if isinstance(part, ast.Subscript) else part)
            elif isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute):
                path = binding_path(part.func.value)
            else:
                continue
            if path:
                self.alias_changes.add((part.lineno, path.split(".")[0]))
        recorders = {name for name, bindings in self.bindings.items()
                     if isinstance(bindings[0][1], ast.List) and not bindings[0][1].elts}
        result_pins = set(self.result_pins())
        negative = defaultdict(list)
        positive = defaultdict(list)
        for line, node, guards in self.checks:
            if node is None:
                continue
            operator, left, right = comparison(node)
            if constant_truth(node):
                findings.append((line, "const-truth"))
            if self.self_comparison(operator, left, right, line, test_name, line_comments):
                findings.append((line, "self-compare"))
            if operator is ast.Eq and self.coupled(left, right, line, pins, strings, result_pins):
                findings.append((line, "self-derived-expected"))
            if (operator is ast.Eq and self.zero_origin(self.resolve(left, line), right, line)
                    or self.fixed_output_check(node, line)
                    or self.order_without_cardinality(node, line, guards)):
                findings.append((line, "nondistinguishing-literal"))

            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                recorder = self.recorder(node.operand, recorders)
                if recorder:
                    negative[recorder].append(line)
            if isinstance(node, ast.Name) and node.id in recorders:
                positive[node.id].append(line)
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
                    positive[recorder].append(line)

        producers = self.recorder_producers(recorders)
        controlled = set()
        for name, lines in positive.items():
            keys = {key for key, _ in producers[name]}
            if len(keys) != 1 or any(
                    isinstance(parent, ast.Call) and parent.func is site
                    for site in self.recorder_sites[name]
                    if (parent := self.iterables.parents.get(site, (None, ""))[0]) is not None):
                continue
            if min(call_line for _, call_line in producers[name]) <= max(lines):
                controlled.update(keys)
        for name in recorders - positive.keys():
            if name in negative:
                if not self.failure_collector(name):
                    findings.extend((line, "absence-no-control") for line in negative[name]
                                    if not any(key in controlled and call_line <= line
                                               for key, call_line in producers[name]))
            elif name in self.writes and name not in self.reads:
                findings.append((self.writes[name], "absence-no-control"))
        for name, line in self.callback_writes.items():
            bindings = self.bindings.get(name)
            if bindings and name not in self.reads and literal(bindings[0][1]) in (False, 0):
                findings.append((line, "absence-no-control"))
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


def checking_helpers(tree, *, require_evidence=False):
    definitions = {}
    parents = {}

    def index(nodes, scope):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions[scope, node.name] = node
                if require_evidence:
                    index(node.body, (*scope, node.name))
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

    def local_definition(scope, name):
        receiver, _, method = name.rpartition(".")
        if receiver in {"self", "cls"}:
            pending = [scope[:depth] for depth in range(len(scope), 0, -1)
                       if scope[:depth] in parents][:1]
            visited = set()
            while pending:
                owner = pending.pop(0)
                if owner in visited:
                    continue
                visited.add(owner)
                if (owner, method) in definitions:
                    return owner, method
                pending.extend(parents.get(owner, ()))
        elif receiver:
            return tuple(receiver.split(".")), method
        else:
            for depth in range(len(scope), -1, -1):
                owner = scope[:depth]
                if owner not in parents and (owner, name) in definitions:
                    return owner, name
        return (), name

    def checks_call(scope, name):
        key = local_definition(scope, name)
        if key in definitions:
            return key in checked
        return helper(name.rsplit(".", 1)[-1])

    nodes = {key: list(body_nodes(fn.body)) for key, fn in definitions.items()
             if require_evidence or not fn.name.startswith("test")}
    while True:
        found = set()
        for key, inside in nodes.items():
            if key in checked:
                continue
            if require_evidence:
                owner, name = key
                scope = (*owner, name)
                verifies = any(isinstance(node, (ast.Assert, ast.Raise)) or
                               (isinstance(node, ast.Call) and checks_call(scope, name_of(node.func)))
                               for node in inside)
            else:
                known = available(key[0])
                verifies = any(isinstance(node, ast.Assert) or
                               (isinstance(node, ast.Call) and
                                (name_of(node.func) in known or helper(name_of(node.func).rsplit(".", 1)[-1])))
                               for node in inside)
            if verifies:
                found.add(key)
        if not found:
            return (checks_call if require_evidence else available), definitions
        checked.update(found)


def imports_in(statements):
    imports = {}
    for node in statements:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                imports[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module + "." if node.module else "")
            for alias in node.names:
                imports[alias.asname or alias.name] = prefix + alias.name

    return imports


class ModuleConstants:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.modules = {}
        self.trees = {}
        self.delegates = {}

    @staticmethod
    def module_file(root, module):
        stem = root.joinpath(*module)
        candidates = (stem.with_suffix(".py"), stem / "__init__.py") if module else (stem / "__init__.py",)
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def source(self, module):
        if not module[0]:
            levels = next((index for index, part in enumerate(module) if part), len(module))
            if levels <= len(self.path.parents):
                return self.module_file(self.path.parents[levels - 1], module[levels:])
            return None
        for parent in self.path.parents:
            for root in (parent, parent / "src"):
                if candidate := self.module_file(root, module):
                    return candidate
            if (parent / ".git").exists() or (parent / "pyproject.toml").is_file():
                break
        return None

    def parse_module(self, module):
        source = self.source(module)
        if source is None or source == self.path:
            return None
        try:
            with tokenize.open(source) as stream:
                tree = ast.parse(stream.read(), filename=str(source))
        except (OSError, SyntaxError, UnicodeError, LookupError):
            return None
        return tree

    def tree(self, module):
        if module not in self.trees:
            self.trees[module] = self.parse_module(module)
        return self.trees[module]

    def definition(self, path):
        tree = self.tree(path[:-1])
        definitions = [node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == path[-1]] if tree else []
        return definitions[0] if len(definitions) == 1 else None

    def delegate(self, path):
        if path not in self.delegates:
            self.delegates[path] = self.find_delegate(path)
        return self.delegates[path]

    def find_delegate(self, path):
        function = self.definition(path)
        if function is None or function.decorator_list or function.args.vararg or function.args.kwarg:
            return ()
        body = function.body[1:] if ast.get_docstring(function) is not None else function.body
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return ()
        call = body[0].value
        if not isinstance(call, ast.Call) or call.keywords:
            return ()
        owner = path[:-1]
        tree = self.tree(owner)
        imported = imports_in(tree.body)
        parameters = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
        if len(call.args) != len(parameters):
            return ()
        for operand, parameter in zip(call.args, parameters):
            if (isinstance(operand, ast.Call) and isinstance(operand.func, ast.Name)
                    and operand.func.id in {"list", "tuple", "str", "bytes", "int", "float"}
                    and operand.func.id not in scope_changes(tree.body) and operand.func.id not in parameters
                    and len(operand.args) == 1 and not operand.keywords):
                operand = operand.args[0]
            if not isinstance(operand, ast.Name) or operand.id != parameter:
                return ()
        root, dot, suffix = name_of(call.func).partition(".")
        if not root or root in parameters:
            return ()
        target = tuple((imported[root] + dot + suffix).split(".")) if root in imported else (*owner, root)
        return target if (root in imported or not dot) and self.definition(target) is not None else ()

    def directories(self, path):
        function = self.definition(path)
        if function is None or "path" not in path[-1]:
            return ()
        segments = sorted((node.right for node in body_nodes(function.body)
                           if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)),
                          key=lambda node: (node.lineno, node.col_offset))
        return path_directories(value for node in segments if isinstance(value := literal(node), str))

    def string_transform(self, path):
        function = self.definition(path)
        if function is None or function.decorator_list:
            return ""
        body = function.body
        if ast.get_docstring(function) is not None:
            body = body[1:]
        args = parameter_names(function)
        if len(body) == 1 and isinstance(body[0], ast.Return):
            call = body[0].value
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == args.get(0)
                    and not (call.args or call.keywords)):
                return call.func.attr
        return ""

    def dependencies(self, module):
        tree = self.tree(module)
        if tree is None:
            return {}
        names = scope_changes(tree.body)
        constants = {target.id for _, target, _ in assignments(tree.body)
                     if isinstance(target, ast.Name) and target.id.isupper()
                     and names[target.id] == 1}
        reads = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or names[node.name] != 1:
                continue
            local = set(scope_changes(node.body)) | set(parameter_names(node).values())
            local.update(arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg)
            available = constants - local
            reads[node.name] = {part.id for part in body_nodes(node.body)
                                if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load)
                                and part.id in available}
        return reads

    def shared(self, callee, expected):
        module, name = callee[:-1], callee[-1]
        owner, _, constant = expected.rpartition(".")
        if not constant.isupper() or owner != ".".join(module):
            return False
        if module not in self.modules:
            self.modules[module] = self.dependencies(module)
        return constant in self.modules[module].get(name, ())


class GuardIterables(Iterables):
    def __init__(self, test, scope, tables, module_names, tree, path, scan_root):
        super().__init__(test, scope, tables, module_names)
        self.root = project_root(path, scan_root, (".git", "pyproject.toml", "setup.py"))
        self.path = path.resolve()
        self.imports = imports_in(tree.body)
        self.local_imports = {name: (binding, qualified)
                              for binding in self.parents if isinstance(binding, (ast.Import, ast.ImportFrom))
                              for name, qualified in imports_in((binding,)).items()
                              if self.changed[name] == 1}
        self.table_cache = {}
        self.scripts = {}
        self.module_calls = {name_of(node.value.func): node.value
                             for node in tree.body if isinstance(node, ast.Expr)
                             and isinstance(node.value, ast.Call)}
        self.locals.update((target.id, node)
                           for node, target, _ in assignments(self.parents)
                           if isinstance(target, ast.Name) and self.changed[target.id] == 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                changed = scope_changes(node.body)
                for _, target, value in assignments(node.body):
                    if isinstance(target, ast.Name) and changed[target.id] == 1:
                        self.tables = {**self.tables, (node.name, target.id): value}
        self.module_bindings = {target.id: value for _, target, value in assignments(tree.body)
                                if isinstance(target, ast.Name) and module_names[target.id] == 1}
        self.loop_rows = {}

    def module_source(self, source, module):
        relative = len(module) - len(module.lstrip("."))
        parts = module.lstrip(".").split(".") if module.lstrip(".") else []
        if relative:
            if relative > len(source.parents):
                return None
            roots = (source.parents[relative - 1],)
        else:
            roots = (root for parent in source.parents if parent.is_relative_to(self.root)
                     for root in (parent, parent / "src"))
        for root in roots:
            candidate = ModuleConstants.module_file(root, parts)
            if candidate and candidate.resolve().is_relative_to(self.root):
                return candidate.resolve()
        return None

    def imported_table(self, source, qualified):
        module, dot, name = qualified.rpartition(".")
        if not module.strip("."):
            module += dot
        if not dot or not name.isupper() or not (path := self.module_source(source, module)):
            return None
        return self.source_table(path, name)

    def source_table(self, path, name):
        key = path, name
        if key in self.table_cache:
            return self.table_cache[key]
        self.table_cache[key] = None
        try:
            with tokenize.open(path) as stream:
                tree = ast.parse(stream.read(), filename=str(path))
        except (OSError, SyntaxError, UnicodeError, LookupError):
            return None
        changed = scope_changes(tree.body)
        if changed[name] == 1:
            values = dict(file_iterables(tree.body, changed))
            value = values.get((name,))
            if not nonempty_literal(value):
                value = self.imported_table(path, imports_in(tree.body).get(name, ""))
            self.table_cache[key] = value
        return self.table_cache[key]

    def script(self, alias):
        if alias in self.scripts:
            return self.scripts[alias]

        def arguments(node, callee):
            if isinstance(node, ast.Call) and name_of(node.func) == callee and not node.keywords:
                return node.args
            return ()

        module_args = arguments(self.module_bindings.get(alias), "importlib.util.module_from_spec")
        module_spec = self.module_bindings.get(name_of(module_args[0])) if len(module_args) == 1 else None
        spec_args = arguments(module_spec, "importlib.util.spec_from_loader")
        loader = spec_args[1] if len(spec_args) == 2 else None
        loader_args = arguments(self.module_bindings.get(name_of(loader)), "importlib.machinery.SourceFileLoader")
        path_arg = loader_args[1] if len(loader_args) == 2 else None
        path_args = arguments(path_arg, "str")
        path_expr = self.module_bindings.get(name_of(path_args[0])) if len(path_args) == 1 else None
        path = None
        loader_name = name_of(loader)
        exec_args = arguments(self.module_calls.get(loader_name + ".exec_module"), loader_name + ".exec_module")
        if len(exec_args) != 1 or name_of(exec_args[0]) != alias:
            return None
        if isinstance(path_expr, ast.BinOp) and isinstance(path_expr.op, ast.Div):
            parent, filename = path_expr.left, literal(path_expr.right)
            if (isinstance(parent, ast.Subscript) and isinstance(filename, str)
                    and same(parent.value, ast.parse("Path(__file__).resolve().parents", mode="eval").body)):
                level = literal(parent.slice)
                if type(level) is int and 0 <= level < len(self.path.parents):
                    candidate = (self.path.parents[level] / filename).resolve()
                    if candidate.is_relative_to(self.root):
                        path = candidate
        self.scripts[alias] = path
        return path

    def definition(self, node):
        name = binding_path(node)
        if not name:
            return None
        if name in self.loop_rows:
            return self.loop_rows[name]
        value = super().definition(node)
        if value is not None:
            return value
        root, dot, member = name.partition(".")
        if member and self.changed[name]:
            return None
        if root in self.changed:
            local = self.local_imports.get(root)
            if local:
                imported_at, imported = local
                if self.precedes(imported_at, node):
                    return self.imported_table(self.path, imported + dot + member)
            return None
        if self.module_names[root] != 1:
            return None
        if imported := self.imports.get(root):
            return self.imported_table(self.path, imported + dot + member)
        if member.isupper() and (path := self.script(root)):
            return self.source_table(path, member)
        return None

    def rows(self, node):
        value = self.definition(node) if isinstance(node, (ast.Name, ast.Attribute)) else node
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            if not any(isinstance(item, ast.Starred) for item in value.elts):
                return value.elts
        if isinstance(value, ast.Dict) and all(key is not None for key in value.keys):
            return value.keys
        if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and all(
                not generator.is_async and not generator.ifs and self.nonempty(generator.iter)
                for generator in value.generators):
            return [value.elt]
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and len(value.args) == 1:
            if (value.func.id in {"set", "frozenset"} and not value.keywords
                    and value.func.id not in self.changed and not self.module_names[value.func.id]):
                return self.rows(value.args[0])
        if isinstance(value, ast.Subscript) and isinstance(value.value, (ast.Name, ast.Attribute)):
            mapping = self.definition(value.value)
            if isinstance(mapping, ast.Dict) and mapping.values and all(
                    nonempty_literal(item) for item in mapping.values):
                return [None]
        return []

    def precedes(self, statement, node):
        block = self.parents[statement]
        while node in self.parents:
            if self.parents[node] == block:
                return (statement.lineno, statement.col_offset) < (node.lineno, node.col_offset)
            node = self.parents[node][0]
        return False

    def appended(self, node):
        binding = self.locals.get(node.id)
        if not (isinstance(binding, (ast.Assign, ast.AnnAssign))
                and isinstance(binding.value, ast.List)):
            return False
        return any(self.parents[statement] == self.parents[binding]
                   and binding.lineno < statement.lineno and self.precedes(statement, node)
                   and isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                   and name_of(statement.value.func) == node.id + ".append"
                   for statement in self.parents)

    def nonempty(self, node):
        if super().nonempty(node) or self.rows(node):
            return True
        if isinstance(node, ast.Name) and self.appended(node):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            left = node.left
            if (isinstance(left, ast.Call) and name_of(left.func) == "set"
                    and len(left.args) == 1 and not left.keywords
                    and "set" not in self.changed and not self.module_names["set"]
                    and isinstance(node.right, ast.Set)):
                removed = [literal(item) for item in node.right.elts]
                return UNKNOWN not in removed and any(
                    (value := literal(item)) is not UNKNOWN and value not in removed
                    for item in self.rows(left.args[0]))
        if (isinstance(node, ast.Call) and name_of(node.func) == "zip" and len(node.args) >= 2
                and "zip" not in self.changed and not self.module_names["zip"]
                and len(node.keywords) == 1 and node.keywords[0].arg == "strict"
                and literal(node.keywords[0].value) is True):
            return any(self.nonempty(arg) for arg in node.args)
        return False

    def enter_loop(self, node):
        target = node.target
        if not isinstance(target, (ast.Tuple, ast.List)):
            return
        rows = self.rows(node.iter)
        if not rows or not all(isinstance(row, (ast.Tuple, ast.List))
                               and len(row.elts) == len(target.elts) for row in rows):
            return
        for column, name in enumerate(target.elts):
            if isinstance(name, ast.Name) and self.changed[name.id] == 1:
                values = [row.elts[column] for row in rows]
                if all(nonempty_literal(value) for value in values):
                    self.loop_rows[name.id] = values[0]


def collection_producers(definitions):
    for (owner, name), node in definitions.items():
        if owner or name.startswith("test"):
            continue
        annotation = node.returns
        annotation = annotation.value if isinstance(annotation, ast.Subscript) else annotation
        if name_of(annotation) in {"list", "tuple", "set", "dict", "List", "Tuple", "Set", "Dict"}:
            yield name
            continue
        returns = [part.value for part in body_nodes(node.body) if isinstance(part, ast.Return)]
        if returns and all(isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict,
                                             ast.ListComp, ast.SetComp, ast.DictComp)) for value in returns):
            yield name


def materialized_iterator(node):
    while (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
           and node.func.id in {"list", "tuple", "set", "sorted"}
           and len(node.args) == 1 and not node.keywords):
        node = node.args[0]
    return node


def runtime_inventory(node):
    node = materialized_iterator(node)
    if not isinstance(node, ast.Call):
        return False
    return (name_of(node.func) in {"listdir", "scandir", "os.listdir", "os.scandir", "glob", "iglob"}
            or isinstance(node.func, ast.Attribute)
            and node.func.attr in {"glob", "rglob", "iglob", "iterdir"})


def success_check(node):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"is_ok", "success"} and not node.args and not node.keywords):
        return True
    op, left, right = comparison(node)
    return op is ast.Eq and any(
        isinstance(status, ast.Attribute) and status.attr in {"returncode", "exit_code"}
        and type(literal(expected)) is int and literal(expected) == 0
        for status, expected in ((left, right), (right, left)))


def status_only(evidence):
    for part in evidence:
        if isinstance(part, ast.Assert) and success_check(part.test):
            continue
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute):
            if part.func.attr == "check_returncode" and not part.args and not part.keywords:
                continue
            if success_check(predicate(part, part.func.attr)):
                continue
        return False
    return True


def setup_only(node):
    checked = set()
    unchecked = False
    for statement in node.body:
        if isinstance(statement, ast.Assert):
            if unchecked:
                return False
            parts = tuple(ast.walk(statement.test))
            if any(isinstance(part, ast.Call) and name_of(part.func) not in {"len", "bool"}
                   for part in parts):
                return False
            checked.update(part.id for part in parts if isinstance(part, ast.Name))
        elif checked:
            if isinstance(statement, ast.Expr):
                value = statement.value
            elif isinstance(statement, ast.Assign) and all(name_of(target) == "_" for target in statement.targets):
                value = statement.value
            else:
                continue
            if not isinstance(value, ast.Call):
                continue
            arguments = (*value.args, *(kw.value for kw in value.keywords))
            callee = name_of(value.func)
            receiver, _, method = callee.rpartition(".")
            if callee == "print" or receiver in {"log", "logger", "logging"} and method in {
                    "debug", "info", "warning", "error", "exception", "critical"}:
                unchecked |= any(isinstance(part, ast.Call) for argument in arguments for part in ast.walk(argument))
            else:
                unchecked |= any(isinstance(part, ast.Name) and part.id in checked
                                 for argument in arguments for part in ast.walk(argument))
    return unchecked


def has_outcome_evidence(node, scope, checks_call):
    evidence = [part for part in body_nodes(node.body)
                if isinstance(part, ast.Assert)
                or isinstance(part, ast.Raise) and raises_assertion(part)
                or isinstance(part, ast.Call) and checks_call(scope, name_of(part.func))]
    if not evidence:
        return False
    if claims_effect(node.name) and status_only(evidence):
        return False
    if any(not isinstance(part, ast.Assert) for part in evidence):
        return True
    if OUTCOME_CLAIM.search(node.name) and all(
            isinstance(part.test, ast.Call) and isinstance(part.test.func, ast.Attribute)
            and part.test.func.attr == "is_ok" and not part.test.args and not part.test.keywords
            for part in evidence):
        return False
    return not setup_only(node)


def body_fingerprint(node, tokens, lines):
    first, last = node.body[0], node.body[-1]
    first_column = len(lines[first.lineno - 1].encode()[:first.col_offset].decode())
    last_column = len(lines[last.end_lineno - 1].encode()[:last.end_col_offset].decode())
    start = bisect_left(tokens, (first.lineno, first_column), key=lambda token: token.start)
    end = bisect_left(tokens, (last.end_lineno, last_column), key=lambda token: token.start)
    return node.name, tuple(
        (token.type, "" if token.type in {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE} else token.string)
        for token in tokens[start:end] if token.type not in {tokenize.COMMENT, tokenize.NL}
    )


def parse(source, path, *, scan_root=None, collection_controls=None):
    tree = ast.parse(source, filename=str(path))
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    comments = [
        Comment(start[0], start[1], text[1:].strip(), not raw[:start[1]].strip())
        for kind, text, start, _, raw in tokens
        if kind == tokenize.COMMENT
    ]
    line_comments = {comment.line: comment.text for comment in comments}
    imports = imports_in(tree.body)
    module_constants = ModuleConstants(path)

    checks_in, definitions = checking_helpers(tree)
    if not (functions := list(test_functions(tree.body))):
        return [], comments
    checks_call, _ = checking_helpers(tree, require_evidence=True)
    parameters, methods = file_parameters(definitions)
    local_helpers = {node.name for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and not node.name.startswith("test")}
    module_names = scope_changes(tree.body)
    tables = dict(file_iterables(tree.body, module_names))
    tests = []
    inspected = []
    for node, parents in functions:
        iterables = Iterables(node, parents, tables, module_names)
        local_parameters = {local.name: parameter_names(local) for local in node.body
                          if isinstance(local, (ast.FunctionDef, ast.AsyncFunctionDef))}
        test_parameters = ChainMap(local_parameters, methods.get(parents, {}), parameters)
        oracle_imports = {name: module for name, module in imports.items()
                          if module_names[name] == 1 and name not in iterables.changed}
        oracle_imports.update((name, module) for name, module in imports_in(node.body).items()
                              if iterables.changed[name] == 1)
        inspector = Inspect(imports, checks_in(parents), local_helpers, iterables, test_parameters,
                            oracle_imports, module_constants, definitions, parents)
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
        inspected.append((test, inspector, node))
        test.guarded = bool(inspector.checks) and all(guards for _, _, guards in inspector.checks)
        if test.guarded and scan_root is not None:
            guard_iterables = GuardIterables(node, parents, tables, module_names, tree, path, scan_root)
            guard_inspector = Inspect(imports, checks_in(parents), local_helpers, guard_iterables,
                                      test_parameters, oracle_imports, module_constants, definitions, parents)
            for statement in node.body:
                guard_inspector.visit(statement)
            test.guarded = all(guards for _, _, guards in guard_inspector.checks)
        test.conditional = bool(inspector.constraints)
        test.measured = inspector.measured
        tests.append(test)
    pins = {pin for _, inspector, _ in inspected for pin in inspector.literal_pins()}
    producers = set(collection_producers(definitions))
    observations = [list(inspector.collection_observations()) for _, inspector, _ in inspected]
    collections = [list(inspector.collection_checks(producers, checks))
                   for (_, inspector, _), checks in zip(inspected, observations)]
    witnessed = {name for checks in collections for name, present, _ in checks if present}
    fingerprints = {}
    if collection_controls is not None:
        lines = source.splitlines()
        for name in {name for checks in collections for name, _, _ in checks}:
            fingerprints[name] = body_fingerprint(definitions[(), name], tokens, lines)
        for name in witnessed:
            collection_controls.record(path, scan_root, fingerprints[name])
    for (test, inspector, node), collection_checks, checks in zip(inspected, collections, observations):
        strings = {part.value for part in body_nodes(node.body)
                   if isinstance(part, ast.Constant) and isinstance(part.value, str)}
        test.issues = inspector.issues(test.name, pins, strings, line_comments)
        scope = (*inspector.owner, node.name)
        for name, present, line in collection_checks:
            if present or name in witnessed:
                continue
            if collection_controls is None:
                test.issues.append((line, "absence-no-control"))
            else:
                collection_controls.record(path, scan_root, fingerprints[name], test, line)
        test.issues.extend((line, "absence-no-control") for line in inspector.runtime_sweep_checks(checks))
        test.issues.extend((line, "absence-no-control") for line in inspector.unchecked_mocks())
        if not has_outcome_evidence(node, scope, checks_call):
            test.issues.append((test.line, "no-assert"))
    return tests, comments
