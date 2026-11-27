"""Discover package:test and flutter_test callbacks through ast-grep."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib

from . import lint
from .ast_grep import Syntax, line, location, offset, query, syntax_errors
from .ast_grep_lint import COMPARISONS
from .ast_grep_dart_syntax import (
    COLLECTIONS, between, calls, collection_entries, comments, contents, final_call, identifier,
    kind, named, names, plain_literal, register_grammar, signature, split, string, tokens, unwrap,
)
from .source import Test


FRAMEWORKS = {"package:test/test.dart", "package:flutter_test/flutter_test.dart",
              "package:test_core/test_core.dart"}
REGISTRATIONS = {"test", "testWidgets", "group"}
SCOPES = {"source_file", "block", "function_expression", "for_statement"}
BOOLEAN = {"isTrue": True, "isFalse": False}
CHANGING_COLLECTION = {"add", "addAll", "insert", "insertAll", "putIfAbsent"}


def expression_operator(nodes):
    nodes = unwrap(nodes)
    if len(nodes) == 1:
        node = nodes[0]
        if node.kind() in {"equality_expression", "relational_expression", "logical_and_expression",
                           "logical_or_expression", "if_null_expression"}:
            return split(tokens(node), COMPARISONS.keys() | {"&&", "||", "??"})
    return "", (), ()


def negate(nodes):
    nodes = unwrap(nodes)
    if kind(nodes) == "unary_expression":
        nodes = contents(nodes[0])
        if nodes and nodes[0].kind() == "negate_operator":
            return nodes[1:]
    return ()


def comparison(operator, first, second):
    if lint.UNKNOWN in (first, second):
        return lint.UNKNOWN
    numeric = type(first) in {int, float} and type(second) in {int, float}
    if operator == "is":
        return type(first) is type(second) and first == second
    if operator in {"==", "!="}:
        equal = (numeric or type(first) is type(second)) and first == second
        return equal if operator == "==" else not equal
    return COMPARISONS[operator](first, second) if numeric and operator in COMPARISONS else lint.UNKNOWN


def constant(nodes):
    value = plain_literal(nodes)
    if value is not lint.UNKNOWN:
        return value
    nodes = unwrap(nodes)
    if kind(nodes) == "unary_expression":
        children = tokens(nodes[0])
        if children and children[0].text() == "-":
            value = constant(named(children[1:]))
            if type(value) in {int, float}:
                return -value
    if inverse := negate(nodes):
        value = constant(inverse)
        return not value if type(value) is bool else lint.UNKNOWN
    operator, left, right = expression_operator(nodes)
    if operator in COMPARISONS:
        return comparison(operator, constant(left), constant(right))
    if operator in {"&&", "||"}:
        first, second = constant(left), constant(right)
        decisive = operator == "||"
        if first is decisive or second is decisive:
            return decisive
        if type(first) is type(second) is bool:
            return not decisive
    return lint.UNKNOWN


def collection_size(nodes, *, exact=False):
    nodes = unwrap(nodes)
    if kind(nodes) not in COLLECTIONS:
        return None
    entries = tuple(collection_entries(nodes[0]))
    literal_entries = tuple(entry for entry in entries
                            if entry[0].kind() not in {"if_element", "for_element", "spread_element"})
    if not exact:
        return len(literal_entries) if kind(nodes) == "list_literal" else int(bool(literal_entries))
    if not entries or kind(nodes) == "list_literal" and len(literal_entries) == len(entries):
        return len(entries)
    return None


@dataclass
class Check:
    node: object
    actual: tuple = ()
    expected: tuple = ()
    operator: str = ""
    predicate: tuple = ()
    truth: object = lint.UNKNOWN
    matcher: str = ""
    matcher_args: tuple = ()
    guarded: bool = False

    def constant_success(self):
        if self.predicate:
            return constant(self.predicate) is self.truth and self.truth is not lint.UNKNOWN
        return comparison(self.operator, constant(self.actual), constant(self.expected)) is True


@dataclass(frozen=True)
class Registration:
    call: object
    title: str
    callback: object
    ignored: bool


@dataclass(frozen=True)
class Binding:
    node: object
    scope: object
    initializer: tuple
    final: bool


def is_within(node, owner):
    return offset(owner) <= offset(node) < owner.range().end.index


class File:
    def __init__(self, syntax):
        self.syntax = syntax
        self.frameworks = set()
        self.functions = defaultdict(list)
        self.bindings = defaultdict(list)
        for entry in query(syntax.root, "import_specification"):
            uri = entry.find(kind="string_literal")
            if uri is not None and string(uri) in FRAMEWORKS:
                prefix = between(tokens(entry), "as")
                self.frameworks.add(identifier(prefix[0]) if prefix else "")
        for definition in query(syntax.root, "function_signature"):
            body = definition.next()
            if body is not None and body.kind() == "function_body":
                name = next((identifier(node) for node in reversed(contents(definition))
                             if node.kind() == "identifier"), "")
                if name:
                    self.functions[name].append((self.scope(definition), body))
        for node in query(syntax.root, "initialized_variable_definition", "initialized_identifier"):
            self.index_variable(node)
        for parameter in query(syntax.root, "formal_parameter"):
            target = next((child for child in reversed(contents(parameter)) if child.kind() == "identifier"), None)
            if target is not None:
                scope = next((owner for owner in parameter.ancestors() if owner.kind() == "function_expression"), None)
                if scope is None:
                    definition = next((owner for owner in parameter.ancestors()
                                       if owner.kind() == "function_signature"), None)
                    scope = definition.next() if definition is not None else None
                if scope is not None:
                    self.bindings[target.text()].append(Binding(target, scope, (), False))

    def scope(self, node):
        return next((owner for owner in node.ancestors() if owner.kind() in SCOPES), self.syntax.root)

    def index_variable(self, node):
        elements = tokens(node)
        initial = named(between(elements, "=", ","))
        before = elements[:next((i for i, child in enumerate(elements) if child.kind() in {"=", ","}),
                                len(elements))]
        target = next((child for child in reversed(before) if child.kind() == "identifier"), None)
        if target is not None:
            immutable = any(child.kind() in {"final", "const", "final_builtin", "const_builtin"} for child in before)
            self.bindings[target.text()].append(Binding(target, self.scope(node), initial, immutable))

    def binding(self, nodes):
        path = names(nodes)
        if not path:
            return None
        site = nodes[0]
        visible = (entry for entry in self.bindings[path[0]]
                   if offset(entry.node) < offset(site) and is_within(site, entry.scope))
        return max(visible, key=lambda entry: (offset(entry.scope), offset(entry.node)), default=None)

    def callback(self, nodes):
        nodes = unwrap(nodes)
        if kind(nodes) == "function_expression":
            return next((child for child in contents(nodes[0]) if child.kind() == "function_expression_body"), None)
        path = names(nodes)
        if len(path) != 1:
            return None
        binding = self.binding(nodes)
        if binding:
            if kind(binding.initializer) == "function_expression" and binding.final:
                return self.callback(binding.initializer)
            return None
        visible = [(scope, body) for scope, body in self.functions[path[0]] if is_within(nodes[0], scope)]
        return max(visible, key=lambda entry: offset(entry[0]))[1] if visible else None

    def framework_name(self, path):
        if len(path) == 1:
            return path[0]
        if len(path) == 2 and path[0] in self.frameworks:
            return path[1]
        return ""

    def registration(self, call):
        if self.framework_name(call.name) not in REGISTRATIONS or len(call.args) < 2:
            return None
        if len(call.name) == 1 and (self.binding((call.node,)) or self.functions[call.name[0]]):
            return None
        title_nodes, target = call.args[:2]
        title = string(title_nodes[0], display=True) if kind(title_nodes) == "string_literal" else None
        if title is None:
            title = self.syntax.source[offset(title_nodes[0]):title_nodes[-1].range().end.index]
        if not title:
            title = "<unnamed>"
        skip = constant(call.keywords.get("skip", ()))
        return Registration(call, title, self.callback(target), skip is True or type(skip) is str)

    def inventory(self):
        registrations = []
        seen = set()
        owners = {}
        for arguments in query(self.syntax.root, "arguments"):
            selector = arguments.parent().parent()
            if selector.kind() != "selector":
                continue
            container = selector.parent()
            site = offset(container), container.range().end.index
            if site in seen:
                continue
            seen.add(site)
            for call in calls(contents(container)):
                if entry := self.registration(call):
                    registrations.append(entry)
                    if entry.callback is not None and kind(call.args[1]) == "function_expression":
                        owners[offset(entry.callback)] = entry
        for entry in registrations:
            if entry.call.method == "group":
                continue
            enclosing = [owners[offset(node)] for node in entry.call.node.ancestors()
                         if node.kind() == "function_expression_body" and offset(node) in owners]
            qualified = "::".join([*(item.title for item in reversed(enclosing)), entry.title])
            span = (entry.callback or entry.call.node).range()
            columns = span.start.column, span.end.column
            test = Test(entry.title, entry.title, line(entry.call.node), span.start.line + 1,
                        span.end.line + 1, columns[0], qualified=qualified, span_columns=columns)
            yield test, entry.callback, entry.ignored or any(item.ignored for item in enclosing)

    def check(self, call):
        method = self.framework_name(call.name)
        if method in {"expect", "expectLater"} and len(call.args) >= 2:
            actual, target = call.args[:2]
            check = Check(call.node, actual, target)
            matcher_call = final_call(target)
            matcher_name = self.framework_name(matcher_call.name if matcher_call else names(target))
            args = matcher_call.args if matcher_call else ()
            check.matcher, check.matcher_args = matcher_name, args
            if matcher_name in BOOLEAN:
                check.predicate = actual
                check.truth = BOOLEAN[matcher_name]
                operator, left, right = expression_operator(actual)
                if operator in {"==", "!="}:
                    check.operator = operator if check.truth else {"==": "!=", "!=": "=="}[operator]
                    check.actual, check.expected = left, right
            elif matcher_name in {"equals", "same", "isNot"} and args:
                nested = final_call(args[0])
                if matcher_name != "isNot" or not nested or nested.method == "equals":
                    check.operator = {"isNot": "!=", "same": "is", "equals": "=="}[matcher_name]
                    check.expected = nested.args[0] if matcher_name == "isNot" and nested and nested.args else args[0]
            elif constant(target) is not lint.UNKNOWN or signature(actual) == signature(target):
                check.operator = "=="
            return check
        if method == "fail":
            return Check(call.node)
        return None

    def identity(self, nodes):
        path = names(nodes)
        binding = self.binding(nodes)
        if binding is None:
            return path or None
        if binding.final:
            source = self.binding(binding.initializer)
            if names(binding.initializer) and source is not None and source.final:
                return (*self.identity(binding.initializer), *path[1:])
        return offset(binding.node), *path[1:]

    def recorder(self, nodes, seen=frozenset()):
        path = names(nodes)
        if not path:
            return None
        binding = self.binding(nodes)
        if binding is None:
            return None
        site = offset(binding.node)
        if site in seen:
            return None
        if binding.final and names(binding.initializer):
            alias = self.recorder(binding.initializer, seen | {site})
            if alias:
                return (*alias, *path[1:])
        if collection_size(binding.initializer, exact=True) == 0:
            return site, *path[1:]
        return None

    def observation(self, check):
        actual, matcher = check.actual, check.matcher
        if check.predicate:
            actual = check.predicate
            inverted = negate(actual)
            predicate = names(inverted or actual)
            if predicate[-1:] in {("isEmpty",), ("isNotEmpty",)}:
                present = (predicate[-1] == "isNotEmpty") is check.truth
                return self.recorder(unwrap(inverted or actual)[:-1]), not present if inverted else present
            actual = check.actual
        if matcher in {"isEmpty", "isNotEmpty"}:
            return self.recorder(actual), matcher == "isNotEmpty"
        if matcher == "hasLength" and check.matcher_args:
            length = constant(check.matcher_args[0])
            if type(length) is int and length >= 0:
                return self.recorder(actual), length > 0
        if not check.operator:
            return None, None
        for operand, expected in ((actual, check.expected), (check.expected, actual)):
            path = names(operand)
            if path[-1:] == ("length",):
                length = constant(expected)
                key = self.recorder(unwrap(operand)[:-1])
                if type(length) is int and length >= 0 and (check.operator == "==" or length == 0):
                    return key, (length > 0) == (check.operator == "==")
            elif (size := collection_size(expected, exact=True)) is not None:
                return self.recorder(operand), (size > 0) == (check.operator == "==") if (
                    check.operator == "==" or size == 0) else None
        return None, None


def string_needle(nodes):
    nodes = unwrap(nodes)
    return kind(nodes) == "string_literal" and nodes[0].find(kind="template_substitution") is None


def subject_text(nodes):
    return "".join(node.text() for node in nodes)


def micro_snapshot(file, test, checks):
    fragments = []
    whole = []
    for check in checks:
        matcher, args = check.matcher, check.matcher_args
        if matcher == "isNot" and args:
            nested = final_call(args[0])
            if nested:
                matcher, args = file.framework_name(nested.name), nested.args
        if matcher == "contains" and args and string_needle(args[0]):
            fragments.append((location(check.node), check.actual))
        elif check.operator in {"==", "is"} and matcher != "contains":
            whole.extend((check.actual, check.expected))
        elif matcher in {"matchesGoldenFile", "matchesReferenceImage"} and check.matcher == matcher:
            whole.append(check.actual)
    lint.micro_snapshot(test, fragments, whole, key=signature, display=subject_text)


def parse(source, path, *, inventory=False, observe=None):
    from .ast_grep_dart_flow import Flow

    register_grammar()
    syntax = Syntax.parse(source, "dart")
    file = File(syntax)
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    tests = []
    candidates = file.inventory() if file.frameworks or path.name.endswith("_test.dart") else ()
    for test, callback, ignored in candidates:
        tests.append(test)
        if ignored:
            continue
        flow = Flow(file, callback)
        incomplete = flow.run()
        micro_snapshot(file, test, flow.checks)
        test.checks = len(flow.checks)
        if not flow.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(flow.checks and incomplete)
        if inventory and callback is not None:
            test.body_hash = hashlib.sha256(repr(signature((callback,))).encode()).hexdigest()
        witnesses, absences = set(), []
        for check in flow.checks:
            site = line(check.node)
            if check.constant_success():
                test.issues.append((site, "const-truth"))
            if flow.compares_itself(check, test.name, line_comments):
                test.issues.append((site, "self-compare"))
            key, present = file.observation(check)
            if key is None:
                continue
            if present is True and not check.guarded:
                witnesses.add(key)
            elif present is False:
                absences.append((key, site))
        test.issues.extend((site, "absence-no-control") for key, site in absences
                           if key in flow.recorders and key not in witnesses)
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
