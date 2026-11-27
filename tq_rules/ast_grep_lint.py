"""Test evidence derived from ast-grep nodes for both supported languages."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import tokenize

from . import lint
from .ast_grep import (
    CALLS, COMMENTS, FUNCTIONS, MEMBERS, NAMES, RUST_ASSERTIONS, SCOPES, Call, Syntax,
    attribute_name, attributes, children, line, macro_name, namespace,
    offset, opposite, parts, query, shape, split_operands, syntax_errors, unwrap,
)
from .source import Test, project_root


UNKNOWN = lint.UNKNOWN
FAILURES = {"panic", "unreachable", "todo", "unimplemented"}
RECORDING = {"append", "push", "extend", "insert", "add", "set"}
SHRINKING = {"clear", "drain", "pop", "pop_front", "pop_back", "retain", "remove",
             "truncate", "discard", "difference_update", "intersection_update", "take"}
RUST_NUMERIC_SUFFIXES = ("f32", "f64", "usize", "isize", *(
    prefix + size for prefix in ("u", "i") for size in ("128", "64", "32", "16", "8")
))
PYTHON_PREDICATES = {
    "assertEqual": "==", "assertEquals": "==", "assertNotEqual": "!=", "assertNotEquals": "!=",
    "assertGreater": ">", "assertGreaterEqual": ">=", "assertLess": "<", "assertLessEqual": "<=",
    "assertIs": "is", "assertIsNot": "is not", "assertListEqual": "==", "assertTupleEqual": "==",
}


def literal(syntax, node):
    node = unwrap(node)
    if node is None:
        return UNKNOWN
    kind = node.kind()
    if kind in {"boolean_literal", "true", "false"}:
        return node.text().lower() == "true"
    if kind in {"none", "unit_expression"}:
        return None if kind == "none" else ()
    if kind in {"integer_literal", "integer", "float_literal", "float"}:
        number = node.text().replace("_", "")
        suffix = ""
        if syntax.language == "rust":
            suffix = next((suffix for suffix in RUST_NUMERIC_SUFFIXES if number.endswith(suffix)
                           and not (suffix.startswith("f") and number.startswith("0x"))), "")
            number = number.removesuffix(suffix)
        try:
            value = (float(number) if kind in {"float", "float_literal"} or suffix.startswith("f")
                     else int(number, 0) if number.startswith(("0x", "0o", "0b")) else int(number))
        except ValueError:
            return UNKNOWN
        return lint.SuffixedNumber(value, suffix) if suffix else value
    if kind in {"unary_expression", "unary_operator"} and node.children()[0].text() in {"-", "+"}:
        value = literal(syntax, children(node)[0])
        sign = -1 if node.children()[0].text() == "-" else 1
        if type(value) in {float, int}:
            return value * sign
        if isinstance(value, lint.SuffixedNumber):
            return lint.SuffixedNumber(value.value * sign, value.suffix)
    if kind in {"string", "string_literal", "raw_string_literal", "char_literal"}:
        text = node.text()
        if kind == "raw_string_literal":
            return text[text.index('"') + 1:text.rindex('"')]
        quote = next((index for index, char in enumerate(text) if char in {"'", '"'}), None)
        if quote is not None and not any(char in text[:quote].lower() for char in "ft"):
            delimiter = text[quote] * (3 if text[quote:quote + 3] == text[quote] * 3 else 1)
            content = text[quote + len(delimiter):-len(delimiter)]
            if "\\" not in content or "r" in text[:quote].lower():
                return content.encode() if text[:quote].lower() == "b" else content
    if kind in {"tuple", "list", "array_expression", "tuple_expression", "dictionary", "set"}:
        if any(child.kind() == ";" for child in node.children()):
            return UNKNOWN
        values = [literal(syntax, child) for child in children(node)]
        if UNKNOWN not in values:
            if kind == "dictionary":
                return {} if not values else UNKNOWN
            return tuple(values) if kind in {"tuple", "tuple_expression"} else values
    if kind == "macro_invocation" and macro_name(node) == ("vec",):
        args = syntax.macro_arguments(node)
        if args or node.text().endswith(("[]", "()", "{}")):
            values = [literal(syntax, child) for child in args]
            if UNKNOWN not in values:
                return values
    return UNKNOWN


def tri_truth(syntax, node):
    node = unwrap(node)
    value = literal(syntax, node)
    if type(value) is bool or syntax.language == "python" and value is not UNKNOWN:
        return bool(value)
    negated = opposite(node)
    if negated is not None:
        result = tri_truth(syntax, negated)
        return UNKNOWN if result is UNKNOWN else not result
    operator, left, right = split_operands(node)
    if operator in {"&&", "||", "and", "or"}:
        first, second = (tri_truth(syntax, child) for child in (left, right))
        decisive = operator in {"||", "or"}
        if decisive in (first, second):
            return decisive
        if UNKNOWN not in (first, second):
            return not decisive
    if operator in {"==", "!="}:
        first, second = (literal(syntax, child) for child in (left, right))
        if UNKNOWN not in (first, second):
            return (first == second) == (operator == "==")
    return UNKNOWN


@dataclass
class Check:
    source: object
    syntax: Syntax
    kind: str
    predicate: object = None
    operator: str = ""
    left: object = None
    right: object = None
    guarded: bool = False
    outcome: bool = True
    reachability: bool = True

    @property
    def location(self):
        return self.syntax.absolute(self.source)

    def truth(self):
        if self.predicate is not None:
            value = tri_truth(self.syntax, self.predicate)
            return not value if self.kind == "assertFalse" and value is not UNKNOWN else value
        if self.operator in {"==", "!="}:
            first, second = (literal(self.syntax, child) for child in (self.left, self.right))
            if UNKNOWN not in (first, second):
                return (first == second) == (self.operator == "==")
        return UNKNOWN


def check_call(syntax, node, call):
    method = call.method
    args = call.arguments
    if method in RUST_ASSERTIONS and call.macro:
        if method.endswith(("_eq", "_ne")) and len(args) >= 2:
            operator = "==" if method.endswith("_eq") else "!="
            return Check(node, syntax, method, operator=operator, left=args[0], right=args[1])
        predicate = args[0] if args and not method.endswith(("_eq", "_ne", "_matches")) else None
    elif method in PYTHON_PREDICATES and not call.macro:
        operator = PYTHON_PREDICATES[method]
        slots = {}
        position = 0
        for arg in args:
            if arg.kind() == "keyword_argument":
                slots[child_text(arg.field("name"))] = arg.field("value")
            else:
                slots[position] = arg
                position += 1
        return Check(node, syntax, method, operator=operator,
                     left=slots.get(0, slots.get("first")), right=slots.get(1, slots.get("second")))
    else:
        predicate = args[0] if args and method in {"assertTrue", "assertFalse", "assert_"} else None
    return Check(node, syntax, method, predicate, *split_operands(predicate))


def child_text(node):
    return node.text() if node is not None else ""


def function_name(node):
    return child_text(node.field("name")).removeprefix("r#")


def body_of(node):
    return node.field("body")


def direct_statements(body):
    for node in children(body):
        if node.kind() == "expression_statement":
            yield from children(node)
        else:
            yield node


def binding(node):
    if node.kind() in {"assignment", "augmented_assignment", "assignment_expression",
                       "compound_assignment_expr", "let_declaration", "const_item", "static_item"}:
        target = node.field("left") or node.field("pattern") or node.field("name")
        value = node.field("right") or node.field("value")
        return parts(target), value
    return (), None


def integer(syntax, node):
    value = literal(syntax, node)
    if isinstance(value, lint.SuffixedNumber):
        value = value.value
    return value if type(value) is int else None


def range_size(start, stop, step=1):
    if step == 0:
        return None
    distance = (stop - start) * (1 if step > 0 else -1)
    return max(0, (distance + abs(step) - 1) // abs(step))


class Flow:
    """Record checks and prove when execution cannot skip all of them."""

    def __init__(self, file, syntax, body, scope, initial=()):
        self.file = file
        self.syntax = syntax
        self.body = body
        self.scope = scope
        self.call_kinds = ("call",) if syntax.language == "python" else ("call_expression", "macro_invocation")
        self.bindings = defaultdict(list)
        self.writes = defaultdict(list)
        self.calls = []
        self.checks = []
        self.loops = []
        self.alias_changes = defaultdict(list)
        self.collection_growth = defaultdict(list)
        self.strategies = dict(initial)
        self.active_closures = set()

    def record(self, check):
        self.checks.append(check)
        return check.reachability and not check.guarded

    def conditional(self, node):
        start = len(self.checks)
        growth = {name: list(sites) for name, sites in self.collection_growth.items()}
        self.visit(node)
        self.collection_growth = defaultdict(list, growth)
        for check in self.checks[start:]:
            check.guarded = True
        return False

    def known_value(self, node, seen=frozenset()):
        number = integer(self.syntax, node)
        if number is not None:
            return number
        name = parts(node)
        if name and name not in seen and (value := self.resolve(name, self.syntax.absolute(node))) is not None:
            return self.known_value(value, seen | {name})
        operator, left, right = split_operands(node)
        if operator in {"+", "-", "*"}:
            first, second = (self.known_value(item, seen) for item in (left, right))
            if first is not None and second is not None:
                return {"+": first + second, "-": first - second, "*": first * second}[operator]
        return None

    def resolve(self, name, before):
        entries = self.bindings.get(name, ())
        if entries:
            value = next((value for site, value in reversed(entries) if site < before), None)
            return value
        return self.file.constant(name, self.scope)

    def size(self, node, *, before=None, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return None
        if before is None:
            before = self.syntax.absolute(node)
        kind = node.kind()
        elements = children(node)
        if kind in {"list", "tuple", "set", "dictionary", "array_expression", "tuple_expression"}:
            if any(child.kind() in {"list_splat", "dictionary_splat"} for child in elements):
                return sum(child.kind() not in {"list_splat", "dictionary_splat"} for child in elements)
            if any(child.kind() == ";" for child in node.children()):
                return self.known_value(elements[-1])
            return len(elements)
        value = literal(self.syntax, node)
        if isinstance(value, (str, bytes)):
            return len(value)
        if kind == "list_comprehension":
            clauses = children(node)[1:]
            if len(clauses) == 1 and clauses[0].kind() == "for_in_clause":
                return self.size(clauses[0].field("right"), before=before, seen=seen)
        if kind in {"reference_expression", "try_expression"}:
            return self.size(elements[-1], before=before, seen=seen)
        if kind == "range_expression":
            if len(elements) == 2:
                ends = [self.known_value(child) for child in elements]
                if ends[1] is None and len(name := parts(elements[1])) == 1 and name[0] in self.strategies:
                    ends[1] = self.strategy_minimum(self.strategies[name[0]])
                if None not in ends:
                    return range_size(ends[0], ends[1] + int(any(child.kind() == "..="
                                                                     for child in node.children())))
        if kind in CALLS:
            call = self.syntax.call(node)
            args = call.arguments
            method = call.method
            if method == "range" and 1 <= len(args) <= 3:
                sizes = [self.known_value(arg) for arg in args]
                if None not in sizes:
                    return range_size(0, sizes[0]) if len(sizes) == 1 else range_size(*sizes)
            if call.name == ("vec",):
                if args:
                    return len(args)
                tree = node.find(kind="token_tree")
                if tree is not None and not children(tree):
                    return 0
                if tree is not None:
                    array = Syntax.parse("fn tq() { let _ = [" + tree.text()[1:-1] + "]; }", "rust")
                    repeated = array.root.find(kind="array_expression")
                    if repeated is not None and not syntax_errors(array.root, missing=True):
                        return self.size(repeated, before=before, seen=seen)
            if call.receiver is None:
                if method in {"enumerate", "reversed", "iter", "list", "tuple", "sorted", "array", "repeat"} and args:
                    return self.size(args[0], before=before, seen=seen)
                if method in {"once", "repeat_with"} or method == "repeat" and self.syntax.language == "rust":
                    return 1
                if method == "zip" and args:
                    sizes = [self.size(arg, before=before, seen=seen) for arg in args]
                    return min(sizes) if None not in sizes else None
                if method in {"new", "empty", "default"} and call.name[:-1] in {
                        ("Vec",), ("VecDeque",), ("HashSet",), ("HashMap",), ("BTreeSet",),
                        ("BTreeMap",), ("std", "iter")}:
                    return 0
                target = self.file.resolve_function(call.name, self.scope)
                if target is not None and not args and target not in seen:
                    function = self.file.functions[target]
                    statements = list(direct_statements(body_of(function)))
                    if len(statements) == 1:
                        expression = statements[0]
                        if expression.kind() in {"return_statement", "return_expression"}:
                            expression = next(iter(children(expression)), None)
                        return self.size(expression, before=before, seen=seen | {target})
            else:
                if self.syntax.language == "python" and call.name[:1] == ("self",) and not args:
                    target = self.file.resolve_function(call.name, self.scope)
                    if target is not None and target not in seen:
                        statements = list(direct_statements(body_of(self.file.functions[target])))
                        returned = statements[-1] if statements else None
                        if returned is not None and returned.kind() == "return_statement":
                            return self.size(next(iter(children(returned)), None), before=before,
                                             seen=seen | {target})
                if method in {"iter", "iter_mut", "into_iter", "chars", "bytes", "lines", "items", "keys",
                              "values", "enumerate", "rev", "map", "cloned", "copied", "collect", "as_slice",
                              "to_vec", "iter_rows", "by_ref", "cycle", "values_mut", "into_values"}:
                    return self.size(call.receiver, before=before, seen=seen)
                size = self.size(call.receiver, before=before, seen=seen)
                first = self.known_value(args[0]) if args else None
                if method in {"take", "skip", "step_by", "chunks", "chunks_exact", "windows"} and size is not None and first is not None:
                    if method == "take":
                        return min(first, size)
                    if method == "skip":
                        return max(0, size - first)
                    if first > 0:
                        if method == "chunks_exact":
                            return size // first
                        if method == "windows":
                            return max(0, size - first + 1)
                        return (size + first - 1) // first
                if method == "chain" and args:
                    other = self.size(args[0], before=before, seen=seen)
                    return (size or 0) + (other or 0)
                if method == "zip" and args and size is not None:
                    other = self.size(args[0], before=before, seen=seen)
                    return min(size, other) if other is not None else None
        name = parts(node)
        if not name or name in seen:
            return None
        entries = self.bindings.get(name, ())
        if not entries and len(name) == 1 and (strategy := self.strategies.get(name[0])) is not None:
            if strategy.kind() == "call_expression":
                call = self.syntax.call(strategy)
                if call.name[-2:] == ("collection", "vec") and len(call.arguments) == 2:
                    return self.strategy_minimum(call.arguments[1])
            return None
        binding_site = max((site for site, _ in entries if site < before), default=-1)
        if any(binding_site < start < before for start in self.writes[name]):
            return None
        value = self.resolve(name, before)
        if value is None:
            return None
        initial = self.size(value, before=before, seen=seen | {name})
        grown = sum(added for site, added in self.collection_growth[name] if binding_site < site < before)
        return (initial or 0) + grown if grown else initial

    def strategy_minimum(self, node):
        if node.kind() != "range_expression":
            return integer(self.syntax, node)
        ends = children(node)
        if len(ends) != 2 or node.children()[0].kind() in {"..", "..="}:
            return 0
        return integer(self.syntax, ends[0])

    def comprehension_guard(self, check):
        predicate = check.predicate
        if predicate is None or predicate.kind() not in CALLS:
            return False
        call = self.syntax.call(predicate)
        if call.method != "all" or len(call.arguments) != 1:
            return False
        generator = call.arguments[0]
        if generator.kind() not in {"generator_expression", "list_comprehension"}:
            return False
        return any(child.kind() == "if_clause" or child.kind() == "for_in_clause"
                   and (any(part.kind() == "async" for part in child.children())
                        or not self.size(child.field("right"))) for child in children(generator))

    def branch(self, node):
        before = len(self.checks)
        primary = self.visit(node.field("condition"))
        if primary:
            self.visit(node.field("consequence"))
            for arm in children(node):
                if arm.kind() in {"elif_clause", "else_clause"}:
                    self.visit(arm)
            return True
        branches = [self.visit(node.field("consequence"))]
        alternatives = [child for child in children(node) if child.kind() in {"elif_clause", "else_clause"}]
        for arm in alternatives:
            if arm.kind() == "elif_clause":
                condition = self.visit(arm.field("condition"))
                body = self.visit(arm.field("consequence"))
                branches.append(condition or body)
            else:
                branches.append(self.visit(arm.field("body") or next(iter(children(arm)), None)))
        exhaustive = bool(alternatives and alternatives[-1].kind() == "else_clause" and all(branches))
        if not exhaustive:
            for check in self.checks[before:]:
                check.guarded = True
        return exhaustive

    def match(self, node):
        subject = node.field("value") or node.field("subject")
        before = len(self.checks)
        condition = self.visit(subject)
        body = node.field("body")
        arms = [part for part in children(body) if part.kind() in {"match_arm", "case_clause"}] if body else []
        if self.syntax.language == "python":
            arms = [part for part in children(node) if part.kind() == "case_clause"] or arms
        covered = []
        for arm in arms:
            first = len(self.checks)
            covered.append((self.visit(arm), self.checks[first:]))
        if self.syntax.language == "rust" and len(covered) > 1:
            if all(checked for checked, _ in covered):
                return True
            if sum(not checked for checked, _ in covered) == 1 and all(
                    not checked or checks and all(check.kind in FAILURES and not check.guarded for check in checks)
                    for checked, checks in covered):
                return True
        if not condition:
            for check in self.checks[before:]:
                check.guarded = True
        return condition

    def can_exit_block(self, statement):
        jumps = (("return_statement", "break_statement", "continue_statement")
                 if self.syntax.language == "python" else
                 ("return_expression", "break_expression", "continue_expression"))
        loops = {"for_statement", "while_statement", "for_expression", "while_expression", "loop_expression"}
        start = offset(statement)
        for jump in query(statement, *jumps):
            boundaries = SCOPES if jump.kind().startswith("return") else SCOPES | loops
            if not any(parent.kind() in boundaries and offset(parent) >= start for parent in jump.ancestors()):
                return True
        return False

    def visit(self, node):
        if node is None:
            return False
        kind = node.kind()
        if kind in SCOPES | COMMENTS:
            return False
        if kind in {"block", "expression_statement"}:
            possible_exit = False
            guaranteed = False
            for statement in children(node):
                checked = self.conditional(statement) if possible_exit else self.visit(statement)
                guaranteed |= checked
                possible_exit |= self.can_exit_block(statement)
            return guaranteed
        operator, left, right = split_operands(node)
        if operator in {"and", "or", "&&", "||"}:
            checked = self.visit(left)
            self.conditional(right)
            return checked
        if kind in {"if_statement", "if_expression"}:
            return self.branch(node)
        if kind in {"match_statement", "match_expression"}:
            return self.match(node)
        if kind in {"for_statement", "while_statement", "for_expression", "while_expression", "loop_expression"}:
            body = node.field("body")
            iterator = node.field("right") or node.field("value")
            condition = node.field("condition")
            evaluated = self.visit(iterator) if iterator is not None else self.visit(condition)
            size = self.size(iterator)
            runs = (size is not None and size > 0 and not any(child.kind() == "async" for child in node.children())
                    or condition is not None and tri_truth(self.syntax, condition) is True
                    or kind == "loop_expression")
            self.loops.append((iterator, node.field("pattern") or node.field("left"), body, runs))
            before = self.collection_growth.copy()
            self.collection_growth = defaultdict(list, {name: sites.copy() for name, sites in before.items()})
            checked = self.visit(body) if runs else self.conditional(body)
            if runs and size and size > 1:
                for name, sites in self.collection_growth.items():
                    old = len(before.get(name, ()))
                    sites[old:] = [(site, added * size) for site, added in sites[old:]]
            if not runs:
                self.collection_growth = defaultdict(list, before)
            alternate = self.visit(node.field("alternative"))
            return evaluated or runs and checked or alternate
        if kind == "assert_statement":
            predicate = next(iter(children(node)), None)
            check = Check(node, self.syntax, "assert", predicate, *split_operands(predicate))
            check.guarded = self.comprehension_guard(check)
            return self.record(check)
        if kind == "raise_statement":
            exception = next(iter(children(node)), None)
            name = (self.syntax.call(exception).method if exception is not None and exception.kind() in CALLS
                    else child_text(exception))
            if name == "AssertionError" or self.file.pending_helpers:
                return self.record(Check(node, self.syntax, name))
        if kind == "let_declaration":
            alternative = node.field("alternative")
            if alternative is not None:
                self.conditional(alternative)
                return self.visit(node.field("value"))
            annotation = node.field("type")
            if annotation is not None and annotation.kind() == "function_type" and child_text(
                    node.field("pattern")) == "_" and node.field("value") is not None:
                return self.record(Check(node, self.syntax, "function type"))
        if kind in CALLS:
            call = self.syntax.call(node)
            self.calls.append((node, call))
            receiver = parts(call.receiver)
            site = self.syntax.absolute(node)
            if receiver:
                if call.method in SHRINKING:
                    self.writes[receiver].append(site)
                if call.method not in {"clone", "len", "as_ref", "is_empty", "as_str", "as_slice",
                                        "to_vec", "to_owned", "iter", "contains", "get"}:
                    self.alias_changes[receiver[0]].append(site)
                if call.method in {"push", "append", "push_back", "push_front", "extend"}:
                    added = (self.size(call.arguments[0]) or 0) if call.method == "extend" and call.arguments else 1
                    self.collection_growth[receiver].append((site, added))
            if self.syntax.language == "rust" and call.receiver is not None:
                owner = self.receiver_owner(call.receiver, site)
                if owner:
                    call = Call((*owner, call.method), call.arguments, call.receiver)
            recognized, outcome = self.file.check_call(call, self.scope)
            if self.syntax.language == "rust" and call.method in {"unwrap", "expect"} and call.receiver is not None:
                inner = unwrap(call.receiver)
                if inner.kind() in CALLS and self.syntax.call(inner).method.startswith(("validate", "verify", "check")):
                    recognized = outcome = True
                if (call.method == "expect" and self.scope[-1].endswith("_request_format")
                        and call.arguments and integer(self.syntax, call.arguments[0]) is not None):
                    recognized = outcome = False
            checked_callback = False
            if self.syntax.language == "rust" and not call.macro:
                local = self.resolve(call.name, site) if len(call.name) == 1 else None
                if local is not None and local.kind() == "closure_expression":
                    identity = offset(local)
                    if identity not in self.active_closures:
                        self.active_closures.add(identity)
                        try:
                            checked_callback = self.visit(body_of(local))
                        finally:
                            self.active_closures.remove(identity)
                for argument in call.arguments:
                    if argument.kind() == "reference_expression" and any(
                            child.kind() == "mutable_specifier" for child in children(argument)):
                        if borrowed := parts(children(argument)[-1]):
                            self.alias_changes[borrowed[0]].append(site)
                    if argument.kind() == "closure_expression":
                        callback = body_of(argument)
                        if call.method in {"unwrap_or_else", "expect_err"} and callback is not None and any(
                                macro_name(inner)[-1:] in {(failure,) for failure in FAILURES}
                                for inner in query(callback, "macro_invocation")):
                            recognized = outcome = True
                        if callback is not None:
                            checked_callback |= self.visit(callback)
            if recognized:
                check = check_call(self.syntax, node, call)
                check.outcome = outcome
                check.guarded = self.comprehension_guard(check)
                if self.syntax.language == "rust" and not call.macro:
                    check.reachability = (
                        lint.helper(call.method) or call.method.startswith("debug_assert")
                        or call.receiver is None and len(call.name) == 1
                        and (*self.scope[:-1], call.method) in self.file.functions
                    )
                checked = self.record(check)
                if not call.macro:
                    for argument in children(node):
                        if argument.kind() not in {"arguments", "argument_list"}:
                            self.visit(argument)
                return checked or checked_callback
            if checked_callback:
                return True
            if call.macro:
                return False
        target, value = binding(node)
        if target:
            location = self.syntax.absolute(node)
            if len(target) > 1 or kind in {"augmented_assignment", "compound_assignment_expr"}:
                self.writes[target[:1]].append(location)
            if self.bindings.get(target) or len(target) > 1:
                self.alias_changes[target[0]].append(location)
            self.bindings[target].append((location, value))
        return any([self.visit(child) for child in children(node)])

    def receiver_owner(self, node, before, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return ()
        if node.kind() in {"await_expression", "try_expression", "reference_expression"}:
            return self.receiver_owner(children(node)[-1], before, seen)
        if node.kind() == "struct_expression":
            return parts(node.field("name"))
        if node.kind() in CALLS:
            call = self.syntax.call(node)
            if call.receiver is not None:
                return self.receiver_owner(call.receiver, before, seen) if call.method in {
                    "unwrap", "expect", "clone", "as_ref"} else ()
            if len(call.name) > 1 and call.name[-2][:1].isupper():
                return call.name[:-1]
            target = self.file.resolve_function(call.name, self.scope)
            if target is not None:
                return parts(self.file.functions[target].field("return_type"))
        path = parts(node)
        if len(path) == 1 and path not in seen and (value := self.resolve(path, before)) is not None:
            return self.receiver_owner(value, self.syntax.absolute(value), seen | {path})
        return ()

    def resolve_alias(self, node, before, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return ()
        if node.kind() == "reference_expression":
            node = children(node)[-1]
        if node.kind() in CALLS and self.syntax.language == "rust":
            call = self.syntax.call(node)
            if call.method in {"clone", "to_owned", "to_vec"} and not call.arguments:
                node = call.receiver
        name = parts(node)
        if not name or name in seen:
            return ()
        root = name[:1]
        entries = [(site, value) for site, value in self.bindings.get(root, ()) if site < before]
        if len(entries) != 1:
            return name
        site, value = entries[0]
        if value is None or value.kind() == "reference_expression" and any(
                child.kind() == "mutable_specifier" for child in children(value)):
            return name
        alias = self.resolve_alias(value, site, seen | {name})
        if alias and not any(site < changed < before for key in {root[0], alias[0]}
                             for changed in self.alias_changes[key]):
            return (*alias, *name[1:])
        return name

    def compares_itself(self, check, name, comments):
        operator, left, right = check.operator, check.left, check.right
        if operator not in {"==", "!=", "is", "is not"} or left is None or right is None:
            return False
        lhs = shape(left)
        rhs = shape(right)
        if lhs != rhs:
            alias = self.resolve_alias(left, check.location)
            return bool(alias) and alias == self.resolve_alias(right, check.location)
        if operator not in {"==", "is"} or not query(left, *self.call_kinds):
            return True
        at = self.file.line_at(check.location)
        comment = " ".join(comments.get(position, "") for position in (at - 1, at))
        if any(lint.DISTINCT_INPUTS.search(text) for text in (name, comment)):
            return True
        if any(lint.REPEATABILITY.search(text) for text in (name, comment)):
            return False
        for companion in self.checks:
            if companion is check:
                continue
            for operand, other in ((companion.left, companion.right), (companion.right, companion.left)):
                if shape(operand) == lhs and (
                        companion.operator == "!=" and shape(other) != lhs
                        or companion.operator == "==" and (
                            literal(self.syntax, other) is not UNKNOWN or self.size(other, before=companion.location)
                            is not None)):
                    return False
        return True

    def success_only(self, check):
        if check.kind == "check_returncode":
            return True
        predicate = check.predicate
        if predicate is not None and predicate.kind() in CALLS:
            call = self.syntax.call(predicate)
            return call.method in {"is_ok", "success"} and not call.arguments
        if check.operator == "==":
            for operand, other in ((check.left, check.right), (check.right, check.left)):
                name = parts(operand)
                if name and name[-1] in {"returncode", "exit_code"} and integer(self.syntax, other) == 0:
                    return True
        return False

    def verifies_outcome(self, name):
        checks = [check for check in self.checks if check.outcome]
        if not checks:
            return False
        if lint.claims_effect(name):
            observations = [check for check in checks if check.kind in RUST_ASSERTIONS | {"assert"}]
            if observations and all(self.success_only(check) for check in observations) and all(
                    self.success_only(check) or check.kind not in RUST_ASSERTIONS | {"assert"}
                    and check.kind in {"init", "expect", "check_returncode"} for check in checks):
                return False
            if all(self.success_only(check) for check in checks):
                return False
        if lint.OUTCOME_CLAIM.search(name) and all(
                check.predicate is not None and check.predicate.kind() in CALLS
                and self.syntax.call(check.predicate).method == "is_ok" for check in checks):
            return False
        if not all(check.kind in RUST_ASSERTIONS | {"assert"} for check in checks):
            return True
        return not self.setup_only(checks)

    def setup_only(self, checks):
        direct = {self.syntax.absolute(node): node for node in direct_statements(self.body)}
        if any(check.location not in direct for check in checks):
            return False
        seen = set()
        for check in checks:
            operands = (check.predicate,) if check.predicate is not None else (check.left, check.right)
            for operand in (part for part in operands if part is not None):
                if any(self.syntax.call(call).method not in {"len", "bool"}
                       for call in query(operand, *self.call_kinds)):
                    return False
                seen.update(identifier.text() for identifier in query(operand, "identifier"))
        for node, call in self.calls:
            site = self.syntax.absolute(node)
            if site <= checks[-1].location or site not in direct:
                continue
            if call.method in {"drop", "close", "remove_dir_all", "unlink"}:
                continue
            if call.macro and call.method not in {"println", "eprintln", "dbg"}:
                continue
            if call.method in {"print", "println", "eprintln", "debug", "info", "warning"}:
                if any(query(arg, *self.call_kinds) for arg in call.arguments):
                    return True
            elif any(identifier.text() in seen for arg in call.arguments for identifier in query(arg, "identifier")):
                return True
        return False

    def observations(self, check):
        predicate = unwrap(check.predicate)
        negated = opposite(predicate)
        subject = negated or predicate
        positive = (negated is None) != (check.kind == "assertFalse")
        subject_path = parts(subject)
        if subject_path or subject is not None and subject.kind() in CALLS and self.syntax.language == "python":
            yield subject, positive
        if subject is not None and subject.kind() in CALLS:
            call = self.syntax.call(subject)
            if call.method == "is_empty" and not call.arguments:
                yield call.receiver, not positive
            elif positive and call.method in {"any", "contains", "contains_key"}:
                receiver = call.receiver
                if receiver is not None and receiver.kind() in CALLS:
                    previous = self.syntax.call(receiver)
                    if previous.method in {"iter", "iter_mut"}:
                        receiver = previous.receiver
                yield receiver, True
        reverse = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
        for operand, other, operator in ((check.left, check.right, check.operator),
                                         (check.right, check.left, reverse.get(check.operator, check.operator))):
            if operand is None or other is None:
                continue
            if operand.kind() in CALLS:
                call = self.syntax.call(operand)
                if call.method == "len":
                    operand = call.receiver or (call.arguments[0] if call.arguments else None)
            known = literal(self.syntax, other)
            if isinstance(known, lint.SuffixedNumber):
                known = known.value
            length = self.size(other, before=check.location)
            if operand is None or known is UNKNOWN and length is None:
                continue
            value = length if known is UNKNOWN else known
            if operator == "==":
                yield operand, bool(value)
            elif operator in {"!=", ">"} and not value or operator == ">=" and type(value) is int and value > 0:
                yield operand, True

    def recorder_evidence(self):
        recorders = {}
        for name, entries in self.bindings.items():
            if len(name) != 1 or len(entries) != 1 or entries[0][1] is None:
                continue
            _, value = entries[0]
            raw = literal(self.syntax, value)
            if raw in ([], (), {}, False, 0):
                recorders[name] = "empty" if type(raw) in {list, tuple, dict} else "counter"
            elif value.kind() in CALLS:
                call = self.syntax.call(value)
                if call.name in {("Vec", "new"), ("Vec", "default"), ("Vec", "with_capacity"),
                                  ("VecDeque", "new"), ("list",)}:
                    recorders[name] = "empty"
                elif call.name[-2:] == ("Cell", "new") and call.arguments and literal(
                        self.syntax, call.arguments[0]) in (False, 0):
                    recorders[name] = "counter"

        inputs = defaultdict(list)
        writes = defaultdict(list)
        calls = self.calls.copy()
        callback_kind = "lambda" if self.syntax.language == "python" else "closure_expression"
        if self.syntax.language == "python":
            for node in query(self.body, "function_definition", "lambda"):
                if node.kind() == "function_definition" and not any(
                        parts(arg) == (function_name(node),) for _, call in self.calls for arg in call.arguments):
                    continue
                calls.extend((call, self.syntax.call(call)) for call in query(node, "call"))
        for node, call in calls:
            receiver = parts(call.receiver)
            site = self.syntax.absolute(node)
            if receiver in recorders and call.method in RECORDING:
                writes[receiver].append((site, node))
            for index, arg in enumerate(call.arguments):
                if arg.kind() == "keyword_argument":
                    slot = child_text(arg.field("name"))
                    arg = arg.field("value")
                else:
                    slot = index
                if arg is not None and arg.kind() == "reference_expression":
                    name = parts(children(arg)[-1])
                    is_recorder = any(child.kind() == "mutable_specifier" for child in children(arg))
                else:
                    path = parts(arg)
                    name = path[:-1]
                    is_recorder = path[-1:] and path[-1] in RECORDING
                if is_recorder and name in recorders:
                    owner = self.receiver_owner(call.receiver, site) if self.syntax.language == "rust" else ()
                    callee = (*owner, call.method) if owner else call.name
                    producer = self.file.resolve_function(callee, self.scope) or callee
                    inputs[name].append(((producer, slot, "mut" if self.syntax.language == "rust" else path[-1]), site))

        positive = defaultdict(list)
        negative = defaultdict(list)
        observations = []
        for check in self.checks:
            for operand, present in self.observations(check):
                if operand is not None:
                    observations.append((operand, present, check))
                if name := parts(operand):
                    (positive if present else negative)[name].append(check)
        controlled = {producer for name, checks in positive.items() for producer, site in inputs[name]
                      if not writes[name] and len({key for key, _ in inputs[name]}) == 1
                      and any(site < check.location for check in checks)}
        if self.syntax.language == "rust":
            self.file.recorder_witnesses.update((producer, slot) for producer, slot, _ in controlled
                                               if producer in self.file.functions)
        for name in recorders.keys() - positive.keys():
            if name in negative and recorders[name] == "empty":
                if self.is_failure_collector(writes[name]) and not inputs[name]:
                    continue
                for check in negative[name]:
                    producers = {producer for producer, site in inputs[name] if site < check.location}
                    if producers & controlled:
                        continue
                    if self.syntax.language == "rust" and len(producers) == 1:
                        producer, slot, _ = next(iter(producers))
                        if producer in self.file.functions and not writes[name]:
                            self.file.recorder_absences.append(((producer, slot), self.file.line_at(check.location)))
                            continue
                    yield self.file.line_at(check.location)
            elif name in writes and name not in negative:
                methods = {parts(call.receiver) for _, call in self.calls if call.method not in RECORDING}
                if name not in methods and not self.is_read_in_checks(name):
                    callback_write = next((site for site, node in writes[name]
                                           if any(parent.kind() in {callback_kind, "function_definition"}
                                                  for parent in node.ancestors()
                                                  if offset(parent) > offset(self.body))), None)
                    if callback_write is not None:
                        yield self.file.line_at(callback_write)
        yield from self.unobserved_callback_writes(recorders)
        self.detector_absences(observations)

    def is_read_in_checks(self, name):
        for check in self.checks:
            for expression in (check.predicate, check.left, check.right):
                if expression is not None and any(
                        parts(node) == name for node in query(expression, "identifier")):
                    return True
        return False

    def unobserved_callback_writes(self, recorders):
        closures, assignments = (
            (("lambda", "function_definition"), ("assignment", "augmented_assignment"))
            if self.syntax.language == "python" else
            (("closure_expression",), ("assignment_expression", "compound_assignment_expr"))
        )
        for closure in query(self.body, *closures):
            is_python_fn = closure.kind() == "function_definition"
            nonlocal_names = {child.text() for statement in (query(closure, "nonlocal_statement") if is_python_fn else [])
                              for child in children(statement)}
            for statement in query(closure, *assignments):
                name, _ = binding(statement)
                if (recorders.get(name) == "counter" and (not is_python_fn or name[0] in nonlocal_names)
                        and not self.is_read_in_checks(name)):
                    site = self.syntax.absolute(statement)
                    if self.syntax.language == "rust":
                        site = next((self.syntax.absolute(parent) for parent in statement.ancestors()
                                     if parent.kind() == "call_expression" and parent.inside(kind="block")),
                                    site)
                    yield self.file.line_at(site)

    def is_failure_collector(self, sites):
        if not sites:
            return False
        for _, node in sites:
            loops = [entry for entry in self.loops if entry[2] is not None and
                     entry[2].range().start.index <= offset(node) < entry[2].range().end.index]
            if not loops or any(not runs for _, _, _, runs in loops):
                return False
            branch = next((parent for parent in node.ancestors() if parent.kind() in
                           {"if_statement", "if_expression"}), None)
            if branch is None or split_operands(branch.field("condition"))[0] not in {
                    "!=", "not in", ">", "<", ">=", "<="} and opposite(branch.field("condition")) is None:
                return False
        return True

    def detector_absences(self, observations):
        present_values = set()
        candidates = []
        for operand, present, check in observations:
            if check.guarded:
                continue
            name = parts(operand)
            if name and (self.writes.get(name) or self.collection_growth.get(name)):
                continue
            value = self.resolve(name, check.location) if name else operand
            if value is None:
                continue
            candidates.append((value, present, check.location))
            if present:
                present_values.add(shape(value))
        reported = set()
        for value, present, site in candidates:
            if not present:
                sources = self.iteration_sources(value)
                if any(self.runtime_inventory(source) and shape(source) not in present_values
                       for source in sources):
                    self.file.detectors.append((("sweep", shape(value)), False, self.file.line_at(site)))
                    continue
            producer = self.detector(value)
            if producer is not None and (producer, present) not in reported:
                self.file.detectors.append((producer, present, self.file.line_at(site)))
                reported.add((producer, present))

    def iteration_sources(self, node):
        if node.kind() in {"list_comprehension", "set_comprehension", "dictionary_comprehension",
                           "generator_expression"}:
            for clause in query(node, "for_in_clause"):
                source = clause.field("right")
                yield self.resolve(parts(source), self.syntax.absolute(source)) or source
        elif node.kind() in CALLS:
            call = self.syntax.call(node)
            if call.method in {"filter", "filter_map", "map", "collect", "flatten", "iter", "into_iter",
                               "unwrap", "expect", "copied", "cloned"}:
                source = call.receiver
                if source is not None:
                    value = self.resolve(parts(source), self.syntax.absolute(source))
                    if value is not None:
                        yield value
                    yield from self.iteration_sources(source)
            elif call.method in {"list", "set", "tuple"} and call.arguments:
                yield from self.iteration_sources(call.arguments[0])
            elif call.method in {"read_dir", "iterdir", "glob", "rglob", "listdir", "walk"}:
                yield node

    def runtime_inventory(self, node, seen=frozenset()):
        if node is None or node.kind() not in CALLS:
            return False
        call = self.syntax.call(node)
        if call.method in {"read_dir", "iterdir", "glob", "rglob", "listdir", "walk"}:
            return True
        if call.receiver is not None:
            return self.runtime_inventory(call.receiver, seen)
        if call.method in {"list", "tuple", "set", "sorted"} and call.arguments:
            return self.runtime_inventory(call.arguments[0], seen)
        if (target := self.file.resolve_function(call.name, self.scope)) is not None and target not in seen:
            body = body_of(self.file.functions[target])
            return any(self.runtime_inventory(call, seen | {target}) for call in query(body, *self.call_kinds)
                       if call.kind() != "macro_invocation")
        return False

    def detector(self, node):
        if node.kind() not in CALLS:
            return None
        call = self.syntax.call(node)
        target = self.file.resolve_function(call.name, self.scope)
        if target is None or call.receiver is not None:
            return None
        function = self.file.functions[target]
        body = body_of(function)
        returns = function.field("return_type")
        while returns is not None and returns.kind() == "type" and children(returns):
            returns = children(returns)[0]
        annotation = parts(returns)
        if annotation[-1:] in {("Vec",), ("VecDeque",), ("HashSet",), ("BTreeSet",), ("list",), ("set",)}:
            return target[-1], shape(body)
        if any(self.syntax.call(part).method in {"append", "push", "extend", "collect"}
               for part in query(body, *self.call_kinds) if part.kind() != "macro_invocation"):
            return target[-1], shape(body)
        return None


class File:
    def __init__(self, source, language, path, scan_root=None):
        self.syntax = Syntax.parse(source, language)
        self.lines = [0]
        for index, char in enumerate(source):
            if char == "\n":
                self.lines.append(index + 1)
        self.path = path
        self.scan_root = scan_root
        self.module_files = {}
        self.functions = {}
        self.function_items = []
        self.bases = {}
        self.macros = {}
        self.constants = {}
        self.imports = {}
        self.detectors = []
        self.recorder_witnesses = set()
        self.recorder_absences = []
        self.helper_checks = {}
        self.pending_helpers = set()
        self.incomplete = []
        self.index()

    def line_at(self, position):
        return bisect_right(self.lines, position)

    def functions_in(self, syntax):
        kind = "function_definition" if syntax.language == "python" else "function_item"
        return query(syntax.root, kind)

    def index(self):
        rust = self.syntax.language == "rust"
        for node in self.functions_in(self.syntax):
            if body_of(node) is not None and function_name(node):
                name = (*namespace(node), function_name(node))
                self.function_items.append((name, node))
                self.functions.setdefault(name, node)
        if not rust:
            for node in query(self.syntax.root, "class_definition"):
                bases = node.field("superclasses")
                if bases is not None:
                    self.bases[(*namespace(node), function_name(node))] = [
                        name for base in children(bases) if (name := parts(base))
                    ]
        declarations = ("const_item", "static_item", "use_declaration", "macro_definition") if rust else (
            "assignment", "import_statement", "import_from_statement")
        table = defaultdict(list)
        for node in query(self.syntax.root, *declarations):
            if any(parent.kind() in FUNCTIONS for parent in node.ancestors()):
                continue
            owner = namespace(node)
            kind = node.kind()
            if kind == "macro_definition":
                self.macros[(*owner, function_name(node))] = node
            elif kind == "use_declaration":
                self.index_rust_import(node.field("argument"), owner)
            elif kind.startswith("import"):
                for child in node.field_children("name"):
                    name = child.field("name") or child
                    alias = child.field("alias")
                    if name is not None:
                        module = child_text(node.field("module_name")).strip(".")
                        path = tuple(name.text().split("."))
                        self.imports[(*owner, child_text(alias) or path[0])] = (
                            *module.split("."), *path) if module else path
            else:
                name, value = binding(node)
                if name and value is not None:
                    table[(*owner, *name)].append(value)
        self.constants = {name: entries[0] for name, entries in table.items() if len(entries) == 1}

    def index_rust_import(self, node, scope, prefix=()):
        if node is None:
            return
        kind = node.kind()
        if kind == "scoped_use_list":
            self.index_rust_import(node.field("list"), scope, (*prefix, *parts(node.field("path"))))
        elif kind == "use_list":
            for child in children(node):
                self.index_rust_import(child, scope, prefix)
        elif kind == "use_as_clause":
            path = (*prefix, *parts(node.field("path")))
            alias = child_text(node.field("alias"))
            if path and alias:
                self.imports[(*scope, alias)] = path
        elif path := parts(node):
            self.imports[(*scope, path[-1])] = (*prefix, *path)

    def constant(self, name, scope):
        if not name:
            return None
        if name[0] == "crate":
            return self.constants.get(name[1:]) or self.imported_constant(name[1:])
        for end in range(len(scope), -1, -1):
            owner = scope[:end]
            path = (*owner, *name)
            if path in self.constants:
                return self.constants[path]
            imported = self.imports.get((*owner, name[0]))
            if imported:
                qualified = (*imported, *name[1:])
                if qualified[:1] == ("crate",):
                    qualified = qualified[1:]
                if qualified in self.constants:
                    return self.constants[qualified]
                if result := self.imported_constant(qualified):
                    return result
        return self.imported_constant(name)

    def imported_constant(self, qualified):
        if self.scan_root is None or len(qualified) < 2:
            return None
        module, name = qualified[:-1], qualified[-1:]
        if module not in self.module_files:
            self.module_files[module] = self.read_module(module)
        return self.module_files[module].get(name)

    def read_module(self, module):
        current = self.path.resolve()
        if self.syntax.language == "rust":
            root = project_root(current, self.scan_root, {"Cargo.toml", ".git"})
            bases = (root / "src", current.parent)
            extension, package = ".rs", "mod.rs"
        else:
            root = project_root(current, self.scan_root, {"pyproject.toml", "setup.py", ".git"})
            bases = (root, root / "src", current.parent)
            extension, package = ".py", "__init__.py"
        for base in bases:
            stem = base.joinpath(*module)
            for candidate in (stem.with_suffix(extension), stem / package):
                if not candidate.is_file() or candidate == current or not candidate.resolve().is_relative_to(root):
                    continue
                try:
                    if self.syntax.language == "python":
                        with tokenize.open(candidate) as stream:
                            source = stream.read()
                    else:
                        source = candidate.read_text(encoding="utf-8")
                    return File(source, self.syntax.language, candidate).constants
                except (OSError, UnicodeError, ValueError, SyntaxError):
                    return {}
        return {}

    def python_method(self, owner, name, seen=frozenset()):
        if not owner or owner in seen:
            return None
        target = (*owner, *name)
        if target in self.functions:
            return target
        for base in self.bases.get(owner, ()):
            for depth in range(len(owner) - 1, -1, -1):
                parent = (*owner[:depth], *base)
                if parent in self.bases or (*parent, *name) in self.functions:
                    target = self.python_method(parent, name, seen | {owner})
                    if target is not None:
                        return target
        return None

    def resolve_function(self, name, scope, *, macro=False):
        if not name:
            return None
        definitions = self.macros if macro else self.functions
        if name[0] == "self" and self.syntax.language == "python":
            return self.python_method(scope[:-1], name[1:])
        if name[0] == "crate":
            name = name[1:]
        for count in range(len(scope), -1, -1):
            owner = scope[:count]
            if name[0] == "super" and count:
                candidate = (*owner[:-1], *name[1:])
            elif name[0] == "self":
                candidate = (*owner, *name[1:])
            else:
                candidate = (*owner, *name)
            if candidate in definitions:
                return candidate
            imported = self.imports.get((*owner, name[0]))
            if imported:
                candidate = (*imported, *name[1:])
                if candidate in definitions:
                    return candidate
        return None

    def checks_in_helper(self, target, macro):
        key = macro, target
        if key in self.helper_checks:
            return self.helper_checks[key]
        if key in self.pending_helpers:
            return False
        self.pending_helpers.add(key)
        try:
            if macro:
                definition = self.macros[target]
                checks = False
                for rule in query(definition, "macro_rule"):
                    expansion = rule.field("right")
                    if expansion is None:
                        continue
                    source = expansion.text()
                    edits = [(offset(part) - offset(expansion), part.range().end.index - offset(expansion))
                             for part in query(expansion, "metavariable")]
                    for start, end in reversed(edits):
                        source = source[:start] + "tq_metavariable" + source[end:]
                    prefix = "fn tq() {"
                    syntax = Syntax.parse(prefix + source[1:-1] + "}", "rust")
                    body = syntax.root.find(kind="block")
                    flow = Flow(self, syntax, body, target)
                    flow.visit(body)
                    checks |= any(check.outcome for check in flow.checks)
            else:
                function = self.functions[target]
                flow = Flow(self, self.syntax, body_of(function), target)
                flow.visit(flow.body)
                checks = any(check.outcome and (check.kind not in FAILURES
                             and check.kind not in {"expect", "unwrap_or_else"}
                             and not check.kind.startswith("debug_assert")
                             or target[-1].startswith(("assert", "check", "verify")))
                             for check in flow.checks)
            self.helper_checks[key] = checks
            return checks
        finally:
            self.pending_helpers.remove(key)

    def check_call(self, call, scope):
        method = call.method
        if call.macro and method in RUST_ASSERTIONS | FAILURES:
            return True, True
        if (target := self.resolve_function(call.name, scope, macro=call.macro)) is not None:
            if self.syntax.language == "rust" and not call.macro:
                definition = self.functions[target]
                associated = any(owner.kind() == "impl_item" for owner in definition.ancestors())
                if call.receiver is None and not lint.helper(method) and (
                        associated or (len(call.name) > 1 or target[:-1] != scope[:-1])
                        and target[:-1] != scope):
                    return False, False
                parameters = definition.field("type_parameters")
                if method.startswith(("assert", "check")) and parameters is not None and query(
                        parameters, "trait_bounds"):
                    return True, True
            verifies = self.checks_in_helper(target, call.macro)
            return verifies or lint.helper(method) and not call.macro, verifies
        if method in {"check_returncode", "assert_that"} or lint.helper(method) or (
                self.syntax.language == "rust" and method.startswith("debug_assert")):
            return True, True
        return False, False

    def inventory(self):
        if self.syntax.language == "python":
            for names, fn in self.function_items:
                if not names[-1].startswith("test") or any(
                        parent.kind() in FUNCTIONS for parent in fn.ancestors()):
                    continue
                parent = fn.parent()
                start = offset(parent) if parent.kind() == "decorated_definition" else offset(fn)
                yield self.syntax, fn, Test(names[-1], ".".join((
                    *self.path.with_suffix("").parts, *names)), line(fn), self.line_at(start),
                    fn.range().end.line + 1, fn.range().start.column, qualified="::".join(names)), (), ()
            return
        for names, fn in self.function_items:
            if item := self.rust_test(self.syntax, fn, names):
                yield item
        for macro in query(self.syntax.root, "macro_invocation"):
            path = macro_name(macro)
            if path and path[-1] == "proptest":
                for syntax, fn, strategies in self.syntax.property_items(macro):
                    names = (*namespace(macro), function_name(fn))
                    item = self.rust_test(syntax, fn, names, strategies.items())
                    if item:
                        yield item
                    self.incomplete.extend(syntax.macro_errors)

    def rust_test(self, syntax, fn, names, initial=()):
        attrs = attributes(fn)
        marked = {attribute_name(attr) for attr in attrs}
        if (not attrs or not any(path and path[-1] in {"test", "rstest", "test_case"}
                                 for path in marked) or any(path and path[-1] == "template" for path in marked)):
            return None
        start = self.line_at(syntax.absolute(attrs[0]))
        marker = next((child for child in fn.children() if child.kind() == "fn"), fn)
        site = self.line_at(syntax.absolute(marker))
        stop = syntax.absolute(fn) + len(fn.text())
        end = self.line_at(stop - 1)
        column = syntax.absolute(marker) - self.lines[site - 1]
        test = Test(names[-1], "::".join(names), site, start, end, column,
                    qualified="::".join(names),
                    span_columns=(syntax.absolute(attrs[0]) - self.lines[start - 1], stop - self.lines[end - 1]))
        return syntax, fn, test, marked, initial


def parse(source, path, *, scan_root=None, collection_controls=None, inventory=False, observe=None):
    from .ast_grep_languages import ECMASCRIPT_LANGUAGES

    if path.suffix in ECMASCRIPT_LANGUAGES:
        from .ast_grep_ts import parse as parse_ecmascript

        return parse_ecmascript(source, path, ECMASCRIPT_LANGUAGES[path.suffix],
                                inventory=inventory, observe=observe)
    file = File(source, "rust" if path.suffix == ".rs" else "python", path, scan_root)
    if file.syntax.language == "python" and (errors := syntax_errors(file.syntax.root)):
        if observe is not None:
            observe(path, [], syntax_errors(file.syntax.root, missing=True), [])
        raise SyntaxError("invalid syntax", (str(path), errors[0], 0, ""))
    comments = list(file.syntax.comments())
    tests = []
    line_comments = {comment.line: comment.text for comment in comments}
    spans = {(comment.line, comment.column) for comment in comments if comment.standalone}
    observations = []
    pending_recorders = []
    for syntax, fn, test, attrs, initial in file.inventory():
        if body_of(fn) is None:
            continue
        while (test.start - 1, test.column) in spans:
            test.start -= 1
        if file.syntax.language == "python":
            trailing = {comment.line for comment in comments if comment.standalone and comment.column > test.column}
            while test.end + 1 in trailing:
                test.end += 1
        flow = Flow(file, syntax, body_of(fn), tuple(test.qualified.split("::")), initial)
        guaranteed = flow.visit(flow.body)
        should_panic = ("should_panic",) in attrs
        ignored = ("ignore",) in attrs
        ignored_tool = ignored and all(check.kind == "expect" for check in flow.checks)
        test.guarded = any(check.reachability for check in flow.checks) and not (
            guaranteed or should_panic or ignored_tool)
        if not (should_panic or ignored or flow.verifies_outcome(test.name)):
            test.issues.append((test.line, "no-assert"))
        for check in flow.checks:
            check_line = file.line_at(check.location)
            if check.truth() is True:
                test.issues.append((check_line, "const-truth"))
            if flow.compares_itself(check, test.name, line_comments):
                test.issues.append((check_line, "self-compare"))
        test.issues.extend((site, "absence-no-control") for site in flow.recorder_evidence())
        observations.extend((key, present, site, test) for key, present, site in file.detectors)
        file.detectors.clear()
        pending_recorders.extend((key, site, test) for key, site in file.recorder_absences)
        file.recorder_absences.clear()
        if inventory:
            test.checks = len(flow.checks)
            test.body_hash = hashlib.sha256(repr(shape(flow.body)).encode()).hexdigest()
        tests.append(test)
        file.incomplete.extend(syntax.macro_errors)
        syntax.macro_errors.clear()
    for key, site, test in pending_recorders:
        if key not in file.recorder_witnesses:
            test.issues.append((site, "absence-no-control"))
    witnessed = {key for key, present, _, _ in observations if present}
    if collection_controls is not None:
        for key in witnessed:
            collection_controls.record(path, scan_root, key)
    for key, present, site, test in observations:
        if present or key in witnessed:
            continue
        if collection_controls is None:
            test.issues.append((site, "absence-no-control"))
        else:
            collection_controls.record(path, scan_root, key, test, site)
    if observe is not None:
        observe(path, tests, syntax_errors(file.syntax.root, missing=True),
                [file.line_at(site) for site in set(file.incomplete)])
    return tests, comments
