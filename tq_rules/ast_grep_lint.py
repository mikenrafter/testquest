"""Test evidence derived from ast-grep nodes for both supported languages."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import hashlib
from math import comb, prod
import operator
import tokenize
import tomllib

from . import ast_grep_micro as micro, lint
from .ast_grep import (
    CALLS, COMMENTS, FUNCTIONS, MEMBERS, NAMES, RUST_ASSERTIONS, SCOPES, Call, Syntax,
    attribute_name, attributes, children, line, location, macro_name, macro_tree, namespace,
    offset, opposite, parts, query, shape, split_operands, syntax_errors, unwrap,
)
from .source import Test, project_root


UNKNOWN = lint.UNKNOWN
FAILURES = {"panic", "unreachable", "todo", "unimplemented"}
RECORDING = {"append", "push", "extend", "insert", "add", "set"}
SHRINKING = {"clear", "drain", "pop", "pop_front", "pop_back", "retain", "remove",
             "truncate", "discard", "difference_update", "intersection_update", "take", "shutdown",
             "detach_all", "join_all"}
RUST_NUMERIC_SUFFIXES = ("f32", "f64", "usize", "isize", *(
    prefix + size for prefix in ("u", "i") for size in ("128", "64", "32", "16", "8")
))
COMPARISONS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
               ">": operator.gt, ">=": operator.ge}
REVERSE_COMPARISON = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
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


def tri_truth(syntax, node, known=None):
    node = unwrap(node)
    value = literal(syntax, node)
    if value is UNKNOWN and known is not None:
        value = known(node)
    if type(value) is bool or syntax.language == "python" and value is not UNKNOWN:
        return bool(value)
    negated = opposite(node)
    if negated is not None:
        result = tri_truth(syntax, negated, known)
        return UNKNOWN if result is UNKNOWN else not result
    operator, left, right = split_operands(node)
    if operator in {"&&", "||", "and", "or"}:
        first, second = (tri_truth(syntax, child, known) for child in (left, right))
        decisive = operator in {"||", "or"}
        if decisive in (first, second):
            return decisive
        if UNKNOWN not in (first, second):
            return not decisive
    if operator in {"==", "!="}:
        first, second = (literal(syntax, child) for child in (left, right))
        if known is not None:
            first = known(left) if first is UNKNOWN else first
            second = known(right) if second is UNKNOWN else second
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

    def truth(self, known=None):
        if self.predicate is not None:
            value = tri_truth(self.syntax, self.predicate, known)
            return not value if self.kind == "assertFalse" and value is not UNKNOWN else value
        if self.operator in {"==", "!="}:
            first, second = (literal(self.syntax, child) for child in (self.left, self.right))
            if known is not None:
                first = known(self.left) if first is UNKNOWN else first
                second = known(self.right) if second is UNKNOWN else second
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
        self.recorder_changes = defaultdict(list)
        self.recorder_escapes = None
        self.collection_growth = defaultdict(list)
        strategy_entries = []
        param_values = {}
        for key, val in initial:
            if key == "__params__":
                param_values = val
            else:
                strategy_entries.append((key, val))
        self.strategies = dict(strategy_entries)
        self.param_values = param_values
        self.loop_values = {}
        self.loop_ranges = {}
        self.checked_collections = {}
        self.vector_collections = set()
        self.active_closures = set()
        self.reaching_facts = None

    def record(self, check):
        self.checks.append(check)
        if check.reachability and not check.guarded:
            predicate = unwrap(check.predicate)
            if predicate is not None and predicate.kind() in CALLS:
                call = self.syntax.call(predicate)
                if call.method == "contains" and call.receiver is not None:
                    for source in self.positive_sources(call.receiver, check.location):
                        name = parts(source)
                        if any(site < check.location for site, _ in self.bindings.get(name, ())):
                            self.checked_collections[name] = check.location
            return True
        return False

    def conditional(self, node):
        start = len(self.checks)
        self.isolated(node)
        for check in self.checks[start:]:
            check.guarded = True
        return False

    def isolated(self, node):
        growth = {name: list(sites) for name, sites in self.collection_growth.items()}
        checked = self.checked_collections.copy()
        loop_values = self.loop_values
        reaches_check = self.visit(node)
        self.collection_growth = defaultdict(list, growth)
        self.checked_collections = checked
        self.loop_values = loop_values
        return reaches_check

    def known_value(self, node, seen=frozenset(), *, minimum=False):
        node = unwrap(node)
        number = integer(self.syntax, node)
        if number is not None:
            return number
        if node is not None and node.kind() in CALLS:
            call = self.syntax.call(node)
            if call.method == "len":
                receiver = call.receiver or (call.arguments[0] if len(call.arguments) == 1 else None)
                if self.syntax.language == "rust":
                    source = self.resolve(parts(receiver), self.syntax.absolute(node)) or receiver
                    if isinstance(text := literal(self.syntax, source), str):
                        return len(text.encode())
                return self.size(receiver, exact=not minimum, seen=seen)
        name = parts(node)
        if minimum and name in self.loop_ranges:
            return self.loop_ranges[name].start
        if name and name not in seen and (value := self.resolve(name, self.syntax.absolute(node))) is not None:
            return self.known_value(value, seen | {name}, minimum=minimum)
        operator, left, right = split_operands(node)
        if operator in {"+", "-", "*", "/", "//"}:
            first = self.known_value(left, seen, minimum=minimum and operator == "+")
            second = self.known_value(right, seen)
            if first is not None and second is not None:
                if operator in {"/", "//"}:
                    if second and (operator == "//" or self.syntax.language == "rust"):
                        quotient = abs(first) // abs(second)
                        return -quotient if (first < 0) != (second < 0) else quotient
                    return None
                return {"+": first + second, "-": first - second, "*": first * second}[operator]
        return None

    def resolve(self, name, before):
        entries = self.bindings.get(name, ())
        if entries:
            value = next((value for site, value in reversed(entries) if site < before), None)
            return value
        return self.file.constant(name, self.scope)

    def size(self, node, *, before=None, seen=frozenset(), exact=False):
        node = unwrap(node)
        if node is None:
            return None
        if before is None:
            before = self.syntax.absolute(node)
        kind = node.kind()
        elements = children(node)
        if kind in {"list", "tuple", "set", "dictionary", "array_expression", "tuple_expression"}:
            if any(child.kind() in {"list_splat", "dictionary_splat"} for child in elements):
                return None if exact else sum(
                    child.kind() not in {"list_splat", "dictionary_splat"} for child in elements)
            if kind in {"set", "dictionary"}:
                keys = [literal(self.syntax, child.field("key") if kind == "dictionary" else child)
                        for child in elements]
                if all(type(key) in {int, bool, str, bytes} for key in keys):
                    return len(set(keys))
                return None if exact else int(bool(elements))
            if any(child.kind() == ";" for child in node.children()):
                return self.known_value(elements[-1])
            return len(elements)
        value = literal(self.syntax, node)
        if isinstance(value, (str, bytes)):
            return len(value)
        if kind == "list_comprehension":
            clauses = children(node)[1:]
            if all(clause.kind() == "for_in_clause" for clause in clauses):
                sizes = [self.size(clause.field("right"), before=before, exact=exact, seen=seen)
                         for clause in clauses]
                return prod(sizes) if None not in sizes else None
        if kind in {"if_expression", "conditional_expression"}:
            if kind == "if_expression":
                branches = (node.field("consequence"), node.field("alternative"))
            else:
                branches = (elements[0], elements[-1])
            sizes = [self.size(branch, before=before, exact=exact, seen=seen) for branch in branches]
            if None not in sizes and (not exact or sizes[0] == sizes[1]):
                return min(sizes)
        if kind in {"else_clause", "block"} and elements:
            tail = elements[-1]
            if tail.kind() != "expression_statement":
                return self.size(tail, before=before, exact=exact, seen=seen)
        operator, left, right = split_operands(node)
        if self.syntax.language == "python" and operator == "-" and not exact:
            items = self.fixture_keys(left, before)
            removed = self.fixture_keys(right, before)
            if items is not None and removed is not None:
                return len(items - removed)
        if kind in {"reference_expression", "try_expression"} and elements:
            return self.size(elements[-1], before=before, exact=exact, seen=seen)
        if kind == "subscript":
            if self.syntax.language == "python" and (slc := next(
                    (part for part in children(node) if part.kind() == "slice"), None)) is not None:
                slice_parts = []
                current = None
                for token in slc.children():
                    if token.kind() == ":":
                        slice_parts.append(current)
                        current = None
                    elif token.is_named() and token.kind() not in COMMENTS:
                        current = token
                slice_parts.append(current)
                if len(slice_parts) <= 3:
                    start_node = slice_parts[0]
                    stop_node = slice_parts[1] if len(slice_parts) >= 2 else None
                    step_node = slice_parts[2] if len(slice_parts) == 3 else None
                    step_val = 1 if step_node is None else self.known_value(step_node)
                    length = self.size(node.field("value"), before=before, exact=exact, seen=seen)
                    if step_val == 1 and length is not None:
                        start_val = 0 if start_node is None else self.known_value(start_node)
                        if start_val is not None and start_val >= 0:
                            if stop_node is None:
                                return max(0, length - start_val)
                            stop_val = self.known_value(stop_node)
                            if stop_val is not None and stop_val >= 0:
                                return max(0, min(stop_val, length) - start_val)
            rows = self.fixture_rows(node.field("value"), before, seen)
            keys = self.loop_values.get(parts(node.field("subscript")), (node.field("subscript"),))
            wanted = [literal(self.syntax, key) for key in keys]
            if rows and all(isinstance(row, tuple) and len(row) == 2 for row in rows):
                sizes = [self.size(row[1], before=before, exact=exact, seen=seen) for row in rows
                         if UNKNOWN in wanted or literal(self.syntax, row[0]) in wanted]
                if sizes and None not in sizes and (not exact or len(set(sizes)) == 1):
                    return min(sizes)
        if kind == "index_expression" and len(elements) == 2:
            base, index = elements
            if index.kind() == "range_expression" and index.text().endswith(".."):
                terms = children(index)
                start = self.known_value(terms[0]) if len(terms) == 1 else 0 if not terms else None
                length = self.size(base, before=before, exact=exact, seen=seen)
                if start is not None and start >= 0 and length is not None:
                    return max(0, length - start)
        if kind == "range_expression":
            if len(elements) == 2:
                lower = self.known_value(elements[0])
                if lower is None and not exact:
                    lower = self.known_value(elements[0], minimum=True)
                ends = [lower, self.known_value(elements[1], minimum=not exact)]
                if ends[1] is None and not exact and len(name := parts(elements[1])) == 1 and name[0] in self.strategies:
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
            if self.syntax.language == "python" and method == "combinations" and len(args) == 2:
                n = self.size(args[0], before=before, exact=exact, seen=seen)
                r = self.known_value(args[1])
                if n is not None and type(r) is int and 0 <= r <= n:
                    return comb(n, r)
            if (self.syntax.language == "python" and call.receiver is not None and method == "split"
                    and not args and not exact and self._is_pil_image(call.receiver, before, seen)):
                return 1
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
                        return self.size(repeated, before=before, exact=exact, seen=seen)
            if call.receiver is None:
                if method in {"enumerate", "reversed", "iter", "list", "tuple", "sorted", "array", "repeat"} and args:
                    return self.size(args[0], before=before, exact=exact, seen=seen)
                if method in {"once", "repeat_with"} or method == "repeat" and self.syntax.language == "rust":
                    return None if exact and method != "once" else 1
                if method == "zip" and args:
                    inputs = [arg for arg in args if arg.kind() != "keyword_argument"]
                    sizes = [self.size(arg, before=before, exact=exact, seen=seen) for arg in inputs]
                    if sizes and None not in sizes:
                        return min(sizes)
                    if not exact and any(arg.text().replace(" ", "") == "strict=True" for arg in args):
                        return max((size for size in sizes if size is not None), default=None)
                if method in {"new", "empty", "default"} and call.name[:-1] in {
                        ("Vec",), ("VecDeque",), ("HashSet",), ("HashMap",), ("BTreeSet",),
                        ("BTreeMap",), ("std", "iter"), ("JoinSet",), ("tokio", "task", "JoinSet")}:
                    return 0
            if (self.syntax.language == "rust" and call.receiver is None or not args and (
                    call.receiver is None or call.name[:1] == ("self",))) and (
                    helper := self.file.fixture_function(call.name, self.scope)):
                file, target, function = helper
                identity = file.path, target
                if identity not in seen:
                    helper_flow = Flow(file, file.syntax, body_of(function), target)
                    returned = helper_flow.fixture_return(function)
                    if returned is not None:
                        return helper_flow.size(returned, seen=seen | {identity}, exact=exact)
            if call.receiver is not None:
                if method == "concat" and not args:
                    rows = self.fixture_rows(call.receiver, before, seen)
                    sizes = [self.size(row, before=before, exact=exact, seen=seen)
                             for row in rows if row is not None and not isinstance(row, tuple)]
                    if rows and len(sizes) == len(rows) and None not in sizes:
                        return sum(sizes)
                if method == "collect" and self.syntax.language == "rust" and not self.is_vector_collect(node):
                    count = self.size(call.receiver, before=before, seen=seen)
                    return None if count is None or exact else min(1, count)
                if method in {"bytes", "lines"}:
                    source = self.resolve(parts(call.receiver), before) or call.receiver
                    if isinstance(text := literal(self.syntax, source), str):
                        if method == "bytes":
                            return len(text.encode())
                        return text.count("\n") + int(bool(text) and not text.endswith("\n"))
                    return None
                if method == "cycle" and exact:
                    return None
                if method in {"iter", "iter_mut", "into_iter", "chars", "items", "keys", "values",
                              "enumerate", "rev", "map", "cloned", "copied", "collect", "as_slice", "to_vec",
                              "clone", "iter_rows", "by_ref", "cycle", "values_mut", "into_values"}:
                    return self.size(call.receiver, before=before, exact=exact, seen=seen)
                size = self.size(call.receiver, before=before, exact=exact, seen=seen)
                first = self.known_value(args[0]) if args else None
                if method == "take" and first is not None and call.receiver.kind() in CALLS:
                    base = self.syntax.call(call.receiver)
                    if base.method == "cycle" and self.size(base.receiver, before=before, seen=seen):
                        return first
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
                    other = self.size(args[0], before=before, exact=exact, seen=seen)
                    return None if exact and None in (size, other) else (size or 0) + (other or 0)
                if method == "zip" and args and size is not None:
                    other = self.size(args[0], before=before, exact=exact, seen=seen)
                    return min(size, other) if other is not None else None
        name = parts(node)
        if not name or name in seen:
            return None
        if self.reaching_facts is not None and len(name) == 1:
            joined, minimum = self.reaching_facts.path_minimum(name[0], before)
            if joined:
                return None if exact else minimum
        entries = self.bindings.get(name, ())
        if not entries and name in self.loop_values:
            sizes = [self.size(item, before=before, exact=exact, seen=seen | {name})
                     for item in self.loop_values[name]]
            if sizes and None not in sizes and (not exact or len(set(sizes)) == 1):
                return min(sizes)
        if not entries and not exact and len(name) == 1 and (strategy := self.strategies.get(name[0])) is not None:
            if strategy.kind() == "call_expression":
                call = self.syntax.call(strategy)
                if call.name[-2:] == ("collection", "vec") and len(call.arguments) == 2:
                    return self.strategy_minimum(call.arguments[1])
            elif self.syntax.language == "python" and strategy.kind() == "call":
                scall = self.syntax.call(strategy)
                if scall.method == "tuples":
                    return len(scall.arguments)
                if scall.method in {"lists", "sets", "dictionaries", "text", "binary"}:
                    for arg in scall.arguments:
                        if arg.kind() == "keyword_argument" and child_text(arg.field("name")) == "min_size":
                            return self.known_value(arg.field("value"))
                    return 0
            return None
        if not entries and not exact and len(name) == 1 and (pvals := self.param_values.get(name[0])):
            sizes = [self.size(item, before=before, exact=exact, seen=seen | {name}) for item in pvals]
            if sizes and None not in sizes:
                return min(sizes)
        binding_site = max((site for site, _ in entries if site < before), default=-1)
        if any(binding_site < start < before for start in self.writes[name]):
            return None
        value = self.resolve(name, before)
        if value is None:
            return None
        initial = self.size(value, before=before, exact=exact, seen=seen | {name})
        grown = sum(added for site, added in self.collection_growth[name] if binding_site < site < before)
        if self.syntax.language == "python" and not entries and not exact:
            grown += sum(self.file.setup_growth.get(name, ()))
        if grown and exact:
            return None
        checked = self.checked_collections.get(name)
        if not exact and checked is not None and binding_site < checked < before:
            return max(1, (initial or 0) + grown)
        return (initial or 0) + grown if grown else initial

    def is_vector_collect(self, node):
        if offset(node) in self.vector_collections:
            return True
        function = node.field("function")
        arguments = function.field("type_arguments") if function is not None else None
        return arguments is not None and parts(next(iter(children(arguments)), None))[-1:] == ("Vec",)

    def mark_vector(self, annotation, value):
        if value is not None and value.kind() == "call_expression" and (
                parts(annotation)[-1:] == ("Vec",) and self.syntax.call(value).method == "collect"):
            self.vector_collections.add(offset(value))

    def fixture_return(self, function):
        parameters = function.field("parameters")
        if parameters is not None:
            for parameter in children(parameters):
                pattern = parameter.field("pattern") or parameter
                for name in query(pattern, "identifier"):
                    self.bindings[parts(name)].append((-1, None))
        statements = list(direct_statements(self.body))
        if not statements:
            return None
        tail = statements[-1]
        if tail.kind() in {"return_statement", "return_expression"}:
            tail = next(iter(children(tail)), None)
        elif self.syntax.language == "python" or list(self.body.children())[-2].kind() == "expression_statement":
            return None
        for statement in statements[:-1]:
            target, value = binding(statement)
            if statement.kind() not in {"let_declaration", "assignment", "const_item"} or not target:
                return None
            self.bindings[target].append((self.syntax.absolute(statement), value))
            self.mark_vector(statement.field("type"), value)
        self.mark_vector(function.field("return_type"), tail)
        return tail

    def fixture_keys(self, node, before):
        seen = set()
        while node is not None:
            if node.kind() in {"list", "tuple", "set", "dictionary"}:
                keys = set()
                for item in children(node):
                    value = literal(self.syntax, item.field("key") if node.kind() == "dictionary" else item)
                    if not isinstance(value, (str, int)):
                        return None
                    keys.add(value)
                return keys
            if node.kind() == "call":
                call = self.syntax.call(node)
                if call.name == ("set",) and len(call.arguments) == 1:
                    node = call.arguments[0]
                    continue
            name = parts(node)
            if not name or name in seen:
                return None
            seen.add(name)
            node = self.resolve(name, before)
        return None

    def fixture_rows(self, node, before=None, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return ()
        if before is None:
            before = self.syntax.absolute(node)
        kind = node.kind()
        if kind in {"reference_expression", "try_expression"}:
            return self.fixture_rows(children(node)[-1], before, seen)
        if kind in {"list", "tuple", "array_expression"}:
            if not any(part.kind() in {";", "list_splat"} for part in node.children()):
                return children(node)
        if kind == "dictionary":
            pairs = [(item.field("key"), item.field("value")) for item in children(node)
                     if item.kind() == "pair"]
            keys = [literal(self.syntax, key) for key, _ in pairs]
            if len(pairs) == len(children(node)) and all(type(key) in {int, bool, str, bytes} for key in keys):
                return list(dict(zip(keys, pairs)).values())
        if kind in CALLS:
            call = self.syntax.call(node)
            if call.name == ("vec",):
                return call.arguments
            if call.receiver is not None and self.syntax.language == "python" and call.method in {
                    "items", "keys", "values"}:
                rows = self.fixture_rows(call.receiver, before, seen)
                if all(isinstance(row, tuple) for row in rows):
                    return rows if call.method == "items" else [row[call.method == "values"] for row in rows]
            if call.receiver is not None and call.method in {
                    "iter", "into_iter", "iter_mut", "cloned", "copied", "enumerate"}:
                rows = self.fixture_rows(call.receiver, before, seen)
                return [(None, row) for row in rows] if call.method == "enumerate" else rows
            if call.name == ("enumerate",) and len(call.arguments) == 1:
                return [(None, row) for row in self.fixture_rows(call.arguments[0], before, seen)]
            if (not call.arguments or self.syntax.language == "rust" and call.receiver is None) and (
                    helper := self.file.fixture_function(call.name, self.scope)):
                file, target, function = helper
                identity = file.path, target
                if identity not in seen:
                    flow = Flow(file, file.syntax, body_of(function), target)
                    return flow.fixture_rows(flow.fixture_return(function), seen=seen | {identity})
        name = parts(node)
        if name and name not in seen:
            if name in self.loop_values:
                return [row for value in self.loop_values[name]
                        for row in self.fixture_rows(value, before, seen | {name})]
            entries = self.bindings.get(name, ())
            site = max((site for site, _ in entries if site < before), default=-1)
            if not any(site < changed < before for changed in self.writes[name]):
                return self.fixture_rows(self.resolve(name, before), seen=seen | {name})
        return ()

    def project_rows(self, pattern, rows):
        if pattern is None or not rows:
            return {}
        if pattern.kind() in {"reference_pattern", "mut_pattern"}:
            return self.project_rows(children(pattern)[-1], rows)
        if pattern.kind() in {"tuple_pattern", "pattern_list", "list_pattern"}:
            targets = children(pattern)
            columns = []
            for row in rows:
                if isinstance(row, tuple):
                    values = row
                elif row is not None and row.kind() in {"tuple_expression", "tuple", "list"}:
                    values = children(row)
                else:
                    return {}
                if len(targets) != len(values):
                    return {}
                columns.append(values)
            return {key: values for target, values_in_column in zip(targets, zip(*columns))
                    for key, values in self.project_rows(target, values_in_column).items()}
        if pattern.kind() == "identifier" and pattern.text() != "_" and all(
                value is not None and not isinstance(value, tuple) for value in rows):
            return {parts(pattern): rows}
        return {}

    def loop_range(self, node):
        if node is None or node.kind() != "range_expression":
            return None
        bounds = children(node)
        if len(bounds) != 2:
            return None
        start, stop = [self.known_value(bound) for bound in bounds]
        if start is not None and stop is not None:
            return range(start, stop + any(part.kind() == "..=" for part in node.children()))
        return None

    def while_has_work(self, condition):
        if condition is None or condition.kind() != "let_condition":
            return False
        pattern = condition.field("pattern")
        value = condition.field("value")
        if pattern is None or value is None or not pattern.text().startswith("Some("):
            return False
        if value.kind() == "await_expression":
            value = children(value)[0]
        if value.kind() != "call_expression":
            return False
        call = self.syntax.call(value)
        return call.method == "join_next" and bool(self.size(call.receiver))

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
        condition = node.field("condition")
        primary = self.visit(condition)
        if self.reaching_facts is not None and tri_truth(self.syntax, condition) is UNKNOWN:
            site = self.syntax.absolute(node)
            decision = tri_truth(self.syntax, condition, lambda value: self.reaching_facts.branch_boolean(value, site))
            if decision is True:
                proved = self.isolated(node.field("consequence"))
                for arm in children(node):
                    if arm.kind() in {"elif_clause", "else_clause"}:
                        self.conditional(arm)
                return primary or proved
        if witness := self.branch_witness(condition):
            name, value = witness
            domains = self.loop_ranges
            self.loop_ranges = {**domains, name: range(value, value + 1)}
            checked = self.isolated(node.field("consequence"))
            self.loop_ranges = domains
            for arm in children(node):
                if arm.kind() == "else_clause":
                    self.conditional(arm)
            return primary or checked
        if primary:
            self.visit(node.field("consequence"))
            for arm in children(node):
                if arm.kind() in {"elif_clause", "else_clause"}:
                    self.visit(arm)
            return True
        branches = [self.isolated(node.field("consequence"))]
        alternatives = [child for child in children(node) if child.kind() in {"elif_clause", "else_clause"}]
        for arm in alternatives:
            if arm.kind() == "elif_clause":
                condition = self.isolated(arm.field("condition"))
                body = self.isolated(arm.field("consequence"))
                branches.append(condition or body)
            else:
                branches.append(self.isolated(arm.field("body") or next(iter(children(arm)), None)))
        exhaustive = bool(alternatives and alternatives[-1].kind() == "else_clause" and all(branches))
        if (not exhaustive and self.syntax.language == "python" and len(branches) == 2
                and alternatives and alternatives[-1].kind() == "else_clause"
                and self._parametrized_raises_dual(condition, branches, node.field("consequence"), alternatives[-1])):
            exhaustive = True
        if not exhaustive:
            for check in self.checks[before:]:
                check.guarded = True
        return exhaustive

    def _is_pil_image(self, node, before, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return False
        if node.kind() == "call":
            call = self.syntax.call(node)
            if call.method in {"new", "open"} and parts(call.receiver)[-1:] == ("Image",):
                return True
            if call.receiver is not None and call.method == "resize" and any(
                    imp == "Image" or imp[-1:] == ("Image",) for imp in self.file.imports.values()):
                return True
            if call.receiver is not None and call.method in {
                    "resize", "convert", "crop", "rotate", "transpose", "copy", "filter", "point"}:
                return self._is_pil_image(call.receiver, before, seen)
        name = parts(node)
        if name:
            entry = next(((site, value) for site, value in reversed(self.bindings.get(name, ()))
                          if site < before and value is not None and (site, name) not in seen), None)
            if entry is not None:
                return self._is_pil_image(entry[1], entry[0], seen | {(entry[0], name)})
            if (-1, name) not in seen and (value := self.file.constant(name, self.scope)) is not None:
                return self._is_pil_image(value, -1, seen | {(-1, name)})
        return False

    def _parametrized_raises_dual(self, condition, branches, consequence, else_clause):
        if branches.count(True) != 1 or branches.count(False) != 1:
            return False
        cond = unwrap(condition)
        if (neg := opposite(cond)) is not None:
            cond = neg
        name = parts(cond)
        if len(name) != 1 or name[0] not in self.param_values:
            return False
        bools = {literal(self.syntax, val) for val in self.param_values[name[0]]}
        if not ({True, False} <= {v for v in bools if type(v) is bool}):
            return False
        unchecked_arm = else_clause if branches[0] else consequence
        return unchecked_arm is not None and bool(query(unchecked_arm, "call"))

    def branch_witness(self, condition):
        op, left, right = split_operands(condition)
        if op not in COMPARISONS:
            return None
        name = parts(left)
        if name not in self.loop_ranges:
            name, right = parts(right), left
            op = REVERSE_COMPARISON.get(op, op)
        domain = self.loop_ranges.get(name)
        bound = self.known_value(right)
        if domain is not None and bound is not None:
            for candidate in (domain.start, domain.stop - 1, bound - 1, bound, bound + 1):
                if candidate in domain and COMPARISONS[op](candidate, bound):
                    return name, candidate
        return None

    def reaches_match_arm(self, domain, arms, covered):
        remaining = set()
        for arm, (checked, _) in zip(arms, covered):
            pattern = arm.field("pattern")
            if pattern is None or pattern.field("condition") is not None:
                return False
            if pattern.text() == "_":
                return checked and domain.stop - domain.start > len(remaining)
            number = integer(self.syntax, next(iter(children(pattern)), None))
            if number is None:
                return False
            if number in domain and number not in remaining:
                if checked:
                    return True
                remaining.add(number)
        return False

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
            covered.append((self.isolated(arm), self.checks[first:]))
        if self.syntax.language == "rust" and len(covered) > 1:
            if (domain := self.loop_ranges.get(parts(subject))) is not None and (
                    self.reaches_match_arm(domain, arms, covered)):
                return True
            if all(checked for checked, _ in covered):
                return True
            if sum(not checked for checked, _ in covered) == 1 and all(
                    not checked or checks and all(check.kind in FAILURES and not check.guarded for check in checks)
                    for checked, checks in covered):
                return True
            if self.strategies and any(check.predicate is not None and
                                       literal(self.syntax, check.predicate) is False
                                       for _, checks in covered for check in checks) and all(
                    checked or arm.field("pattern") is not None
                    and arm.field("pattern").text().replace(" ", "") == "(Err(_),Err(_))"
                    for arm, (checked, _) in zip(arms, covered)):
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
            if any(parent.kind() in boundaries and offset(parent) >= start for parent in jump.ancestors()):
                continue
            if jump.kind() == "return_expression":
                if self.checked_return(jump) or self.fixture_skip(jump):
                    continue
            if jump.kind() == "continue_statement" and self.prune_continued_rows(statement, jump):
                continue
            return True
        return False

    def checked_return(self, jump):
        statement = jump.parent()
        if statement.kind() == "expression_statement":
            statement = statement.parent()
        if statement.kind() != "block":
            return False
        for sibling in direct_statements(statement):
            if offset(sibling) >= offset(jump):
                break
            if sibling.kind() == "macro_invocation" and macro_name(sibling)[-1:] in {
                    (name,) for name in RUST_ASSERTIONS}:
                return True
        return False

    def fixture_skip(self, jump):
        if children(jump):
            return False
        block = next((parent for parent in jump.ancestors() if parent.kind() == "block"), None)
        if block is None or block == self.body:
            return False
        if any(parent.kind() in {"loop_expression", "for_expression", "while_expression"}
               for parent in block.ancestors() if not any(
                   scope.kind() in SCOPES for scope in parent.ancestors() if offset(scope) > offset(self.body))):
            return False
        owner = block.parent()
        if owner.kind() == "let_declaration" and owner.field("alternative") == block:
            value = owner.field("value")
            return value is not None and value.kind() == "call_expression" and (
                self.syntax.call(value).name in {("std", "env", "var"), ("env", "var"), ("writable_dir",)})
        if owner.kind() == "if_expression":
            condition = owner.field("condition")
            probes = [self.syntax.call(probe) for probe in query(condition, "call_expression")]
            if any(call.name[-2:] == ("Command", "new") for call in probes) and any(
                    call.method == "arg" and len(call.arguments) == 1 and
                    literal(self.syntax, call.arguments[0]) == "--version" for call in probes):
                return True
            for call in probes:
                if call.method.startswith(("should_run_", "require_")) or call.name in {
                        ("std", "env", "var"), ("env", "var"), ("libc", "geteuid"),
                        ("libc", "getuid"), ("libc", "mkfifo")}:
                    return True
        return any(any(isinstance(message := literal(self.syntax, arg), str) and "skip" in message.casefold()
                       for arg in self.syntax.macro_arguments(macro))
                   for macro in query(block, "macro_invocation")
                   if macro_name(macro)[-1:] in {("eprintln",), ("println",)})

    def prune_continued_rows(self, statement, jump):
        if statement.kind() == "expression_statement":
            statement = next(iter(children(statement)), statement)
        if statement.kind() != "if_statement" or statement.field("alternative") is not None:
            return False
        body = statement.field("consequence")
        if list(direct_statements(body)) != [jump]:
            return False
        op, left, right = split_operands(statement.field("condition"))
        if op not in {"==", "!="}:
            return False
        name = parts(left)
        rows = self.loop_values.get(name)
        against = literal(self.syntax, right)
        if not rows or against is UNKNOWN:
            return False
        values = [literal(self.syntax, row) for row in rows]
        if any(value is UNKNOWN for value in values):
            return False
        remaining = [row for row, value in zip(rows, values) if not COMPARISONS[op](value, against)]
        if remaining:
            self.loop_values = {**self.loop_values, name: remaining}
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
            known_boolean = None
            if self.reaching_facts is not None:
                site = self.syntax.absolute(node)
                known_boolean = lambda value: self.reaching_facts.branch_boolean(value, site)
            runs = (size is not None and size > 0 and not any(child.kind() == "async" for child in node.children())
                    or condition is not None and tri_truth(self.syntax, condition, known_boolean) is True
                    or kind == "loop_expression")
            runs |= self.while_has_work(condition)
            if self.syntax.language == "rust" and condition is not None and condition.kind() == "let_condition":
                value = condition.field("value")
                if value is not None and value.kind() == "await_expression":
                    value = children(value)[0]
                if value is not None and value.kind() == "call_expression":
                    call = self.syntax.call(value)
                    if call.method == "join_next" and (name := parts(call.receiver)):
                        self.writes[name].append(self.syntax.absolute(node))
            pattern = node.field("pattern") or node.field("left")
            self.loops.append((iterator, pattern, body, runs))
            outer_values = self.loop_values
            outer_ranges = self.loop_ranges
            if runs:
                self.loop_values = {**outer_values, **self.project_rows(pattern, self.fixture_rows(iterator))}
            if (domain := self.loop_range(iterator)) is not None and domain.start < domain.stop and (
                    len(name := parts(pattern)) == 1):
                self.loop_ranges = {**outer_ranges, name: domain}
            before = self.collection_growth.copy()
            self.collection_growth = defaultdict(list, {name: sites.copy() for name, sites in before.items()})
            checked = self.visit(body) if runs else self.conditional(body)
            if runs and size and size > 1:
                for name, sites in self.collection_growth.items():
                    old = len(before.get(name, ()))
                    sites[old:] = [(site, added * size) for site, added in sites[old:]]
            if not runs:
                self.collection_growth = defaultdict(list, before)
            self.loop_values = outer_values
            self.loop_ranges = outer_ranges
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
                if call.method in {"push", "append", "push_back", "push_front", "extend", "spawn"}:
                    added = (self.size(call.arguments[0]) or 0) if call.method == "extend" and call.arguments else 1
                    self.collection_growth[receiver].append((site, added))
            if self.syntax.language == "rust" and call.receiver is not None:
                owner = self.receiver_owner(call.receiver, site)
                if owner:
                    call = Call((*owner, call.method), call.arguments, call.receiver)
            if self.syntax.language == "python":
                qualified = self.file.python_import(call.name, self.scope)
                if qualified in {("pytest", "raises"), ("pytest", "warns"), ("pytest", "deprecated_call")}:
                    manager = node
                    while manager.parent().kind() in {"as_pattern", "parenthesized_expression"}:
                        manager = manager.parent()
                    recognized = outcome = len(call.arguments) >= 2 or manager.parent().kind() == "with_item"
                elif qualified == ("warnings", "catch_warnings"):
                    manager = node
                    while manager.parent().kind() in {"as_pattern", "parenthesized_expression"}:
                        manager = manager.parent()
                    in_with = manager.parent().kind() == "with_item"
                    is_error = any(
                        arg.kind() == "keyword_argument" and child_text(arg.field("name")) == "action"
                        and literal(self.syntax, arg.field("value")) == "error"
                        for arg in call.arguments
                    )
                    if not is_error and in_with and (with_stmt := manager.parent().parent().parent()) is not None:
                        is_error = any(
                            self.syntax.call(inner).method in {"simplefilter", "filterwarnings"}
                            and self.syntax.call(inner).arguments
                            and literal(self.syntax, self.syntax.call(inner).arguments[0]) == "error"
                            for inner in query(with_stmt, "call")
                        )
                    recognized = outcome = in_with and is_error
                elif qualified[:2] == ("unittest", "TestCase") and len(qualified) == 3:
                    recognized = outcome = lint.assertion(call.method) and bool(call.arguments)
                    if recognized:
                        call = Call(call.name, call.arguments[1:], call.receiver)
                elif qualified and (call.method in PYTHON_PREDICATES or call.method in {
                        "raises", "warns", "deprecated_call", "assertTrue", "assertFalse"}):
                    recognized = outcome = False
                else:
                    recognized, outcome = self.file.check_call(call, self.scope)
            else:
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
                    self.record_mutable_borrow(argument, site)
                    if argument.kind() == "closure_expression":
                        callback = body_of(argument)
                        if call.method in {"unwrap_or_else", "expect_err"} and callback is not None and any(
                                macro_name(inner)[-1:] in {(failure,) for failure in FAILURES}
                                for inner in query(callback, "macro_invocation")):
                            recognized = outcome = True
                        if callback is not None:
                            checked_callback |= self.visit(callback)
            if recognized:
                if call.macro and call.method in RUST_ASSERTIONS:
                    for argument in call.arguments:
                        for borrow in query(argument, "reference_expression"):
                            self.record_mutable_borrow(borrow, site)
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
        if kind in {"assignment", "augmented_assignment", "assignment_expression", "compound_assignment_expr"}:
            left = node.field("left")
            if left is not None and left.kind() in {"subscript", "index_expression"}:
                indexed = parts(left.field("value") or next(iter(children(left)), None))
                if indexed:
                    site = self.syntax.absolute(node)
                    self.writes[indexed].append(site)
                    self.alias_changes[indexed[0]].append(site)
        if target:
            self.mark_vector(node.field("type"), value)
            location = self.syntax.absolute(node)
            if len(target) > 1 or kind in {"augmented_assignment", "compound_assignment_expr"}:
                self.writes[target[:1]].append(location)
            if self.bindings.get(target) or len(target) > 1:
                self.alias_changes[target[0]].append(location)
                self.recorder_changes[target].append(location)
            self.bindings[target].append((location, value))
        elif kind in {"assignment", "let_declaration"}:
            pattern = node.field("left") or node.field("pattern")
            value = node.field("right") or node.field("value")
            if pattern is not None and value is not None and pattern.kind() in {
                    "pattern_list", "tuple_pattern", "list_pattern"} and value.kind() in {
                    "expression_list", "tuple", "list", "tuple_expression"}:
                targets, values = children(pattern), children(value)
                if len(targets) == len(values):
                    location = self.syntax.absolute(node)
                    shapes = [shape(v) for v in values] if self.syntax.language == "python" and len(values) > 1 else []
                    for entry, initial in zip(targets, values):
                        if target := parts(entry):
                            repeated_eval = (bool(shapes) and shapes.count(shape(initial)) > 1
                                             and (len(parts(initial)) >= 2 or bool(query(initial, *self.call_kinds))))
                            self.bindings[target].append((location, None if repeated_eval else initial))
                            self.recorder_changes[target].append(location)
        return any([self.visit(child) for child in children(node)])

    def record_mutable_borrow(self, node, site):
        if node.kind() != "reference_expression":
            return
        elements = children(node)
        if any(child.kind() == "mutable_specifier" for child in elements):
            if borrowed := parts(elements[-1]):
                self.alias_changes[borrowed[0]].append(site)
                self.writes[borrowed].append(site)

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
            if self.syntax.language == "python" and len(alias) >= 2:
                pure_checks = {c.location for c in self.checks
                               if c.kind == "assert" or c.kind.startswith(("assert", "raises", "warns", "deprecated"))}
                for call_node, call in self.calls:
                    call_site = self.syntax.absolute(call_node)
                    if site < call_site < before and call_site not in pure_checks and call.method not in {
                            "len", "bool", "str", "repr", "isinstance", "issubclass", "type", "id", "hash"}:
                        return name
            return (*alias, *name[1:])
        return name

    def recorder_path(self, node, before):
        node = unwrap(node)
        if node is not None and node.kind() == "reference_expression":
            node = children(node)[-1]
        path = parts(node)
        if not path:
            return ()
        root = path[:1]
        versions = [(site, value) for site, value in self.bindings.get(root, ()) if site < before]
        if len(versions) != 1 or versions[0][1] is None:
            return path
        start, value = versions[0]
        original = self.recorder_path(value, start)
        if not original:
            return path
        relevant = (root, original[:1], original)
        if any(start < site < before for key in relevant for site in self.recorder_changes[key]):
            return path
        if self.recorder_escapes is None:
            self.recorder_escapes = defaultdict(list)
            checked_sites = {check.location for check in self.checks}
            for call_node, call in self.calls:
                site = self.syntax.absolute(call_node)
                if call.macro or site in checked_sites or call.method in {"len", "bool", "str", "repr", "tuple", "list"}:
                    continue
                for argument in call.arguments:
                    value = argument.field("value") if argument.kind() == "keyword_argument" else argument
                    if value is not None and value.kind() == "reference_expression":
                        value = children(value)[-1]
                    if supplied := parts(value):
                        self.recorder_escapes[supplied].append(site)
        if any(start < site < before for key in relevant for site in self.recorder_escapes[key]):
            return path
        return (*original, *path[1:])

    def compares_itself(self, check, name, comments):
        operator, left, right = check.operator, check.left, check.right
        if operator not in {"==", "!=", "is", "is not"} or left is None or right is None:
            return False
        lhs = shape(left)
        rhs = shape(right)
        if lhs != rhs:
            alias = self.resolve_alias(left, check.location)
            return bool(alias) and alias == self.resolve_alias(right, check.location)
        if self.syntax.language == "python":
            if operator in {"!=", "is not"}:
                return not query(left, *self.call_kinds)
            lhs_node = unwrap(left)
            if operator in {"==", "is"} and lhs_node is not None and lhs_node.kind() == "call":
                lcall = self.syntax.call(lhs_node)
                if lcall.method[:1].isupper():
                    return False
        if operator not in {"==", "is"} or not query(left, *self.call_kinds):
            return True
        at = self.file.line_at(check.location)
        comment = " ".join(comments.get(position, "") for position in (at - 1, at))
        if any(lint.describes_distinct_inputs(text) for text in (name, comment)):
            return True
        if any(lint.suggests_repeatability(text) for text in (name, comment)):
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
        if lint.claims_outcome(name) and all(
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
        if check.kind in {"expect", "unwrap"} and check.source.kind() == "call_expression":
            receiver = self.syntax.call(check.source).receiver
            if receiver is not None and receiver.kind() == "call_expression":
                search = self.syntax.call(receiver)
                if search.method in {"find", "first", "last", "get"}:
                    yield search.receiver, True
        if subject is not None and subject.kind() in CALLS:
            call = self.syntax.call(subject)
            if call.method == "is_empty" and not call.arguments:
                yield call.receiver, not positive
            elif positive and call.method in {"any", "contains", "contains_key", "is_some"}:
                receiver = call.receiver
                if call.method == "is_some":
                    if receiver is None or receiver.kind() not in CALLS:
                        return
                    search = self.syntax.call(receiver)
                    if search.method not in {"find", "first", "last", "get"}:
                        return
                    receiver = search.receiver
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
            if known is UNKNOWN and (other.kind() in {"list", "tuple", "set", "dictionary",
                                                      "array_expression", "tuple_expression"}
                                     or other.kind() == "macro_invocation" and macro_name(other) == ("vec",)):
                known = self.size(other, before=check.location, exact=True)
            if operand is None or known is UNKNOWN or known is None:
                continue
            value = known
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
            site = self.syntax.absolute(node)
            receiver = self.recorder_path(call.receiver, site)
            if receiver in recorders and call.method in RECORDING:
                writes[receiver].append((site, node))
            for index, arg in enumerate(call.arguments):
                if arg.kind() == "keyword_argument":
                    slot = child_text(arg.field("name"))
                    arg = arg.field("value")
                else:
                    slot = index
                if arg is not None and arg.kind() == "reference_expression":
                    name = self.recorder_path(children(arg)[-1], site)
                    is_recorder = any(child.kind() == "mutable_specifier" for child in children(arg))
                else:
                    path = self.recorder_path(arg, site)
                    name = path if self.syntax.language == "rust" else path[:-1]
                    value = self.resolve(parts(arg), site) if self.syntax.language == "rust" else None
                    is_recorder = (value is not None and value.kind() == "reference_expression" and
                                   any(part.kind() == "mutable_specifier" for part in children(value))
                                   if self.syntax.language == "rust" else path[-1:] and path[-1] in RECORDING)
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
                if name := self.recorder_path(operand, check.location):
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
                if name not in methods and not self.is_read_in_checks(name) and not self.serialized(name):
                    callback_write = next((site for site, node in writes[name]
                                           if any(parent.kind() in {callback_kind, "function_definition"}
                                                  for parent in node.ancestors()
                                                  if offset(parent) > offset(self.body))), None)
                    if callback_write is not None:
                        yield self.file.line_at(callback_write)
        yield from self.unobserved_callback_writes(recorders)
        self.detector_absences(observations)

    def serialized(self, name):
        if self.syntax.language != "rust":
            return False
        for node, call in self.calls:
            if call.macro and call.name == ("serde_json", "json") and not any(
                    parent.kind() == "closure_expression" for parent in node.ancestors()):
                if any(parts(identifier) == name for identifier in query(node, "identifier")
                       if identifier.inside(kind="token_tree")):
                    return True
        return False

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

    def positive_sources(self, node, site):
        while node is not None:
            node = unwrap(node)
            if node.kind() == "reference_expression":
                node = children(node)[-1]
                continue
            yield node
            name = parts(node)
            if name:
                binding_site = max((start for start, _ in self.bindings.get(name, ()) if start < site), default=-1)
                if any(binding_site < changed < site for changed in self.writes[name]):
                    return
                value = self.resolve(name, site)
                if value is None or shape(value) == shape(node):
                    return
                node, site = value, binding_site
                continue
            if node.kind() in CALLS:
                call = self.syntax.call(node)
                if call.method in {"iter", "into_iter", "filter", "filter_map", "map", "collect",
                                   "enumerate", "copied", "cloned", "as_ref", "as_slice"}:
                    node = call.receiver
                    continue
            return

    def checked_detectors(self):
        for check in self.checks:
            if check.guarded:
                continue
            for operand, present in self.observations(check):
                if present and operand is not None:
                    for source in self.positive_sources(operand, check.location):
                        if producer := self.detector(source):
                            yield producer

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
            if present:
                for source in self.positive_sources(value, check.location):
                    present_values.add(shape(source))
                    if producer := self.detector(source):
                        self.file.detectors.append((producer, True, self.file.line_at(check.location)))
            else:
                candidates.append((value, check.location))
        reported = set()
        for value, site in candidates:
            if any(self.runtime_inventory(source) and shape(source) not in present_values
                   for source in self.sweep_sources(value)):
                self.file.detectors.append((("sweep", shape(value)), False, self.file.line_at(site)))
                continue
            producer = self.detector(value)
            if producer is not None and producer not in reported:
                self.file.detectors.append((producer, False, self.file.line_at(site)))
                reported.add(producer)

    def sweep_sources(self, node):
        if self.syntax.language == "python":
            node = self.unwrap_materializer(node)
            if node.kind() in {"list_comprehension", "set_comprehension", "dictionary_comprehension",
                               "generator_expression"}:
                for clause in children(node):
                    if clause.kind() == "for_in_clause":
                        source = clause.field("right")
                        yield self.resolve(parts(source), self.syntax.absolute(source)) or source
            return
        if node.kind() != "call_expression":
            return
        call = self.syntax.call(node)
        if call.method != "collect":
            return
        source = call.receiver
        filtered = False
        while source is not None and source.kind() == "call_expression":
            step = self.syntax.call(source)
            if step.method not in {"filter", "filter_map", "map", "flatten", "iter", "into_iter",
                                   "unwrap", "expect", "copied", "cloned", "enumerate"}:
                break
            filtered |= step.method in {"filter", "filter_map"}
            source = step.receiver
        if filtered and source is not None:
            yield self.resolve(parts(source), self.syntax.absolute(source)) or source

    def unwrap_materializer(self, node):
        while node is not None and node.kind() == "call":
            call = self.syntax.call(node)
            if call.receiver is not None or call.method not in {"list", "tuple", "set", "sorted"} or (
                    len(call.arguments) != 1):
                break
            node = call.arguments[0]
        return node

    def runtime_inventory(self, node):
        if self.syntax.language == "python":
            node = self.unwrap_materializer(node)
        while node is not None and node.kind() in CALLS:
            call = self.syntax.call(node)
            if self.syntax.language == "python":
                return call.method in {"listdir", "scandir", "glob", "iglob", "rglob", "iterdir"}
            if call.method in {"read_dir", "glob", "glob_with"} or call.name[-2:] == ("WalkDir", "new"):
                return True
            if call.method not in {"unwrap", "expect", "unwrap_or_default", "flatten", "map", "filter",
                                   "filter_map", "collect", "into_iter", "iter"}:
                return False
            node = call.receiver
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
        self.module_files = {path.resolve(): self}
        self.module_context = None
        self.functions = {}
        self.function_items = []
        self.bases = {}
        self.macros = {}
        self.constants = {}
        self.imports = {}
        self.glob_imports = defaultdict(list)
        self.detectors = []
        self.recorder_witnesses = set()
        self.recorder_absences = []
        self.helper_checks = {}
        self.pending_helpers = set()
        self.incomplete = []
        self.setup_growth = defaultdict(list)
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
            for names, node in self.function_items:
                if names[-1] in {"setup_module", "setup_class", "setup_method", "setUp", "setUpClass"}:
                    setup_body = body_of(node)
                    for stmt in children(setup_body) if setup_body is not None else ():
                        if stmt.kind() != "expression_statement":
                            continue
                        expr = next(iter(children(stmt)), None)
                        if expr is not None and expr.kind() == "call":
                            scall = self.syntax.call(expr)
                            if scall.receiver is not None and scall.method in {"append", "add"}:
                                if target := parts(scall.receiver):
                                    self.setup_growth[target].append(1)
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
            owner = namespace(node)
            kind = node.kind()
            if kind == "assignment" and any(parent.kind() in FUNCTIONS for parent in node.ancestors()):
                continue
            if kind == "macro_definition":
                self.macros[(*owner, function_name(node))] = node
            elif kind == "use_declaration":
                self.index_rust_import(node.field("argument"), owner)
            elif kind.startswith("import"):
                for child in node.field_children("name"):
                    name = child.field("name") or child
                    alias = child.field("alias")
                    if name is not None:
                        module = child_text(node.field("module_name"))
                        relative = len(module) - len(module.lstrip("."))
                        prefix = tuple(module.lstrip(".").split(".")) if module.lstrip(".") else ()
                        path = tuple(name.text().split("."))
                        key = (*owner, child_text(alias) or path[0])
                        if not module and alias is None:
                            path = path[:1]
                        self.imports[key] = (*("." for _ in range(relative)), *prefix, *path)
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
        elif kind == "use_wildcard":
            base = next(iter(children(node)), None)
            self.glob_imports[scope].append((*prefix, *parts(base)))
        elif kind == "use_as_clause":
            path = (*prefix, *parts(node.field("path")))
            alias = child_text(node.field("alias"))
            if path and alias:
                self.imports[(*scope, alias)] = path
        elif path := parts(node):
            if path == ("self",) and prefix:
                self.imports[(*scope, prefix[-1])] = prefix
            else:
                self.imports[(*scope, path[-1])] = (*prefix, *path)

    def constant(self, name, scope):
        definition = self.fixture_definition(name, scope, "constants")
        return definition[2] if definition else None

    def fixture_function(self, name, scope):
        if self.syntax.language == "python":
            target = self.resolve_function(name, scope)
            if target is not None:
                function = self.functions[target]
                decorator = function.parent()
                if decorator.kind() != "decorated_definition" or all(
                        child_text(next(iter(children(part)), None)) in {"staticmethod", "classmethod"}
                        for part in children(decorator) if part.kind() == "decorator"):
                    return self, target, function
        return self.fixture_definition(name, scope, "functions")

    def fixture_definition(self, name, scope, table, seen=frozenset()):
        if not name:
            return None
        identity = self.path, name, scope
        if identity in seen:
            return None
        seen = seen | {identity}
        definitions = getattr(self, table)
        rust = self.syntax.language == "rust"
        if rust and name[0] in {"self", "super", "crate"}:
            return self.rust_definition(name, scope, table, seen)
        for end in range(len(scope), -1, -1):
            owner = scope[:end]
            candidate = (*owner, *name)
            if candidate in definitions:
                return self, candidate, definitions[candidate]
            if not rust and name[:1] in {("self",), ("cls",)} and (*owner, *name[1:]) in definitions:
                candidate = (*owner, *name[1:])
                return self, candidate, definitions[candidate]
            if imported := self.imports.get((*owner, name[0])):
                path = (*imported, *name[1:])
                if rust:
                    return self.rust_definition(path, owner, table, seen)
                return self.python_definition(path, table, seen)
            if rust and owner in self.glob_imports:
                matches = [result for glob in self.glob_imports[owner]
                           if (result := self.rust_definition((*glob, *name), owner, table, seen))]
                if len(matches) == 1:
                    return matches[0]
        if not rust and len(name) == 2 and table == "constants":
            if (script := self.python_script(name[0])) is not None:
                return script.fixture_definition(name[1:], (), table, seen)
        return None

    def rust_definition(self, name, scope, table, seen):
        if name[0] == "crate":
            absolute = name[1:]
        elif name[0] in {"self", "super"}:
            module = self.rust_context()[1]
            parent = (*module, *scope)
            for part in name:
                if part == "super":
                    parent = parent[:-1]
                elif part != "self":
                    break
            while name and name[0] in {"self", "super"}:
                name = name[1:]
            absolute = (*parent, *name)
        elif name[0] == self.rust_context()[2]:
            absolute = name[1:]
        else:
            return None
        if not absolute:
            return None
        source_root, module, _ = self.rust_context()
        definitions = getattr(self, table)
        if absolute[:len(module)] == module and (local := absolute[len(module):]) in definitions:
            return self, local, definitions[local]
        if self.scan_root is None:
            return None
        for depth in range(len(absolute) - 1, -1, -1):
            stem = source_root.joinpath(*absolute[:depth])
            paths = ((stem.with_suffix(".rs"), stem / "mod.rs") if depth else
                     (stem / "lib.rs", stem / "main.rs"))
            available = [path for path in paths if path.is_file()]
            if len(available) == 1:
                file = self.load_module(available[0])
                if file is not None:
                    return file.fixture_definition(absolute[depth:], (), table, seen)
        return None

    def rust_context(self):
        if self.module_context is None:
            path = self.path.resolve()
            root = project_root(path, self.scan_root, {"Cargo.toml", ".git"})
            source = next((directory for directory in path.parents if directory.is_relative_to(root / "src")
                           and ((directory / "lib.rs").is_file() or (directory / "main.rs").is_file())),
                          root / "src")
            module = path.relative_to(source).with_suffix("").parts if path.is_relative_to(source) else ()
            if module and module[-1] in {"mod", "lib", "main"}:
                module = module[:-1]
            package = ""
            try:
                config = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
                package = config.get("lib", {}).get("name") or config.get("package", {}).get("name", "")
            except (OSError, ValueError):
                pass
            self.module_context = source, module, package.replace("-", "_") if isinstance(package, str) else ""
        return self.module_context

    def python_definition(self, name, table, seen):
        if self.scan_root is None or len(name) < 2:
            return None
        relative = sum(part == "." for part in name)
        path = self.path.resolve()
        root = project_root(path, self.scan_root, {".git", "pyproject.toml", "setup.py"})
        if relative >= len(path.parents):
            return None
        bases = (path.parents[relative - 1],) if relative else tuple(
            base for parent in path.parents if parent.is_relative_to(root) for base in (parent, parent / "src"))
        module, target = name[relative:-1], name[-1:]
        for base in bases:
            stem = base.joinpath(*module)
            for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
                if candidate.is_file() and (file := self.load_module(candidate)) is not None:
                    return file.fixture_definition(target, (), table, seen)
        return None

    def python_script(self, alias):
        if self.scan_root is None:
            return None

        def args(node, callee):
            if node is not None and node.kind() == "call":
                call = self.syntax.call(node)
                if call.name == tuple(callee.split(".")):
                    return call.arguments
            return []

        module = args(self.constants.get((alias,)), "importlib.util.module_from_spec")
        spec = args(self.constants.get(parts(module[0])), "importlib.util.spec_from_loader") if len(module) == 1 else []
        if len(spec) != 2:
            return None
        loader = parts(spec[1])
        source = args(self.constants.get(loader), "importlib.machinery.SourceFileLoader")
        if len(source) != 2:
            return None
        path = args(source[1], "str")
        if len(path) != 1:
            return None
        expression = self.constants.get(parts(path[0]))
        op, parent, filename = split_operands(expression)
        if op != "/" or parent.kind() != "subscript":
            return None
        resolve = parent.field("value")
        if resolve is None or not resolve.matches(pattern="Path(__file__).resolve().parents"):
            return None
        level = integer(self.syntax, parent.field("subscript"))
        name = literal(self.syntax, filename)
        if level is not None and 0 <= level < len(self.path.resolve().parents) and isinstance(name, str):
            if any(self.syntax.call(node).name == (*loader, "exec_module") and
                   [parts(arg) for arg in self.syntax.call(node).arguments] == [(alias,)]
                   for node in direct_statements(self.syntax.root) if node.kind() == "call"):
                return self.load_module(self.path.resolve().parents[level] / name)
        return None

    def load_module(self, candidate):
        candidate = candidate.resolve()
        if candidate == self.path.resolve():
            return self
        if candidate not in self.module_files:
            root = project_root(self.path, self.scan_root, {".git", "Cargo.toml", "pyproject.toml", "setup.py"})
            file = None
            if candidate.is_relative_to(root):
                try:
                    if self.syntax.language == "python":
                        with tokenize.open(candidate) as stream:
                            source = stream.read()
                    else:
                        source = candidate.read_text(encoding="utf-8")
                    file = File(source, self.syntax.language, candidate, self.scan_root)
                    file.module_files = self.module_files
                except (OSError, UnicodeError, ValueError, SyntaxError):
                    pass
            self.module_files[candidate] = file
        return self.module_files[candidate]

    def python_import(self, name, scope):
        if name:
            for depth in range(len(scope), -1, -1):
                if qualified := self.imports.get((*scope[:depth], name[0])):
                    return (*qualified, *name[1:])
        return ()

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
                self.syntax.language == "python" and call.receiver is not None
                and method in {"fnmatch_lines", "fnmatch_lines_random", "re_match_lines", "re_match_lines_random"}) or (
                self.syntax.language == "rust" and method.startswith("debug_assert")):
            return True, True
        return False, False

    def python_test_initial(self, fn):
        parent = fn.parent()
        if parent is None or parent.kind() != "decorated_definition":
            return ()
        strategies = []
        param_values = defaultdict(list)
        param_nodes = children(fn.field("parameters")) if fn.field("parameters") is not None else ()
        fn_params = []
        for entry in param_nodes:
            pname = child_text(entry.field("name")) if entry.kind().startswith("default") else child_text(entry)
            if pname and pname not in {"self", "cls"}:
                fn_params.append(pname.lstrip("*"))
        for dec in children(parent):
            if dec.kind() != "decorator":
                continue
            call_node = next((c for c in children(dec) if c.kind() == "call"), None)
            if call_node is None:
                continue
            dcall = self.syntax.call(call_node)
            if dcall.method == "given":
                pos = []
                for arg in dcall.arguments:
                    if arg.kind() == "keyword_argument":
                        kname = child_text(arg.field("name"))
                        if kname and arg.field("value") is not None:
                            strategies.append((kname, arg.field("value")))
                    else:
                        pos.append(arg)
                if pos and len(fn_params) >= len(pos):
                    for pname, strat in zip(fn_params[-len(pos):], pos):
                        strategies.append((pname, strat))
            elif dcall.method == "parametrize":
                argnames = None
                argvalues = None
                positional = [a for a in dcall.arguments if a.kind() != "keyword_argument"]
                for a in dcall.arguments:
                    if a.kind() == "keyword_argument":
                        k = child_text(a.field("name"))
                        if k == "argnames":
                            argnames = a.field("value")
                        elif k == "argvalues":
                            argvalues = a.field("value")
                if argnames is None and positional:
                    argnames = positional[0]
                if argvalues is None and len(positional) >= 2:
                    argvalues = positional[1]
                names_list = []
                if argnames is not None:
                    raw = literal(self.syntax, argnames)
                    if isinstance(raw, str):
                        names_list = [part.strip() for part in raw.split(",") if part.strip()]
                    elif isinstance(raw, (list, tuple)):
                        names_list = [str(part).strip() for part in raw if str(part).strip()]
                if names_list and argvalues is not None and argvalues.kind() in {"list", "tuple"}:
                    for row in children(argvalues):
                        if row.kind() == "call" and self.syntax.call(row).method == "param":
                            p_args = [a for a in self.syntax.call(row).arguments if a.kind() != "keyword_argument"]
                        elif len(names_list) == 1:
                            p_args = [row]
                        elif row.kind() in {"list", "tuple"}:
                            p_args = children(row)
                        else:
                            p_args = []
                        if len(p_args) == len(names_list):
                            for kname, pval in zip(names_list, p_args):
                                param_values[kname].append(pval)
        if param_values:
            strategies.append(("__params__", dict(param_values)))
        return tuple(strategies)

    def inventory(self):
        if self.syntax.language == "python":
            if any(part in {"example_scripts", "plugins_integration"} for part in self.path.parts):
                return
            for names, fn in self.function_items:
                if not names[-1].startswith("test") or any(
                        parent.kind() in FUNCTIONS for parent in fn.ancestors()):
                    continue
                parent = fn.parent()
                start = offset(parent) if parent.kind() == "decorated_definition" else offset(fn)
                initial = self.python_test_initial(fn)
                yield self.syntax, fn, Test(names[-1], ".".join((
                    *self.path.with_suffix("").parts, *names)), line(fn), self.line_at(start),
                    fn.range().end.line + 1, fn.range().start.column, qualified="::".join(names)), (), initial
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


def python_string(node):
    node = unwrap(node)
    if node is None:
        return False
    if node.kind() == "concatenated_string":
        return all(python_string(part) for part in children(node))
    return node.kind() == "string" and not node.children()[0].text().lower().startswith(
        ("b", "f", "rf", "t", "rt", "rb")
    )


def rust_string(node):
    node = unwrap(node)
    return node is not None and node.kind() in {"string_literal", "raw_string_literal"} and node.text().startswith(
        ('"', 'r"', "r#")
    )


def python_membership(call):
    args = {}
    for index, arg in enumerate(call.arguments):
        if arg.kind() == "keyword_argument":
            args[child_text(arg.field("name"))] = arg.field("value")
        else:
            args[index] = arg
    return args.get(0, args.get("member")), unwrap(args.get(1, args.get("container")))


def python_golden(call):
    args = call.arguments
    if call.name[-2:] == ("golden", "check") and len(args) >= 3:
        return args[2]
    if call.method in {"assert_golden", "assert_snapshot", "assert_match_snapshot", "check_snapshot"} or (
        call.method == "assert_match" and call.name[-2:-1] == ("snapshot",)
    ):
        return next(iter(args), None)
    return None


RUST_SNAPSHOTS = {"assert_snapshot", "assert_debug_snapshot", "assert_display_snapshot",
                  "assert_json_snapshot", "assert_yaml_snapshot", "assert_toml_snapshot",
                  "assert_ron_snapshot", "assert_csv_snapshot", "assert_binary_snapshot"}


def rust_snapshot(call, node):
    if call.arguments:
        return call.arguments[-1]
    tree = macro_tree(node)
    if tree is not None:
        tokens = tree.children()
        for index, token in enumerate(tokens):
            if token.kind() == "@" and index >= 2 and tokens[index - 1].kind() == ",":
                operand = tree.text()[1:offset(tokens[index - 1]) - offset(tree)]
                parsed = Syntax.parse("fn tq() { tq(" + operand + "); }", "rust")
                args = parsed.root.find(kind="arguments")
                if args is not None and not syntax_errors(parsed.root, missing=True):
                    return next(iter(reversed(children(args))), None)
    return None


def rust_subject(node):
    node = unwrap(node)
    while node is not None and node.kind() == "reference_expression":
        node = unwrap(node.field("value"))
    return node


def micro_snapshot(file, flow, test):
    membership = {"assertIn", "assertNotIn"}
    assertions = [check.source for check in flow.checks]
    assertions.extend(node for node, call in flow.calls if call.method in membership
                      or (python_golden(call) is not None if flow.syntax.language == "python"
                          else call.macro and call.method in RUST_SNAPSHOTS))
    reads = micro.Reads(assertions)
    fragments = []
    whole = []
    syntax = flow.syntax

    def binding_at(node, before):
        name = flow.recorder_path(node, before)
        positions = [site for length in range(1, len(name) + 1) for site, _ in flow.bindings.get(name[:length], ())
                     if site < before]
        positions.extend(site for site in flow.writes.get(name[:1], ()) if site < before)
        return max(positions, default=-1)

    def key(source, subject):
        before = syntax.absolute(source)
        identity = flow.recorder_path(subject, before) or shape(subject)
        return reads.key(source, identity, binding_at(subject, before), reread=micro.has_call(subject))

    def line_count(source, joined):
        call = syntax.call(joined) if joined is not None and joined.kind() == "call" else None
        if not call or call.method != "join" or len(call.arguments) != 1 or child_text(call.receiver) not in {
            r'"\n"', r"'\n'",
        }:
            return False
        lines = call.arguments[0]
        site = syntax.absolute(source)
        version = binding_at(lines, site)
        if not parts(lines) or binding_at(lines, syntax.absolute(joined)) != version:
            return False
        for check in flow.checks:
            if check.operator != "==" or micro.branch_path(source) != micro.branch_path(check.source):
                continue
            for operand, expected in ((check.left, check.right), (check.right, check.left)):
                if type(literal(syntax, expected)) is not int or operand is None:
                    continue
                counted = query(operand, "identifier")
                if any(parts(node) == parts(lines) for node in counted) and any(
                    syntax.call(node).method in {"len", "sum"} for node in query(operand, "call")
                ) and binding_at(lines, check.location) == version:
                    return True
        return False

    def fixture_echo(initializer, needle, before):
        if syntax.language != "rust" or initializer is None or not isinstance(needle, str) or len(needle) < 2:
            return False
        inputs = [syntax.call(node).arguments[0] for node in query(initializer, "call_expression")
                  if syntax.call(node).method == "read_to_string" and syntax.call(node).arguments]
        return any(call.method == "write" and call.name[-2:] == ("fs", "write")
                   and len(call.arguments) == 2 and syntax.absolute(node) < before
                   and any(shape(rust_subject(path)) == shape(rust_subject(call.arguments[0])) for path in inputs)
                   and rust_string(call.arguments[1])
                   and (needle in value if isinstance(value := literal(syntax, call.arguments[1]), str) else
                        any(needle in part.text() for part in children(call.arguments[1])
                            if part.kind() == "string_content"))
                   for node, call in flow.calls)

    def add(source, subject, needle, negative):
        if subject is None:
            return
        before = syntax.absolute(source)
        name = parts(subject)
        initial = flow.resolve(name, before) if name else subject
        diagnostic = child_text(initial)
        if initial is not None:
            receiver = next((part for part in query(initial, "attribute" if syntax.language == "python" else "field_expression")
                             if parts(part)[-1:] == ("stdout",)), None)
            if receiver is not None and (origin := flow.resolve(parts(receiver)[:-1], before)) is not None:
                diagnostic += child_text(origin)
        if micro.excluded(child_text(subject), diagnostic, test.name) or (
                initial is not None and initial.kind() in {"list", "tuple", "set", "dictionary", "array_expression",
                                                            "list_comprehension", "set_comprehension",
                                                            "dictionary_comprehension", "tuple_expression"}):
            return
        producer = syntax.call(initial).method if initial is not None and initial.kind() in CALLS else ""
        compact = bool(micro.words(producer) & {"diff", "hook"})
        value = literal(syntax, needle)
        fragments.append(micro.Fragment(
            (file.line_at(before), source.range().start.column), child_text(subject), key(source, subject),
            value if isinstance(value, str) else child_text(needle), negative, compact,
            syntax.language == "python" and line_count(source, initial), not negative and fixture_echo(initial, value, before),
        ))

    if syntax.language == "python":
        for check in flow.checks:
            if check.operator in {"in", "not in"} and python_string(check.left):
                add(check.source, unwrap(check.right), check.left, check.operator == "not in")
            elif check.operator == "==":
                whole.extend((check.source, subject) for subject in (check.left, check.right) if subject is not None)
        for node, call in flow.calls:
            if call.method in membership:
                needle, subject = python_membership(call)
                if python_string(needle):
                    add(node, subject, needle, call.method == "assertNotIn")
            elif (subject := python_golden(call)) is not None:
                whole.append((node, subject))
    else:
        for check in flow.checks:
            if check.kind in RUST_ASSERTIONS and check.operator == "==":
                whole.extend((check.source, rust_subject(subject)) for subject in (check.left, check.right)
                             if subject is not None)
            elif check.kind in RUST_ASSERTIONS and check.predicate is not None:
                negated = opposite(unwrap(check.predicate))
                predicate = unwrap(negated or check.predicate)
                if predicate.kind() == "call_expression":
                    call = syntax.call(predicate)
                    if (call.method == "contains" and call.receiver is not None and
                            len(call.arguments) == 1 and rust_string(call.arguments[0])):
                        add(check.source, rust_subject(call.receiver), call.arguments[0], negated is not None)
        for node, call in flow.calls:
            if call.macro and call.method in RUST_SNAPSHOTS and (subject := rust_snapshot(call, node)) is not None:
                whole.append((node, rust_subject(subject)))
    micro.report(test, fragments, whole, key)


def no_crash_test(flow, test, comments, missing):
    syntax, body = flow.syntax, flow.body
    python = syntax.language == "python"
    helpers = lint.PYTHON_NO_RAISE if python else lint.RUST_NO_PANIC
    if flow.checks and all(check.kind in helpers and not check.guarded for check in flow.checks):
        return True
    if not missing:
        return False
    statements = list(direct_statements(body))
    description = ""
    if python and statements and python_string(statements[0]):
        description = statements[0].text()
    if not lint.claims_completion(test, comments, description=description):
        return False
    if python and any(statement.kind() not in {"assignment", "call", "await"}
                      and not python_string(statement) for statement in statements):
        return False
    if not python and any(call.method in {"catch_unwind", "unwrap_or", "unwrap_or_else", "unwrap_or_default"}
                          for _, call in flow.calls):
        return False
    for statement in statements:
        candidate = unwrap(binding(statement)[1] if not python and statement.kind() == "let_declaration"
                           else statement)
        if candidate is not None and candidate.kind() in {"await", "await_expression"}:
            candidate = unwrap(next(iter(children(candidate)), None))
        if candidate is not None and candidate.kind() in CALLS:
            return True
    return False


def parse(source, path, *, scan_root=None, collection_controls=None, inventory=False, observe=None):
    from .ast_grep_catches import swallowed_assertions
    from .ast_grep_origins import (
        ReachingFacts, different_python_products, oracle_pins, pinned_oracle, same_product, stage_helpers,
    )
    from . import ast_grep_literals as literals
    from .ast_grep_languages import ECMASCRIPT_LANGUAGES, JVM_LANGUAGES

    if path.suffix in ECMASCRIPT_LANGUAGES:
        from .ast_grep_ts import parse as parse_ecmascript

        return parse_ecmascript(source, path, ECMASCRIPT_LANGUAGES[path.suffix],
                                scan_root=scan_root, inventory=inventory, observe=observe)
    if path.suffix == ".dart":
        from .ast_grep_dart import parse as parse_dart

        return parse_dart(source, path, inventory=inventory, observe=observe)
    if path.suffix in JVM_LANGUAGES:
        from .ast_grep_jvm import parse as parse_jvm

        return parse_jvm(source, path, JVM_LANGUAGES[path.suffix], scan_root=scan_root,
                         collection_controls=collection_controls, inventory=inventory, observe=observe)
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
    control_helpers = set()
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
        kinds = ("call", "assert_statement") if syntax.language == "python" else flow.call_kinds
        sites = query(flow.body, *kinds)
        flow.reaching_facts = ReachingFacts(
            syntax, flow.body, literal, helper=stage_helpers(file, flow.scope),
            captures=(syntax.absolute(site) for site in sites),
        )
        guaranteed = flow.visit(flow.body)
        test.issues.extend((site, "swallowed-assertion") for site in
                           swallowed_assertions(flow.body, syntax.language, (check.source for check in flow.checks)))
        should_panic = ("should_panic",) in attrs
        ignored = ("ignore",) in attrs
        ignored_diagnostic = ignored and any(
            attribute_name(attribute) == ("ignore",) and "diagnostic" in attribute.text().casefold()
            for attribute in attributes(fn)
        )
        ignored_tool = ignored and all(
            check.kind == "expect" or ignored_diagnostic and check.kind in FAILURES | {"unwrap"}
            for check in flow.checks
        )
        test.guarded = any(check.reachability for check in flow.checks) and not (
            guaranteed or should_panic or ignored_tool)
        if not (should_panic or ignored):
            missing = not flow.verifies_outcome(test.name)
            lint.record_completion(test, missing, no_crash_test(flow, test, comments, missing))
        pins = oracle_pins(flow.reaching_facts, flow.checks) if syntax.language in {"python", "rust"} else set()
        for check in flow.checks:
            check_line = file.line_at(check.location)
            if check.truth(lambda node: flow.reaching_facts.branch_boolean(node, check.location)) is True:
                test.issues.append((check_line, "const-truth"))
            compares_itself = flow.compares_itself(check, test.name, line_comments)
            if check.operator == "==" and not compares_itself and (
                not pins or not pinned_oracle(flow.reaching_facts, check, pins)
            ) and (
                same_product(flow.reaching_facts, check)
                or syntax.language == "python" and different_python_products(file, flow, check)
            ):
                test.issues.append((check_line, "self-derived-expected"))
            if compares_itself:
                test.issues.append((check_line, "self-compare"))
        if file.syntax.language == "rust":
            from .ast_grep_rust_oracles import Oracles

            test.issues.extend((file.line_at(site), "self-derived-expected") for site in Oracles(file, flow).issues(pins))
        if not ignored:
            control_helpers.update(target for _, call in flow.calls if not call.macro
                                   and (target := file.resolve_function(call.name, flow.scope)) is not None)
        test.issues.extend((site, "absence-no-control") for site in flow.recorder_evidence())
        test.issues.extend((site, "nondistinguishing-literal")
                           for site in literals.issues(file, syntax, flow, flow.body, test.name))
        if not ignored:
            micro_snapshot(file, flow, test)
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
    missing = {key for key, present, _, _ in observations if not present and key not in witnessed}
    if file.syntax.language == "rust" and missing:
        names = {key[0] for key in missing if key[0] != "sweep"}
        for target in control_helpers:
            function = file.functions[target]
            body = body_of(function)
            if names and any(identifier.text() in names for identifier in query(body, "identifier")):
                helper = Flow(file, file.syntax, body, target)
                helper.visit(body)
                witnessed.update(helper.checked_detectors())
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
