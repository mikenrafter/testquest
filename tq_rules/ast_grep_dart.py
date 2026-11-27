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
    kind, member, named, names, plain_literal, register_grammar, signature, split, string, tokens, unwrap,
)
from .source import Test


FRAMEWORKS = {"package:test/test.dart", "package:flutter_test/flutter_test.dart",
              "package:test_core/test_core.dart"}
MOCKITO = {"verify", "verifyInOrder", "verifyNoMoreInteractions", "verifyZeroInteractions", "verifyNever"}
REGISTRATIONS = {"test", "testWidgets", "group"}
ASYNC_EXPECTATIONS = {"expectAsync", *(f"expectAsync{arity}" for arity in range(7))}
FUNCTION_BODIES = {"function_expression_body", "function_body"}
SCOPES = {"source_file", "block", "function_expression", "for_statement", "class_body", "extension_body"}
BOOLEAN = {"isTrue": True, "isFalse": False}
CHANGING_COLLECTION = {"add", "addAll", "insert", "insertAll", "putIfAbsent"}
MUTATING_COLLECTION = CHANGING_COLLECTION | {
    "clear", "remove", "removeAt", "removeWhere", "removeLast", "removeRange", "retainWhere",
    "replaceRange", "setAll", "setRange", "shuffle", "sort",
}


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
    iterable: tuple = ()


