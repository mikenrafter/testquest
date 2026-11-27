"""Discover package:test and flutter_test callbacks through ast-grep."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib

from . import lint
from .ast_grep import Syntax, line, location, offset, query, syntax_errors
from .ast_grep_lint import COMPARISONS
from .ast_grep_dart_imports import Modules, imports
from .ast_grep_dart_syntax import (
    COLLECTIONS, between, calls, collection_entries, comments, contents, final_call, identifier,
    kind, named, names, plain_literal, register_grammar, signature, split, string, tokens, unwrap,
)
from .source import Test


FRAMEWORKS = {"package:test/test.dart", "package:flutter_test/flutter_test.dart",
              "package:test_core/test_core.dart"}
MOCKITO = {"verify", "verifyInOrder", "verifyNoMoreInteractions", "verifyZeroInteractions"}
REGISTRATIONS = {"test", "testWidgets", "group"}
SCOPES = {"source_file", "block", "function_expression", "for_statement", "class_body"}
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
    file: object = None

    def constant_success(self):
        if self.predicate:
            return constant(self.predicate) is self.truth and self.truth is not lint.UNKNOWN
        return comparison(self.operator, constant(self.actual), constant(self.expected)) is True


@dataclass(frozen=True)
class Registration:
    call: object
    title: str
    callback: object
    span: object
    ignored: bool


@dataclass(frozen=True)
class Binding:
    node: object
    scope: object
    initializer: tuple
    final: bool
    const: bool = False


@dataclass(frozen=True)
class Callback:
    file: object
    body: object
    supplied: dict

    @property
    def key(self):
        return self.file, offset(self.body)

    def bind(self, arguments, keywords):
        owner = self.body.parent() if self.body.kind() == "function_expression_body" else self.body.prev()
        if owner is not None and owner.kind() == "method_signature":
            owner = next(iter(contents(owner)), None)
        parameters = next((node for node in contents(owner) if node.kind() == "formal_parameter_list"), None)
        supplied = dict(self.supplied)
        positional = iter(arguments)
        for parameter in contents(parameters):
            if parameter.kind() == "formal_parameter":
                target = parameter_name(parameter)
                argument = next(positional, None)
                if target is not None and argument is not None:
                    supplied[offset(target)] = argument
            elif parameter.kind() == "optional_formal_parameters":
                named_parameters = any(child.kind() == "{" for child in tokens(parameter))
                for formal in contents(parameter):
                    if formal.kind() != "formal_parameter" or (target := parameter_name(formal)) is None:
                        continue
                    argument = keywords.get(target.text()) if named_parameters else next(positional, None)
                    if argument is not None:
                        supplied[offset(target)] = argument
        return Callback(self.file, self.body, supplied)


def parameter_name(parameter):
    return next((child for child in reversed(contents(parameter)) if child.kind() == "identifier"), None)


def returned_expression(body):
    block = next((child for child in contents(body) if child.kind() == "block"), None)
    if block is None:
        return contents(body) if any(child.kind() == "=>" for child in tokens(body)) else ()
    returns = [node for node in query(block, "return_statement")
               if next((owner for owner in node.ancestors()
                        if owner.kind() in {"function_expression_body", "function_body"}), None) == body]
    if len(returns) == 1 and returns[0].parent() == block:
        return contents(returns[0])
    return ()


def is_within(node, owner):
    return offset(owner) <= offset(node) < owner.range().end.index


class File:
    def __init__(self, syntax, path, modules=None):
        self.syntax = syntax
        self.path = path
        self.modules = modules or Modules(path)
        self.imports = tuple(imports(syntax.root))
        self.frameworks = {entry.prefix for entry in self.imports if entry.uri in FRAMEWORKS}
        self.functions = defaultdict(list)
        self.methods = {}
        self.bindings = defaultdict(list)
        self.collection_writes = {}
        self.enums = {identifier(node.field("name")) for node in query(syntax.root, "enum_declaration")
                      if node.field("name") is not None}
        self.equality_classes = {identifier(node.field("name")) for node in query(syntax.root, "class_declaration")
                                 if node.field("name") is not None and any(
                                     child.text() == "==" for child in query(node, "binary_operator")
                                     if child.parent().kind() == "operator_signature")}
        for definition in query(syntax.root, "function_signature"):
            name = next((identifier(node) for node in reversed(contents(definition))
                         if node.kind() == "identifier"), "")
            if not name:
                continue
            holder = definition.parent() if definition.parent().kind() == "method_signature" else definition
            body = holder.next()
            if body is None or body.kind() != "function_body":
                continue
            scope = self.scope(definition)
            if scope.kind() == "class_body":
                target = scope.parent().field("name")
                if target is not None:
                    self.methods[identifier(target), name] = body
            else:
                self.functions[name].append((scope, body))
        for node in query(syntax.root, "initialized_variable_definition", "initialized_identifier", "static_final_declaration"):
            self.index_variable(node)
        for parameter in query(syntax.root, "formal_parameter"):
            target = parameter_name(parameter)
            if target is not None:
                scope = next((owner for owner in parameter.ancestors() if owner.kind() == "function_expression"), None)
                if scope is None:
                    definition = next((owner for owner in parameter.ancestors()
                                       if owner.kind() == "function_signature"), None)
                    if definition is not None:
                        holder = definition.parent() if definition.parent().kind() == "method_signature" else definition
                        scope = holder.next()
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
            if node.kind() == "static_final_declaration":
                parent = node.parent()
                siblings = tokens(parent.parent())
                index = siblings.index(parent)
                preceding = siblings[:index]
                start = next((i + 1 for i, item in reversed(list(enumerate(preceding))) if item.kind() == ";"), 0)
                before = preceding[start:]
            qualifiers = {child.text() for child in before}
            immutable = bool(qualifiers & {"final", "const"})
            self.bindings[target.text()].append(Binding(target, self.scope(node), initial, immutable,
                                                       "const" in qualifiers))

    def binding(self, nodes):
        path = names(nodes)
        if not path:
            return None
        site = nodes[0]
        visible = (entry for entry in self.bindings[path[0]] if is_within(site, entry.scope)
                   and (offset(entry.node) < offset(site)
                        or entry.const and entry.scope.kind() == "source_file"
                        and kind(unwrap(entry.initializer)) in COLLECTIONS))
        return max(visible, key=lambda entry: (offset(entry.scope), offset(entry.node)), default=None)

    def callback(self, nodes, supplied=None, active=frozenset()):
        nodes = unwrap(nodes)
        supplied = supplied or {}
        if kind(nodes) == "function_expression":
            body = next((child for child in contents(nodes[0]) if child.kind() == "function_expression_body"), None)
            return Callback(self, body, supplied) if body is not None else None
        if (call := final_call(nodes)) is not None:
            factory = self.resolve_call(call, supplied, active)
            if factory is None:
                return None
            return factory.file.callback(returned_expression(factory.body), factory.supplied, active | {factory.key})
        return self.resolve_name(nodes, supplied)

    def resolve_call(self, call, supplied, active=frozenset()):
        if not call.name:
            return None
        target = self.resolve_name((call.node, *call.receiver[1:]), supplied, path=call.name)
        if target is None or target.key in active:
            return None
        arguments = [self.callback(arg, supplied, active | {target.key}) for arg in call.args]
        keywords = {name: self.callback(arg, supplied, active | {target.key}) for name, arg in call.keywords.items()}
        return target.bind(arguments, keywords)

    def resolve_name(self, nodes, supplied, *, path=None):
        path = path or names(nodes)
        if not path:
            return None
        site = nodes[0]
        binding = self.binding((site,))
        if len(path) == 1:
            if binding:
                if offset(binding.node) in supplied:
                    return supplied[offset(binding.node)]
                if binding.final and kind(binding.initializer) == "function_expression":
                    return self.callback(binding.initializer, supplied)
                return None
            visible = [(scope, body) for scope, body in self.functions.get(path[0], ()) if is_within(site, scope)]
            if visible:
                return Callback(self, max(visible, key=lambda entry: offset(entry[0]))[1], supplied)
        if len(path) == 2:
            constructor = final_call(binding.initializer) if binding and binding.final else None
            owner = constructor.name if constructor is not None else path[:1] if binding is None else ()
            if len(owner) == 1 and (body := self.methods.get((owner[0], path[-1]))) is not None:
                return Callback(self, body, supplied)
            if binding:
                if constructor is not None:
                    return self.imported_callback((*constructor.name, path[-1]))
                return None
        return self.imported_callback(path)

    def imported_modules(self, path):
        if len(path) > 3:
            return
        for entry in self.imports:
            if entry.prefix and entry.prefix != path[0]:
                continue
            parts = path[bool(entry.prefix):]
            if parts and entry.exposes(parts[0]) and not parts[0].startswith("_"):
                module = self.modules.load(self.path, entry.uri)
                if module is not None:
                    yield module, parts

    def imported_callback(self, path):
        targets = []
        for module, parts in self.imported_modules(path):
            if len(parts) == 2:
                body = module.methods.get(parts)
            elif len(parts) == 1:
                body = next((body for scope, body in module.functions.get(parts[0], ())
                             if scope.kind() == "source_file"), None)
            else:
                continue
            if body is not None:
                targets.append(Callback(module, body, {}))
        return targets[0] if len(targets) == 1 else None

    def is_enum(self, path, site):
        if self.binding((site,)):
            return False
        return (len(path) == 1 and path[0] in self.enums or any(
            len(parts) == 1 and parts[0] in module.enums for module, parts in self.imported_modules(path)))

    def library_name(self, path, package):
        if len(path) not in {1, 2}:
            return ""
        prefix = path[0] if len(path) == 2 else ""
        if any(entry.uri.startswith(package) and entry.prefix == prefix and entry.exposes(path[-1])
               for entry in self.imports):
            return path[-1]
        return ""

    def unchanged(self, binding):
        site = offset(binding.node)
        if site not in self.collection_writes:
            self.collection_writes[site] = any(self.mutates_collection(use)
                for use in query(binding.scope, "identifier")
                if use.text() == binding.node.text() and self.binding((use,)) == binding)
        return not self.collection_writes[site]

    def mutates_collection(self, use):
        parent = use.parent()
        if parent.kind() == "assignable_expression":
            operation = parent.parent()
            if operation.kind() in {"assignment_expression", "postfix_expression", "unary_expression"}:
                return operation.kind() == "assignment_expression" or any(
                    child.text() in {"++", "--"} for child in tokens(operation))
        nodes = contents(parent)
        if names(nodes) == (use.text(),) and parent.kind() in {
                "argument", "named_argument", "initialized_variable_definition", "static_final_declaration"}:
            return True
        return any(call.method in CHANGING_COLLECTION | {"clear", "remove", "removeAt", "removeWhere",
                                                         "removeLast", "removeRange", "retainWhere",
                                                         "replaceRange", "setAll", "setRange", "shuffle", "sort"}
                   and call.receiver and call.receiver[0] == use for call in calls(nodes))

    def overloads_equality(self, nodes, title):
        binding = self.binding(nodes)
        constructor = final_call(binding.initializer) if binding else None
        return (constructor is not None and (constructor.name and constructor.name[0] in self.equality_classes
                or any(word in title.casefold() for word in ("reflexiv", "comparator", "comparison operators"))))

    def builds_value(self, nodes, seen=frozenset()):
        nodes = unwrap(nodes)
        if kind(nodes) in COLLECTIONS:
            return True
        if nodes and nodes[-1].text() == ".hashCode":
            return any(calls(nodes[:-1]))
        call = final_call(nodes)
        if call is None:
            return False
        if call.name and call.name[0][0].isupper() and not self.binding((call.node,)):
            return True
        callback = self.resolve_call(call, {})
        return callback is not None and callback.key not in seen and callback.file.builds_value(
            returned_expression(callback.body), seen | {callback.key})

    def fixed_number(self, nodes):
        binding = self.binding(nodes)
        if binding and binding.final and len(names(nodes)) == 1:
            return self.fixed_number(binding.initializer)
        return constant(nodes)

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
        callback = self.callback(target)
        span = callback.body if callback is not None and callback.file is self and final_call(target) is None else None
        if span is None:
            span = next((body for node in target for body in query(node, "function_expression_body")), call.node)
        return Registration(call, title, callback, span, skip is True or type(skip) is str)

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
                        owners[offset(entry.span)] = entry
        for entry in registrations:
            if entry.call.method == "group":
                continue
            enclosing = [owners[offset(node)] for node in entry.call.node.ancestors()
                         if node.kind() == "function_expression_body" and offset(node) in owners]
            qualified = "::".join([*(item.title for item in reversed(enclosing)), entry.title])
            span = entry.span.range()
            columns = span.start.column, span.end.column
            test = Test(entry.title, entry.title, line(entry.call.node), span.start.line + 1,
                        span.end.line + 1, columns[0], qualified=qualified, span_columns=columns)
            yield test, entry.callback, entry.span, entry.ignored or any(item.ignored for item in enclosing)

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
            elif kind(unwrap(target)) in COLLECTIONS:
                check.operator = "=="
            return check
        if method == "fail":
            return Check(call.node)
        if self.library_name(call.name, "package:mockito/") in MOCKITO:
            return Check(call.node)
        if method in {"expectAsync", "expectAsync0", "expectAsync1", "expectAsync2", "expectAsync3",
                      "expectAsync4", "expectAsync5", "expectAsync6"}:
            count = self.fixed_number(call.keywords.get("count", ()))
            if "count" not in call.keywords or type(count) is int and count >= 0:
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


def parse(source, path, *, inventory=False, observe=None):
    from .ast_grep_dart_absence import Absence
    from .ast_grep_dart_flow import Flow

    from .ast_grep_dart_micro import micro_snapshot

    register_grammar()
    syntax = Syntax.parse(source, "dart")
    file = File(syntax, path)
    file.modules.loaded[path.resolve()] = file
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    tests = []
    absence = Absence(file)
    candidates = file.inventory() if file.frameworks or path.name.endswith("_test.dart") else ()
    for test, callback, span, ignored in candidates:
        tests.append(test)
        if ignored or callback is None:
            continue
        flow = Flow(file, callback)
        incomplete = flow.run()
        micro_snapshot(file, test, flow.checks)
        test.checks = len(flow.checks)
        if not flow.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(flow.checks and incomplete)
        if inventory:
            body = (span,)
            if span.kind() == "identifier":
                body = contents(span.parent())
            test.body_hash = hashlib.sha256(repr(signature(body)).encode()).hexdigest()
        for check in flow.checks:
            if check.file is not file:
                continue
            site = line(check.node)
            if check.constant_success():
                test.issues.append((site, "const-truth"))
            if flow.compares_itself(check, test.name, line_comments):
                test.issues.append((site, "self-compare"))
        absence.collect(test, flow)
    absence.finish()
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