@dataclass(frozen=True)
class Callback:
    file: object
    body: object
    supplied: dict
    supplied_values: dict = None

    def __post_init__(self):
        if self.supplied_values is None:
            object.__setattr__(self, "supplied_values", {})

    @property
    def key(self):
        return self.file, offset(self.body)

    def bind(self, arguments, keywords, *, raw_args=(), raw_keywords=None, caller_file=None):
        owner = self.body.parent() if self.body.kind() == "function_expression_body" else self.body.prev()
        if owner is not None and owner.kind() == "method_signature":
            owner = next(iter(contents(owner)), None)
        parameters = next((node for node in contents(owner) if node.kind() == "formal_parameter_list"), None)
        supplied = dict(self.supplied)
        supplied_values = dict(self.supplied_values)
        raw_keywords = raw_keywords or {}
        positional = iter(arguments)
        raw_positional = iter(raw_args)
        for parameter in contents(parameters):
            if parameter.kind() == "formal_parameter":
                target = parameter_name(parameter)
                argument = next(positional, None)
                raw_argument = next(raw_positional, None)
                if target is not None:
                    if argument is not None:
                        supplied[offset(target)] = argument
                    if raw_argument is not None and caller_file is not None:
                        supplied_values[self.file, offset(target)] = (caller_file, raw_argument)
            elif parameter.kind() == "optional_formal_parameters":
                named_parameters = any(child.kind() == "{" for child in tokens(parameter))
                for formal in contents(parameter):
                    if formal.kind() != "formal_parameter" or (target := parameter_name(formal)) is None:
                        continue
                    argument = keywords.get(target.text()) if named_parameters else next(positional, None)
                    raw_argument = raw_keywords.get(target.text()) if named_parameters else next(raw_positional, None)
                    if argument is not None:
                        supplied[offset(target)] = argument
                    if raw_argument is not None and caller_file is not None:
                        supplied_values[self.file, offset(target)] = (caller_file, raw_argument)
        return Callback(self.file, self.body, supplied, supplied_values)


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
        self.exports = tuple(imports(syntax.root, exports=True))
        self.frameworks = {entry.prefix for entry in self.imports if entry.uri in FRAMEWORKS}
        self.functions = defaultdict(list)
        self.getters = {}
        self.methods = {}
        self.extension_methods = defaultdict(list)
        self.bindings = defaultdict(list)
        self.collection_writes = {}
        self._unchanged_active = set()
        self.static_constants = {}
        self.constructors = {}
        self.final_fields = defaultdict(set)
        self.enum_members = {}
        for enum in query(syntax.root, "enum_declaration"):
            if (name := enum.field("name")) is not None:
                self.enum_members[name.text()] = tuple(
                    child.text() for entry in query(enum, "enum_constant")
                    for child in contents(entry)[:1] if child.kind() == "identifier")
        for constructor in query(syntax.root, "constant_constructor_signature", "constructor_signature"):
            declaration = next((owner for owner in constructor.ancestors()
                                if owner.kind() == "class_declaration"), None)
            params = next((node for node in contents(constructor) if node.kind() == "formal_parameter_list"), None)
            if declaration is None or params is None or len(tuple(query(declaration, "class_body"))) != 1:
                continue
            keys = tuple(child.text() for child in contents(constructor) if child.kind() == "identifier")
            signature = constructor.parent()
            if not keys or query(signature, "initializers") or constructor.next() is not None:
                continue
            self.constructors[keys] = params
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
            elif scope.kind() == "extension_body":
                extension = scope.parent()
                ext_name = next((identifier(child) for child in contents(extension)
                                 if child.kind() == "identifier"), "")
                on_type = next((identifier(child) for child in contents(extension)
                                if child.kind() == "type_identifier"), "")
                self.extension_methods[name].append((ext_name, on_type, body))
            else:
                self.functions[name].append((scope, body))
        for definition in query(syntax.root, "getter_signature"):
            if any(token.text() == "set" for token in tokens(definition)):
                continue
            name = next((identifier(node) for node in reversed(contents(definition))
                         if node.kind() == "identifier"), "")
            if not name:
                continue
            holder = definition.parent() if definition.parent().kind() == "method_signature" else definition
            body = holder.next()
            if body is not None and body.kind() == "function_body" and self.scope(definition).kind() == "source_file":
                self.getters[name] = body
        for node in query(syntax.root, "initialized_variable_definition", "initialized_identifier", "static_final_declaration"):
            self.index_variable(node)
        for declaration in query(syntax.root, "pattern_variable_declaration"):
            pattern = next((child for child in contents(declaration) if child.kind() == "record_pattern"), None)
            value = next((child for child in contents(declaration) if child.kind() == "record_literal"), None)
            targets, values = contents(pattern), contents(value)
            if not targets or len(targets) != len(values) or any(entry.kind() != "record_field" for entry in values):
                continue
            for entry, initial in zip(targets, values):
                parts = contents(entry)
                if len(parts) == 1 and identifier(parts[0]) and not any(
                        token.kind() == ":" for token in tokens(initial)):
                    self.bindings[parts[0].text()].append(
                        Binding(parts[0], self.scope(declaration), contents(initial),
                                any(token.kind() == "final" for token in tokens(declaration))))
        for parameter in query(syntax.root, "formal_parameter"):
            target = parameter_name(parameter)
            definition = next((owner for owner in parameter.ancestors()
                               if owner.kind() in {"function_expression", "function_signature"}), None)
            if target is None or definition is None:
                continue
            if definition.kind() == "function_expression":
                scope = definition
            else:
                holder = definition.parent() if definition.parent().kind() == "method_signature" else definition
                scope = holder.next()
            if scope is not None and scope.kind() in {"function_expression", "function_body"}:
                self.bindings[target.text()].append(Binding(target, scope, (), False))
        for loop in query(syntax.root, "for_statement", "for_element"):
            parts = next((child for child in contents(loop) if child.kind() == "for_loop_parts"), loop)
            items = between(tokens(parts), "(", ")")
            target = next((child for child in reversed(items[:next(
                (i for i, child in enumerate(items) if child.kind() == "in"), 0)])
                           if child.kind() == "identifier"), None)
            if target is not None:
                self.bindings[target.text()].append(Binding(target, loop, (), False,
                                                            iterable=named(between(items, "in"))))

    def scope(self, node):
        return next((owner for owner in node.ancestors() if owner.kind() in SCOPES), self.syntax.root)

    def index_variable(self, node):
        elements = tokens(node)
        initial = tuple(child for child in between(elements, "=", ",")
                        if child.is_named() or child.kind() == "this")
        before = elements[:next((i for i, child in enumerate(elements) if child.kind() in {"=", ","}),
                                len(elements))]
        target = next((child for child in reversed(before) if child.kind() == "identifier"), None)
        if target is not None:
            if node.kind() in {"static_final_declaration", "initialized_identifier"}:
                holder = node.parent()
                declaration = holder.parent()
                if declaration.kind() in {"declaration", "source_file"}:
                    siblings = tokens(declaration)
                    index = siblings.index(holder)
                    start = next((i + 1 for i in range(index - 1, -1, -1)
                                  if siblings[i].kind() in {";", "class_declaration", "function_body"}), 0)
                    before = siblings[start:index]
            qualifiers = {child.text() for child in before}
            scope = self.scope(node)
            immutable = bool(qualifiers & {"final", "const"})
            if scope.kind() == "class_body":
                owner = scope.parent().field("name")
                if owner is not None:
                    if "const" in qualifiers and "static" in qualifiers:
                        self.static_constants[owner.text(), target.text()] = initial
                        return
                    if immutable and "static" not in qualifiers:
                        self.final_fields[owner.text()].add(target.text())
            self.bindings[target.text()].append(Binding(target, scope, initial, immutable, "const" in qualifiers))

    def binding(self, nodes):
        path = names(nodes)
        if not path:
            return None
        site = nodes[0]
        visible = (entry for entry in self.bindings[path[0]] if is_within(site, entry.scope)
                   and (not entry.iterable or offset(site) >= entry.iterable[-1].range().end.index)
                   and (offset(entry.node) < offset(site)
                        or (entry.const or entry.final and self.unchanged(entry))
                        and entry.scope.kind() == "source_file"
                        and (kind(unwrap(entry.initializer)) in COLLECTIONS
                             or type(constant(entry.initializer)) in {int, float})))
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

    def resolve_call(self, call, supplied, active=frozenset(), supplied_values=None):
        target = None
        if call.name:
            target = self.resolve_name((call.node, *call.receiver[1:]), supplied, path=call.name,
                                       supplied_values=supplied_values)
            if target is None:
                getter = self.resolve_getter(call.name)
                if getter is not None and getter.key not in active:
                    target = getter.file.callback(returned_expression(getter.body), getter.supplied, active | {getter.key})
        if target is None and call.receiver:
            target = self.resolve_extension(call)
        if target is None or target.key in active:
            return None
        arguments = [self.callback(arg, supplied, active | {target.key}) for arg in call.args]
        keywords = {name: self.callback(arg, supplied, active | {target.key}) for name, arg in call.keywords.items()}
        return target.bind(arguments, keywords, raw_args=call.args, raw_keywords=call.keywords, caller_file=self)

    def resolve_extension(self, call):
        if not call.method or not call.receiver:
            return None
        candidates = {}
        for ext_name, on_type, body in self.extension_methods.get(call.method, ()):
            cb = Callback(self, body, {})
            candidates[cb.key] = (on_type, cb)
        for entry in self.imports:
            if entry.prefix:
                continue
            module = self.modules.load(self.path, entry.uri)
            if module is None:
                continue
            for ext_name, on_type, body in module.extension_methods.get(call.method, ()):
                if ext_name and not entry.exposes(ext_name):
                    continue
                cb = Callback(module, body, {})
                candidates[cb.key] = (on_type, cb)
            for export in module.exports:
                exported = module.modules.load(module.path, export.uri)
                if exported is None:
                    continue
                for ext_name, on_type, body in exported.extension_methods.get(call.method, ()):
                    if ext_name and (not entry.exposes(ext_name) or not export.exposes(ext_name)):
                        continue
                    cb = Callback(exported, body, {})
                    candidates[cb.key] = (on_type, cb)
        if not candidates:
            return None
        if len(candidates) == 1:
            return next(iter(candidates.values()))[1]
        owner = self.receiver_owner(call.receiver)
        if owner:
            matching = [cb for on_type, cb in candidates.values() if on_type == owner[-1]]
            if len(matching) == 1:
                return matching[0]
        return None

    def resolve_getter(self, path):
        if len(path) == 1:
            if (body := self.getters.get(path[0])) is not None:
                return Callback(self, body, {})
        targets = {}
        for module, parts in self.imported_modules(path):
            if len(parts) == 1 and (body := module.getters.get(parts[0])) is not None:
                cb = Callback(module, body, {})
                targets[cb.key] = cb
        return next(iter(targets.values())) if len(targets) == 1 else None

    def resolve_name(self, nodes, supplied, *, path=None, supplied_values=None):
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
                if supplied_values and (self, offset(binding.node)) in supplied_values:
                    caller_file, raw_arg = supplied_values[self, offset(binding.node)]
                    return caller_file.callback(raw_arg, supplied)
                return None
            visible = [(scope, body) for scope, body in self.functions.get(path[0], ()) if is_within(site, scope)]
            if visible:
                return Callback(self, max(visible, key=lambda entry: offset(entry[0]))[1], supplied)
        if len(path) == 2:
            if binding is not None:
                owner = self.receiver_owner(binding.initializer) if binding.final else ()
                return self.type_method(owner, path[-1], supplied) if owner else None
            if (body := self.methods.get(path)) is not None:
                return Callback(self, body, supplied)
        return self.imported_callback(path)

    def type_method(self, owner, method, supplied):
        if len(owner) == 1 and (body := self.methods.get((owner[0], method))) is not None:
            return Callback(self, body, supplied)
        return self.imported_callback((*owner, method))

    def receiver_owner(self, nodes):
        call = final_call(nodes)
        if call is None or call.optional:
            return ()
        if call.name:
            qualified = any(entry.prefix == call.name[0] for entry in self.imports)
            return call.name[:2] if qualified else call.name[:1]
        owner = self.receiver_owner(call.receiver)
        if owner and (method := self.type_method(owner, call.method, {})) is not None:
            returned = final_call(returned_expression(method.body))
            if (returned is not None and not returned.optional and returned.name[:1] == owner[-1:]
                    and (owner[-1], call.method) in method.file.methods):
                return owner
        return ()

    def imported_modules(self, path):
        if len(path) > 3:
            return
        for entry in self.imports:
            if entry.prefix and entry.prefix != path[0]:
                continue
            parts = path[bool(entry.prefix):]
            if parts and not parts[0].startswith("_"):
                yield from self.exported_modules(entry, parts, {self.path.resolve()})

    def exported_modules(self, entry, parts, seen):
        if not entry.exposes(parts[0]):
            return
        module = self.modules.load(self.path, entry.uri)
        if module is None or module.path in seen:
            return
        seen.add(module.path)
        yield module, parts
        for export in module.exports:
            yield from module.exported_modules(export, parts, seen)

    def imported_callback(self, path):
        targets = {}
        for module, parts in self.imported_modules(path):
            if len(parts) == 2:
                body = module.methods.get(parts)
            elif len(parts) == 1:
                body = next((body for scope, body in module.functions.get(parts[0], ())
                             if scope.kind() == "source_file"), None)
            else:
                continue
            if body is not None:
                callback = Callback(module, body, {})
                targets[callback.key] = callback
        return next(iter(targets.values())) if len(targets) == 1 else None

    def imported_values(self, path, attribute):
        values = {(module.path, parts): getattr(module, attribute)[parts[0] if len(parts) == 1 else parts]
                  for module, parts in self.imported_modules(path)
                  if (parts[0] if len(parts) == 1 else parts) in getattr(module, attribute)}
        if len(values) == 1:
            (path, _), value = next(iter(values.items()))
            return self.modules.loaded[path], value
        return None

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
        if site in self._unchanged_active:
            return True
        if site not in self.collection_writes:
            self._unchanged_active.add(site)
            try:
                self.collection_writes[site] = any(self.mutates_collection(use)
                    for use in query(binding.scope, "identifier")
                    if use.text() == binding.node.text() and self.binding((use,)) == binding)
            finally:
                self._unchanged_active.discard(site)
        return not self.collection_writes[site]

    def writes_binding(self, use):
        parent = use.parent()
        if parent.kind() == "assignable_expression":
            operation = parent.parent()
            return operation.kind() == "assignment_expression" or (
                operation.kind() in {"postfix_expression", "unary_expression"}
                and any(child.text() in {"++", "--"} for child in tokens(operation)))
        return False

    def mutates_collection(self, use):
        if self.writes_binding(use):
            return True
        parent = use.parent()
        nodes = contents(parent)
        if names(nodes) == (use.text(),) and parent.kind() in {
                "argument", "named_argument", "initialized_variable_definition", "static_final_declaration"}:
            return True
        return any(call.method in MUTATING_COLLECTION
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

    def fixed_number(self, nodes, bindings=None, seen=frozenset()):
        nodes = unwrap(nodes)
        if kind(nodes) == "additive_expression":
            operator, left, right = split(tokens(nodes[0]), {"additive_operator", "+", "-"})
            first = self.fixed_number(left, bindings, seen)
            second = self.fixed_number(right, bindings, seen)
            if type(first) in {int, float} and type(second) in {int, float} and operator in {"+", "-"}:
                return first + second if operator == "+" else first - second
            return lint.UNKNOWN
        if len(names(nodes)) == 1 and (binding := self.binding(nodes)) is not None:
            key = self, offset(binding.node)
            if bindings and key in bindings:
                return bindings[key]
            if key in seen:
                return lint.UNKNOWN
            if binding.final:
                return self.fixed_number(binding.initializer, bindings, seen | {key})
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
        title = self.title(title_nodes)
        skip = constant(call.keywords.get("skip", ()))
        callback = self.callback(target)
        span = callback.body if callback is not None and callback.file is self and final_call(target) is None else None
        if span is None:
            span = next((body for node in target for body in query(node, "function_expression_body")), call.node)
        return Registration(call, title, callback, span, skip is True or type(skip) is str)

    def title(self, nodes):
        title = string(nodes[0], display=True) if kind(nodes) == "string_literal" else None
        if title is None:
            title = self.syntax.source[offset(nodes[0]):nodes[-1].range().end.index]
        return title or "<unnamed>"

    def call_sites(self):
        seen = set()
        for arguments in query(self.syntax.root, "arguments"):
            selector = arguments.parent().parent()
            if selector.kind() != "selector":
                continue
            container = selector.parent()
            site = offset(container), container.range().end.index
            if site in seen:
                continue
            seen.add(site)
            yield from calls(contents(container))

    def inventory(self):
        from .ast_grep_dart_setup import Setups

        calls = tuple(self.call_sites())
        registrations = [entry for call in calls if (entry := self.registration(call))]
        owners = {offset(entry.span): entry for entry in registrations
                  if entry.callback is not None and kind(entry.call.args[1]) == "function_expression"}
        definitions = {offset(body) for entries in self.functions.values() for _, body in entries
                       if contents(body.prev()) and any(child.kind() == "formal_parameter_list"
                                                        and contents(child) for child in contents(body.prev()))}
        enclosing_functions = {offset(entry.call.node): next(
            (offset(node) for node in entry.call.node.ancestors() if offset(node) in definitions
             and node.kind() == "function_body"), None) for entry in registrations}

        def caller_callback(nodes):
            callback = self.callback(nodes)
            return callback if callback is not None and callback.file is self else None

        contexts = defaultdict(list)
        relevant = set(enclosing_functions.values()) - {None}
        if relevant:
            for call in calls:
                if len(call.name) != 1 or call.name[0] not in self.functions:
                    continue
                target = self.resolve_name((call.node,), {})
                if target is not None and target.file is self and offset(target.body) in relevant:
                    bound = target.bind([caller_callback(arg) for arg in call.args],
                                        {name: caller_callback(arg) for name, arg in call.keywords.items()})
                    contexts[offset(target.body)].append((call, target, bound))
        setups = Setups(self, calls)
        for entry in registrations:
            if entry.call.method == "group":
                continue
            calls_with_tests = contexts.get(enclosing_functions[offset(entry.call.node)], ())
            if calls_with_tests and any(bound.supplied for _, _, bound in calls_with_tests):
                title = self.binding(entry.call.args[0]) if len(names(entry.call.args[0])) == 1 else None
                inline = []
                callbacks = []
                for caller, target, bound in calls_with_tests:
                    callback = self.callback(entry.call.args[1], bound.supplied)
                    callbacks.append(callback)
                    supplied = target.bind(caller.args, caller.keywords).supplied if title else {}
                    label = supplied.get(offset(title.node)) if title else None
                    if label:
                        inline.append((caller, self.title(label), callback))
                if len(inline) == len(calls_with_tests):
                    for caller, label, callback in inline:
                        yield self.test_instance(entry, caller.node, caller.node.parent(), label,
                                                 [callback], owners, setups)
                else:
                    yield self.test_instance(entry, entry.call.node, entry.span, entry.title,
                                             callbacks, owners, setups)
            else:
                yield self.test_instance(entry, entry.call.node, entry.span, entry.title,
                                         [entry.callback], owners, setups)

    def test_instance(self, entry, site, span, title, callbacks, owners, setups):
        scopes = (site,) if site == entry.call.node else (site, entry.call.node)
        enclosing = {offset(node): owners[offset(node)] for scope in reversed(scopes) for node in scope.ancestors()
                     if node.kind() == "function_expression_body" and offset(node) in owners}
        qualified = "::".join([*(item.title for item in reversed(enclosing.values())), title])
        bounds = span.range()
        columns = bounds.start.column, bounds.end.column
        test = Test(title, title, line(site), bounds.start.line + 1,
                    bounds.end.line + 1, columns[0], qualified=qualified, span_columns=columns)
        return (test, callbacks, span, entry.ignored or any(item.ignored for item in enclosing.values()),
                setups.callbacks_for(*scopes), setups.collections_for(*scopes))

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
        if any(self.library_name(call.name, pkg) in MOCKITO
               for pkg in ("package:mockito/", "package:mocktail/", "package:mockingjay/")):
            return Check(call.node)
        if call.method == "validate" and call.receiver is not None and self.from_package(
                call.receiver, "package:test_descriptor/"):
            return Check(call.node)
        if method in ASYNC_EXPECTATIONS:
            count = self.fixed_number(call.keywords.get("count", ()))
            if "count" not in call.keywords or type(count) is int and count >= 0:
                return Check(call.node)
        return None

    def from_package(self, nodes, package, seen=frozenset()):
        nodes = unwrap(nodes)
        if (call := final_call(nodes)) is not None and call.name:
            if self.library_name(call.name, package):
                return True
        if len(names(nodes)) == 1 and (binding := self.binding(nodes)) is not None:
            key = offset(binding.node)
            if key not in seen and binding.final and binding.initializer:
                return self.from_package(binding.initializer, package, seen | {key})
        return False

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

    def identity_capture(self, nodes, site_offset):
        path = names(nodes)
        binding = self.binding(nodes)
        if binding is None:
            return (path or None), site_offset, False, None
        if binding.final:
            source = self.binding(binding.initializer)
            if names(binding.initializer) and source is not None and source.final:
                indirect = len(names(binding.initializer)) >= 2 or any(calls(binding.initializer))
                base_id, base_off, base_ind, scope = self.identity_capture(binding.initializer, offset(binding.node))
                if base_id is not None:
                    cap_off = offset(binding.node) if indirect and not base_ind else base_off
                    return (*base_id, *path[1:]), cap_off, (indirect or base_ind), (scope or binding.scope)
        return (offset(binding.node), *path[1:]), offset(binding.node), False, binding.scope

    def recorder_binding(self, nodes):
        nodes = unwrap(nodes)
        if nodes and nodes[0].kind() == "this" and len(nodes) >= 2 and member(nodes[1]):
            field = next((entry for entry in contents(contents(nodes[1])[0]) if identifier(entry)), None)
            if field is not None:
                owner = next((entry for entry in field.ancestors() if entry.kind() == "class_body"), None)
                if owner is not None:
                    binding = next((entry for entry in self.bindings[field.text()] if entry.scope == owner), None)
                    suffix = tuple(member(entry) for entry in nodes[2:])
                    return (binding, (field.text(), *suffix)) if all(suffix) else (None, ())
        return self.binding(nodes), names(nodes)

    def recorder_alias(self, nodes, binding):
        source, initial = self.recorder_binding(binding.initializer)
        if not initial or source == binding or binding.scope.kind() not in {"block", "function_expression"}:
            return False
        start, end = offset(binding.node), offset(unwrap(nodes)[0])
        for watched in (binding, source):
            if watched is None:
                continue
            for use in query(binding.scope, "identifier"):
                if not start < offset(use) < end or use.text() != watched.node.text() or self.binding((use,)) != watched:
                    continue
                if self.writes_binding(use):
                    return False
                parent = use.parent()
                if parent.kind() in {"argument", "named_argument"} and len(names(contents(parent))) == 1:
                    arguments = parent.parent()
                    selector = next((entry for entry in arguments.ancestors() if entry.kind() == "selector"), None)
                    call = final_call(contents(selector.parent())) if selector is not None else None
                    if call is None or self.framework_name(call.name) not in {"expect", "expectLater", "expectAsync"}:
                        return False
        return True

    def recorder_identity(self, nodes, *, collections=False):
        binding, path = self.recorder_binding(nodes)
        if binding is None:
            return (path or None) if not collections else None
        if self.recorder_alias(nodes, binding):
            if (alias := self.recorder_identity(binding.initializer, collections=collections)) is not None:
                return (*alias, *path[1:])
        if not collections or collection_size(binding.initializer, exact=True) == 0:
            return offset(binding.node), *path[1:]
        return None

    def recorder(self, nodes):
        return self.recorder_identity(nodes, collections=True)

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


def completion_check(check):
    from .ast_grep_dart_flow import Flow

    if check.matcher != "returnsNormally" or check.guarded:
        return False
    callback = check.file.callback(check.actual)
    if callback is None:
        return bool(names(check.actual))
    flow = Flow(callback.file, callback, ())
    flow.run()
    return not flow.checks and not query(callback.body, "catch_clause")


def no_crash_test(callback, test, checks, notes):
    if checks:
        return all(map(completion_check, checks))
    if not lint.claims_completion(test, notes):
        return False
    body = callback.body
    if query(body, "catch_clause"):
        return False
    block = next((node for node in contents(body) if node.kind() == "block"), None)
    steps = [contents(step) for step in contents(block)] if block is not None else [contents(body)]
    for args in steps:
        if kind(args) == "await_expression":
            args = contents(args[0])
        if (call := final_call(args)) is not None and not call.optional:
            return True
    return False


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
    for test, callbacks, span, ignored, setup_callbacks, setup_collections in candidates:
        tests.append(test)
        if ignored:
            continue
        if inventory and any(callback is not None for callback in callbacks):
            body = contents(span.parent()) if span.kind() == "identifier" else (span,)
            test.body_hash = hashlib.sha256(repr(signature(body)).encode()).hexdigest()
        completion = []
        for callback in callbacks:
            if callback is None:
                continue
            flow = Flow(file, callback, setup_callbacks, setup_collections)
            incomplete = flow.run()
            micro_snapshot(file, test, flow.checks)
            test.checks = max(test.checks, len(flow.checks))
            completion.append((not flow.checks, no_crash_test(callback, test, flow.checks, notes)))
            test.guarded |= bool(flow.checks and incomplete)
            for check in flow.checks:
                if check.file is not file:
                    continue
                site = line(check.node)
                if check.constant_success():
                    test.issues.append((site, "const-truth"))
                if flow.compares_itself(check, test.name, line_comments):
                    test.issues.append((site, "self-compare"))
            absence.collect(test, flow)
        if completion:
            lint.record_completion(test, any(missing for missing, _ in completion),
                                   all(no_crash for _, no_crash in completion))
    absence.finish()
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
