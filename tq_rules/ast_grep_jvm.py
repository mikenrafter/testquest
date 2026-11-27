"""Discovery and source locations for Kotlin and Java tests."""

from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib

from . import lint
from .ast_grep import Syntax, children, line, location, offset, query, shape, syntax_errors, text, unwrap
from .ast_grep_languages import JVM_LANGUAGES
from .source import Comment, Test, project_root


FUNCTIONS = {"method_declaration", "function_declaration"}
CLASSES = {"class_declaration", "object_declaration", "record_declaration", "enum_declaration"}
LAMBDAS = {"lambda_literal", "lambda_expression", "anonymous_function"}
CALLS = {"call_expression", "method_invocation"}
IDENTIFIERS = {"identifier", "simple_identifier", "type_identifier"}
TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestTemplate", "TestFactory", "Theory"}
DISABLED_ANNOTATIONS = {"Ignore", "Disabled"}
TEST_NAMESPACES = ("org.junit.", "kotlin.test.", "androidx.test.", "android.test.")
MODULE_MARKERS = {"build.gradle", "build.gradle.kts", "pom.xml", ".git"}
STYLES = {
    "BehaviorSpec": ({"given", "when", "and"}, {"then"}),
    "DescribeSpec": ({"describe", "context"}, {"it"}),
    "ExpectSpec": ({"context"}, {"expect"}),
    "FeatureSpec": ({"feature"}, {"scenario"}),
    "FreeSpec": (set(), set()),
    "FunSpec": ({"context"}, {"test"}),
    "ShouldSpec": ({"context"}, {"should"}),
    "StringSpec": (set(), set()),
    "WordSpec": ({"should", "when"}, set()),
}


def child(node, *kinds):
    return next((entry for entry in children(node) if entry.kind() in kinds), None) if node is not None else None


def name(node):
    return node.text().strip("`") if node is not None and node.kind() in IDENTIFIERS else ""


def node_key(node):
    span = node.range()
    return node.kind(), span.start.index, span.end.index


def declaration_name(node):
    return name(node.field("name") or child(node, *IDENTIFIERS))


def body_of(node):
    return node.field("body") or child(node, "function_body", "statements", "block")


def owner_of(node):
    return next((parent for parent in node.ancestors() if parent.kind() in CLASSES), None)


def member(node):
    node = unwrap(node)
    if node is not None and node.kind() == "navigation_expression":
        receiver, *suffixes = children(node)
        suffix = suffixes[-1]
        return receiver, name(child(suffix, *IDENTIFIERS))
    if node is not None and node.kind() == "field_access":
        return node.field("object"), name(node.field("field"))
    return None, name(node)


def has_safe_navigation(node):
    node = unwrap(node)
    if node is None or node.kind() != "navigation_expression":
        return False
    receiver, *suffixes = children(node)
    return has_safe_navigation(receiver) or any(
        any(token.kind() == "?." for token in suffix.children()) for suffix in suffixes)


@dataclass
class Call:
    method: str
    args: list
    receiver: object = None
    named: dict = field(default_factory=dict)
    callee: object = None

    def callbacks(self):
        return [arg for arg in self.args if arg.kind() in LAMBDAS]


def call_of(node):
    node = unwrap(node)
    if node is None:
        return None
    if node.kind() == "method_invocation":
        arguments = node.field("arguments")
        return Call(name(node.field("name")), children(arguments) if arguments is not None else [],
                    node.field("object"))
    if node.kind() != "call_expression":
        return None
    elements = children(node)
    if len(elements) < 2:
        return None
    callee, suffix = elements[:2]
    if suffix.kind() != "call_suffix":
        return None
    args, named = [], {}
    arguments = child(suffix, "value_arguments")
    for entry in children(arguments) if arguments is not None else ():
        values = children(entry)
        if not values:
            continue
        args.append(values[-1])
        if len(values) > 1 and any(token.kind() == "=" for token in entry.children()):
            named[name(values[0])] = values[-1]
    trailing = child(child(suffix, "annotated_lambda"), *LAMBDAS)
    if trailing is not None:
        args.append(trailing)
    receiver, method = member(callee)
    return Call(method, args, receiver, named, callee)


def bind_arguments(declaration, call):
    parameters = declaration.field("parameters") or child(declaration, "function_value_parameters")
    defaults = {}
    entries = []
    for entry in children(parameters) if parameters is not None else ():
        if entry.kind() in {"parameter", "formal_parameter", "spread_parameter"}:
            entries.append(entry)
        elif entries and entry.prev() is not None and entry.prev().kind() == "=":
            defaults[declaration_name(entries[-1])] = entry
    named = {declaration_name(entry): entry for entry in entries}
    if not call.named.keys() <= named.keys():
        return None
    supplied = {identifier: (value, True) for identifier, value in call.named.items()}
    positional = iter(identifier for identifier in named if identifier not in supplied)
    for argument in call.args:
        if any(argument is value for value in call.named.values()):
            continue
        if argument.kind() in LAMBDAS and argument.parent().kind() == "annotated_lambda":
            identifier = next(reversed(named), None)
        else:
            identifier = next(positional, None)
        if identifier is None or identifier in supplied:
            return None
        supplied[identifier] = argument, True
    if not named.keys() <= supplied.keys() | defaults.keys():
        return None
    return [(parameter, *supplied.get(identifier, (defaults.get(identifier), False)))
            for identifier, parameter in named.items()]


def has_modifier(node, word):
    modifiers = child(node, "modifiers")
    return modifiers is not None and any(token.text() == word for token in modifiers.children())


def read_file(path, module):
    try:
        stat = path.stat()
        return cached_file(path, module, stat.st_mtime_ns, stat.st_size)
    except (OSError, UnicodeDecodeError):
        return None


@lru_cache(maxsize=128)
def cached_file(path, module, modified, size):
    return File(parse_syntax(path.read_text(encoding="utf-8"), JVM_LANGUAGES[path.suffix]), path, module)


def parse_syntax(source, language):
    syntax = Syntax.parse(source, language)
    if language != "kotlin" or not query(syntax.root, "ERROR"):
        return syntax
    repairs = []
    for delegation in query(syntax.root, "explicit_delegation"):
        if not any(any(name(entry) in {"try", "finally"} for entry in children(infix))
                   for infix in query(delegation, "infix_expression")):
            continue
        keyword = next((entry for entry in delegation.children() if entry.kind() == "by"), None)
        if keyword is None:
            continue
        start = offset(keyword)
        header = source[start:].split("\n", 1)[0]
        prefix, brace, _ = header.partition("{")
        delegate = prefix.removeprefix("by").strip()
        if brace and delegate.isascii() and all(part.isidentifier() for part in delegate.split(".")):
            repairs.append((start, start + len(prefix)))
    if not repairs:
        return syntax
    # CLAIM(jvm-delegation-span): masking a misparsed delegate preserves source locations
    # and recovers the following statements [test_object_delegation_does_not_swallow_a_test].
    repaired = source
    for start, end in sorted(repairs, reverse=True):
        repaired = repaired[:start] + " " * (end - start) + repaired[end:]
    candidate = Syntax.parse(repaired, language)
    if len(query(candidate.root, "ERROR")) < len(query(syntax.root, "ERROR")):
        candidate.source = source
        return candidate
    return syntax


def string_title(node):
    if node is None or node.kind() != "string_literal":
        return None
    if any(entry.kind() not in {"string_content", "string_fragment"} for entry in children(node)):
        return None
    return node.text().removeprefix('"""').removesuffix('"""') if node.text().startswith('"""') else node.text()[1:-1]


@dataclass
class Registration:
    title: str
    body: object
    is_test: bool
    ignored: bool = False


@dataclass(frozen=True)
class Recorder:
    identity: tuple
    shared: tuple | None = None


class File:
    def __init__(self, syntax, path, module):
        self.syntax = syntax
        self.path = path
        self.module = module
        package = child(syntax.root, "package_header", "package_declaration")
        self.package = next(iter(children(package))).text() if package is not None and children(package) else ""
        self.imports = {}
        self.wildcard_imports = set()
        self.functions = defaultdict(list)
        self.bindings = defaultdict(list)
        self.styles = {}
        self.registrations = {}
        self.packages = {}
        self.resolved_types = {}
        self.callback_origins = None
        kotlin = syntax.language == "kotlin"
        type_kinds = ("class_declaration", "object_declaration") if kotlin else (
            "class_declaration", "record_declaration", "enum_declaration")
        self.types = {declaration_name(node): node for node in query(syntax.root, *type_kinds)}
        for entry in query(syntax.root, "import_header" if kotlin else "import_declaration"):
            qualified = child(entry, "identifier", "scoped_identifier")
            if qualified is not None:
                if any(token.kind() in {"*", "asterisk", "wildcard_import"} for token in entry.children()):
                    self.wildcard_imports.add(qualified.text())
                    continue
                alias = child(child(entry, "import_alias"), *IDENTIFIERS)
                self.imports[name(alias) or qualified.text().rsplit(".", 1)[-1]] = qualified.text()
        for entry in query(syntax.root, "function_declaration" if kotlin else "method_declaration"):
            if body_of(entry) is not None:
                scope = next((parent for parent in entry.ancestors() if parent.kind() in
                              CLASSES | FUNCTIONS | {"statements", "block"}), syntax.root)
                self.functions[declaration_name(entry)].append((scope, entry))
        for entry in query(syntax.root, *(
                ("variable_declaration", "parameter", "class_parameter")
                if kotlin else ("variable_declarator", "formal_parameter", "spread_parameter",
                                "enhanced_for_statement", "catch_formal_parameter"))):
            self.index_binding(entry)

    def resolve_name(self, identifier):
        return self.imports.get(identifier, identifier).rsplit(".", 1)[-1]

    def annotations(self, node):
        modifiers = child(node, "modifiers")
        for annotation in children(modifiers) if modifiers is not None else ():
            if annotation.kind() not in {"annotation", "marker_annotation"}:
                continue
            constructor = child(annotation, "constructor_invocation")
            target = child(constructor or annotation, "user_type", "scoped_identifier", "identifier")
            if target is None:
                continue
            qualified = self.imports.get(target.text(), target.text())
            if "." not in qualified or qualified.startswith(TEST_NAMESPACES):
                yield qualified.rsplit(".", 1)[-1], annotation

    def ignored(self, node):
        return any(annotation in DISABLED_ANNOTATIONS for annotation, _ in self.annotations(node))

    def helper(self, node, call):
        if call.receiver is None or call.receiver.text() == "this":
            ancestors = {node_key(parent) for parent in node.ancestors()}
            visible = [(scope, function) for scope, function in self.functions.get(call.method, ())
                       if node_key(scope) in ancestors and bind_arguments(function, call) is not None]
            if visible:
                closest = max(offset(scope) for scope, _ in visible)
                candidates = [fn for scope, fn in visible if offset(scope) == closest]
                return (self, candidates[0]) if len(candidates) == 1 else None
        if call.receiver is None and call.callbacks():
            qualified = self.imports.get(call.method)
            package, _, method = qualified.rpartition(".") if qualified else (self.package, "", call.method)
            candidates = [(file, fn) for file in self.package_files(package)
                          if file.path != self.path
                          for scope, fn in file.functions.get(method, ())
                          if node_key(scope) == node_key(file.syntax.root)
                          and not has_modifier(fn, "private") and bind_arguments(fn, call) is not None]
        elif call.callbacks() and (receiver := name(unwrap(call.receiver)))[:1].isupper():
            target = self.type_named(receiver)
            if target is None:
                return None
            file, owner = target
            candidates = [(file, fn) for scope, fn in file.functions.get(call.method, ())
                          if node_key(scope) == node_key(owner) and bind_arguments(fn, call) is not None
                          and (file is self or not has_modifier(fn, "private"))
                          and (owner.kind() == "object_declaration"
                               or any(parent.kind() == "companion_object" for parent in fn.ancestors())
                               or file.syntax.language == "java" and has_modifier(fn, "static"))]
        else:
            return None
        return candidates[0] if len(candidates) == 1 else None

    def package_files(self, package):
        if package in self.packages:
            return self.packages[package]
        directories = {self.path.parent} if package == self.package else set()
        if self.module is not None:
            package_path = package.split(".") if package else ()
            src = self.module / "src"
            for source_set in src.glob("*"):
                for language in ("java", "kotlin"):
                    directories.add(source_set.joinpath(language, *package_path))
        files = []
        for directory in sorted(directories):
            for path in sorted(directory.iterdir()) if directory.is_dir() else ():
                if path.suffix in JVM_LANGUAGES and path.suffix != ".kts" and not path.is_symlink():
                    file = self if path == self.path else read_file(path, self.module)
                    if file is not None and file.package == package:
                        files.append(file)
        self.packages[package] = tuple(files)
        return self.packages[package]

    def type_named(self, identifier):
        if identifier in self.resolved_types:
            return self.resolved_types[identifier]
        qualified = self.imports.get(identifier, identifier)
        package, _, short = qualified.rpartition(".")
        local = self.types.get(short)
        if local is not None and (not package or package == self.package):
            return self, local
        packages = [package] if package else [self.package, *sorted(self.wildcard_imports)]
        candidates = [(file, file.types[short]) for pkg in packages
                      for file in self.package_files(pkg) if short in file.types
                      and (file is self or not has_modifier(file.types[short], "private"))]
        target = candidates[0] if len(candidates) == 1 else None
        self.resolved_types[identifier] = target
        return target

    def object_property(self, receiver, identifier):
        if not (type_name := name(receiver))[:1].isupper() or (target := self.type_named(type_name)) is None:
            return None
        file, owner = target
        if owner.kind() != "object_declaration":
            return None
        for scope, field in file.bindings.get(identifier, ()):
            if node_key(scope) == node_key(owner) and not has_modifier(field.parent(), "private"):
                value = file.initializer(field, immutable=True)
                if value is not None:
                    return file, value
        return None

    def index_binding(self, node):
        identifier = declaration_name(node)
        if not identifier:
            return
        scopes = CLASSES | FUNCTIONS | LAMBDAS | {"block", "statements", "for_statement",
                                                "enhanced_for_statement", "catch_clause", "catch_block"}
        scope = node if node.kind() == "enhanced_for_statement" else next(
            (parent for parent in node.ancestors() if parent.kind() in scopes), self.syntax.root)
        self.bindings[identifier].append((scope, node))

    def recorder(self, node):
        node = unwrap(node)
        if node is None:
            return None
        receiver, identifier = member(node)
        if not identifier:
            return None
        if receiver is not None:
            if receiver.text() == "this":
                owner = owner_of(node)
                fields = self.bindings.get(identifier, ())
                binding = next((binding for scope, binding in fields
                                if owner is not None and node_key(scope) == node_key(owner)), None)
                return self.binding_recorder(node, identifier, owner, binding)
            base = self.recorder(receiver)
            if base is None:
                return None
            shared = self.rig_member(receiver, identifier)
            if shared is None and base.shared is not None:
                shared = (*base.shared, identifier)
            return Recorder((*base.identity, identifier), shared)
        scope, binding = self.visible_binding(node, identifier)
        return self.binding_recorder(node, identifier, scope, binding)

    def binding_recorder(self, node, identifier, scope, binding):
        if binding is None:
            owner = owner_of(node)
            return Recorder((self.path, node_key(owner) if owner is not None else None, identifier))
        identity = (self.path, node_key(binding))
        if scope.kind() in CLASSES or scope.kind() in {"source_file", "program"}:
            return Recorder(identity, identity)
        initializer = self.initializer(binding, immutable=True)
        call = call_of(initializer)
        if (call is not None and not call.args and call.receiver is None
                and not call.method[:1].isupper() and (helper := self.helper(initializer, call)) is not None):
            file, declaration = helper
            body = body_of(declaration)
            kind = "call_expression" if file.syntax.language == "kotlin" else "method_invocation"
            if any(inner.method in {"mockk", "mock", "spy", "spyk"}
                   for entry in query(body, kind) if (inner := call_of(entry)) is not None):
                return Recorder(identity, (file.path, node_key(declaration)))
        return Recorder(identity)

    def rig_member(self, node, identifier):
        node = unwrap(node)
        if not (receiver := name(node)):
            return None
        scope, binding = self.visible_binding(node, receiver)
        initializer = self.stable_initializer(node, scope, binding)
        if initializer is None and scope is not None and scope.kind() in LAMBDAS:
            parameter_list = child(scope, "lambda_parameters")
            if parameter_list is not None and len(children(parameter_list)) != 1:
                return None
            invoked = next((parent for parent in scope.ancestors() if parent.kind() in CALLS), None)
            call = call_of(invoked)
            if call is not None and call.method in {"use", "let", "also"}:
                initializer = call.receiver
        if (constructed := self.constructed_type(initializer)) is not None:
            file, type_name = constructed
            return file.support_field(type_name, identifier)
        return None

    def constructed_type(self, node, active=frozenset()):
        if node is None:
            return None
        if node.kind() == "object_creation_expression":
            target = node.field("type")
            return (self, target.text()) if target is not None else None
        call = call_of(node)
        if call is None:
            return None
        if call.method[:1].isupper():
            return self, call.callee.text() if call.callee is not None else call.method
        if call.method in {"also", "apply"}:
            return self.constructed_type(call.receiver, active)
        if (local := self.helper(node, call)) is not None:
            file, declaration = local
            key = file.path, node_key(declaration)
            if key in active:
                return None
            declared_type = next((entry for entry in reversed(children(declaration))
                                  if entry.kind() == "user_type"), None)
            if declared_type is not None:
                return file, declared_type.text()
            expression = children(body_of(declaration))
            if len(expression) == 1:
                return file.constructed_type(expression[0], active | {key})
        return None

    def support_field(self, type_name, identifier):
        if (target := self.type_named(type_name)) is None:
            return None
        file, owner = target
        if file.path != self.path and not {"test", "androidTest", "testFixtures"}.intersection(
                file.path.relative_to(self.module).parts[:-1]):
            return None
        return next(((file.path, node_key(field)) for scope, field in file.bindings.get(identifier, ())
                     if node_key(scope) == node_key(owner)), None)

    def collection_recorder(self, node):
        recorder = self.recorder(node)
        if recorder is None or recorder.shared is not None:
            return recorder
        base = unwrap(node)
        while (receiver := member(base)[0]) is not None:
            base = receiver
        scope, binding = self.visible_binding(base, name(base))
        initialized = self.initializer(binding)
        if initialized is None or scope.kind() in CLASSES:
            return recorder
        origin = call_of(initialized)
        if origin is not None and not (origin.receiver is None and origin.method in {
                "mutableListOf", "mutableSetOf", "mutableMapOf", "arrayListOf", "hashSetOf", "hashMapOf",
                "ArrayList", "HashSet", "HashMap", "ArrayDeque", "LinkedList", "ConcurrentLinkedQueue",
                "LinkedBlockingQueue", "CopyOnWriteArrayList", "mutableStateListOf"}):
            return None
        return recorder

    def visible_binding(self, node, identifier):
        candidates = self.bindings.get(identifier)
        if not candidates:
            return None, None
        position = offset(node)
        ancestors = {node_key(parent) for parent in node.ancestors()}
        visible = [(scope, binding) for scope, binding in candidates
                   if node_key(scope) in ancestors and (offset(binding) < position or scope.kind() in CLASSES)]
        return max(visible, key=lambda entry: (offset(entry[0]), offset(entry[1])), default=(None, None))

    def immutable_value(self, node):
        if not (identifier := name(node)):
            return None
        _, binding = self.visible_binding(node, identifier)
        return self.initializer(binding, immutable=True)

    def local_array(self, node):
        if self.syntax.language != "java" or not (identifier := name(node)):
            return None
        value = self.stable_initializer(node, *self.visible_binding(node, identifier))
        return value if value is not None and value.kind() in {"array_initializer", "array_creation_expression"} else None

    def stable_initializer(self, node, scope, binding):
        immutable = self.initializer(binding, immutable=True)
        if immutable is not None or self.syntax.language != "java" or scope is None or scope.kind() != "block":
            return immutable
        identifier = name(node)
        for assignment in query(scope, "assignment_expression"):
            left = assignment.field("left")
            if (left is not None and name(left) == identifier and offset(binding) < offset(left) < offset(node)):
                return None
        return self.initializer(binding)

    def initializer(self, binding, *, immutable=False):
        if binding is None:
            return None
        if self.syntax.language == "kotlin" and binding.kind() == "variable_declaration":
            declaration = binding.parent()
            if declaration.kind() == "property_declaration":
                binding_kind = child(declaration, "binding_pattern_kind")
                if binding_kind is not None and (not immutable or binding_kind.text() == "val"):
                    tokens = declaration.children()
                    for index, token in enumerate(tokens[:-1]):
                        if token.kind() == "=":
                            return tokens[index + 1]
        if self.syntax.language == "java" and binding.kind() == "variable_declarator":
            declaration = binding.parent()
            modifiers = child(declaration, "modifiers")
            if not immutable or modifiers is not None and any(token.kind() == "final" for token in modifiers.children()):
                return binding.field("value")
        return None

    def style(self, owner):
        key = node_key(owner)
        if key not in self.styles:
            self.styles[key] = ""
            for specifier in children(owner):
                if specifier.kind() != "delegation_specifier":
                    continue
                target = child(child(specifier, "constructor_invocation"), "user_type")
                if target is not None and (style := self.resolve_name(target.text().rsplit(".", 1)[-1])) in STYLES:
                    self.styles[key] = style
                    break
        return self.styles[key]

    def registration(self, node, style):
        if node.kind() == "additive_expression" and style == "FreeSpec":
            entries = children(node)
            if (len(entries) == 2 and any(token.kind() == "-" for token in node.children())
                    and (title := string_title(entries[0])) is not None and entries[1].kind() in LAMBDAS):
                return Registration(title, entries[1], False)
            return None
        call = call_of(node)
        if call is None or not (callbacks := call.callbacks()):
            return None
        from .ast_grep_jvm_checks import truth

        disabled = truth(self.syntax, call.named.get("enabled")) is False
        if style in {"StringSpec", "FreeSpec", "WordSpec"}:
            title = string_title(call.receiver if call.method == "config" else call.callee)
            if title is not None:
                return Registration(title, callbacks[-1], True, disabled)
            if call.method in {"should", "When", "when"} and (title := string_title(call.receiver)) is not None:
                return Registration(title, callbacks[-1], False, disabled)
        registration = call_of(call.receiver) if call.method == "config" else call
        if registration is None or registration.receiver is not None:
            return None
        suites, tests = STYLES[style]
        method = registration.method.lower()
        if method.startswith("x") and method[1:] in suites | tests:
            method, disabled = method[1:], True
        if method not in suites | tests:
            return None
        title = next((title for arg in registration.args if (title := string_title(arg)) is not None), None)
        if title is not None:
            return Registration(title, callbacks[-1], method in tests, disabled)
        return None

    def inventory(self):
        kotlin = self.syntax.language == "kotlin"
        kinds = ("function_declaration", "call_expression", "additive_expression") if kotlin else ("method_declaration",)
        for node in query(self.syntax.root, *kinds):
            owners = [parent for parent in node.ancestors() if parent.kind() in CLASSES]
            namespace = [declaration_name(owner) for owner in reversed(owners)]
            if node.kind() in FUNCTIONS:
                body = body_of(node)
                if body is None:
                    continue
                annotations = list(self.annotations(node))
                test_annotation = next((entry for annotation, entry in annotations if annotation in TEST_ANNOTATIONS), None)
                if test_annotation is None:
                    continue
                title = declaration_name(node)
                ignored = self.ignored(node) or any(self.ignored(owner) for owner in owners)
                expected_exception = any(
                    name(child(entry, *IDENTIFIERS)) == "expected"
                    and (target := children(entry)[-1]).kind() in {"class_literal", "callable_reference"}
                    and target.text().split(".", 1)[0] != "None"
                    and not target.text().endswith(("None.class", "None::class"))
                    for entry in query(test_annotation, "value_argument" if kotlin else "element_value_pair"))
                yield make_test(title, namespace, line(test_annotation), body), body, ignored, expected_exception
            elif owners and (style := self.style(owners[0])):
                parents = []
                for parent in node.ancestors():
                    if parent.kind() in FUNCTIONS:
                        break
                    if parent.kind() in {"call_expression", "additive_expression"}:
                        registration = self.registrations.get(node_key(parent))
                        if registration is None or registration.is_test:
                            break
                        parents.append(registration)
                    if node_key(parent) == node_key(owners[0]):
                        registration = self.registration(node, style)
                        if registration is not None:
                            self.registrations[node_key(node)] = registration
                            if registration.is_test:
                                groups = [group.title for group in reversed(parents)]
                                ignored = (registration.ignored or any(group.ignored for group in parents)
                                           or any(self.ignored(owner) for owner in owners))
                                yield make_test(registration.title, namespace + groups, line(node), registration.body), registration.body, ignored, False
                        break


def make_test(title, namespace, registration, body):
    span = body.range()
    return Test(title, title, registration, span.start.line + 1, span.end.line + 1,
                span.start.column, qualified="::".join((*namespace, title)),
                span_columns=(span.start.column, span.end.column))


def comments(syntax):
    block = "multiline_comment" if syntax.language == "kotlin" else "block_comment"
    for node in query(syntax.root, "line_comment", block):
        raw = node.text()
        site = node.range().start
        start = syntax.source.rfind("\n", 0, site.index) + 1
        standalone = not syntax.source[start:site.index].strip()
        content = raw[2:-2] if raw.startswith("/*") else raw[2:]
        for index, entry in enumerate(content.splitlines()):
            text = entry.strip().removeprefix("*").strip().removeprefix("#").strip()
            if text:
                yield Comment(site.line + index + 1, site.column if index == 0 else len(entry) - len(entry.lstrip()),
                              text, standalone or index > 0)


def completion_control(file, test, body, notes):
    if file.syntax.language != "kotlin" or not any(
            comment.standalone and comment.line == test.start - 1
            and comment.text.lower().removeprefix("why:").lstrip().startswith("positive control.")
            for comment in notes):
        return False

    from .ast_grep_jvm_flow import expects_failure

    block = child(body, "statements")
    steps = children(block) if block is not None else []
    cleanup = call_of(steps[-1]) if steps else None
    if cleanup is None or cleanup.method != "close" or cleanup.args or has_safe_navigation(cleanup.callee):
        return False
    attempt = call_of(cleanup.receiver)
    helper = file.helper(cleanup.receiver, attempt) if attempt is not None else None
    if helper is None:
        return False
    _, declaration = helper
    for sibling, sibling_body, ignored, _ in file.inventory():
        if ignored or sibling.qualified.rpartition("::")[0] != test.qualified.rpartition("::")[0] or sibling.line <= test.line:
            continue
        for entry in query(sibling_body, "call_expression"):
            call = call_of(entry)
            if call is None or not call.callbacks():
                continue
            callback = call.callbacks()[-1]
            block = body_of(callback)
            entries = children(block) if block is not None else []
            if len(entries) != 1 or shape(entries[0]) != shape(cleanup.receiver):
                continue
            target = file.helper(entries[0], call_of(entries[0]))
            if target is None or target[0] is not helper[0] or node_key(target[1]) != node_key(declaration):
                continue
            method = file.resolve_name(call.method)
            if method in {"assertThrows", "assertFailsWith", "shouldThrow", "shouldThrowExactly"}:
                return True
            wrapper = file.helper(entry, call)
            if wrapper is not None and expects_failure(wrapper[1]):
                return True
    return False


def no_crash_test(file, test, body, checks, notes, missing, checked):
    if checks:
        return checked and all(check.no_throw for check in checks)
    if not missing or query(body, "catch_block" if file.syntax.language == "kotlin" else "catch_clause"):
        return False
    if completion_control(file, test, body, notes):
        return True
    if not lint.claims_completion(test, notes):
        return False
    statements = children(body_of(body) if body.kind() in LAMBDAS else body)
    while len(statements) == 1 and statements[0].kind() in {"block", "statements", "function_body"}:
        statements = children(statements[0])
    for statement in statements:
        if statement.kind() == "expression_statement":
            statement = next(iter(children(statement)), statement)
        if (call := call_of(statement)) is not None and not has_safe_navigation(call.callee):
            return True
    return False


def parse(source, path, language, *, scan_root=None, collection_controls=None, inventory=False, observe=None):
    from .ast_grep_jvm_checks import compares_itself
    from .ast_grep_jvm_controls import (
        analyze, answer_controls, capture_controls, shared_keys, sibling_controls, split_collection_controls,
    )
    from .ast_grep_jvm_micro import micro_snapshot
    from .ast_grep_jvm_profiles import disabled_output, observed_mock_controls

    syntax = parse_syntax(source, language)
    resolved = path.resolve()
    file = File(syntax, resolved, project_root(resolved, scan_root, MODULE_MARKERS))
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    tests = []
    analyses = []
    fixed_sizes = defaultdict(set)
    for test, body, ignored, expected_exception in file.inventory():
        tests.append(test)
        if inventory:
            test.body_hash = hashlib.sha256(repr(shape(body)).encode()).hexdigest()
        if not ignored:
            flow, evidence = analyze(file, body)
            analyses.append((test, body, expected_exception, flow, evidence))
            for key, size in evidence.sizes:
                fixed_sizes[key].add(size)

    sizes = {key: next(iter(counts)) for key, counts in fixed_sizes.items() if len(counts) == 1}
    controls_by_test = []
    absences = []
    for index, (test, body, expected_exception, flow, evidence) in enumerate(analyses):
        if sizes and flow.loop_checks and not evidence.checked:
            flow, evidence = analyze(file, body, sizes)
        witnesses = (evidence.witnesses | capture_controls(file, body, evidence.witnesses)
                     | answer_controls(file, body, evidence.witnesses)
                     | split_collection_controls(file, body, evidence.witnesses))
        controls_by_test.append(shared_keys(witnesses))
        checked_reads = observed_mock_controls(evidence.witnesses)
        checks = list(flow.checks.values())
        micro_snapshot(file, test, flow.checks)
        test.checks = len(checks) + int(expected_exception)
        missing = not test.checks and not flow.opaque_check
        if not expected_exception:
            lint.record_completion(test, missing, no_crash_test(file, test, body, checks, notes, missing, evidence.checked))
        test.guarded = bool(checks) and not (evidence.checked or expected_exception)
        if test.guarded and flow.loop_checks.keys() == flow.checks.keys():
            source = next(iter(flow.loop_checks.values()))
            expression = " ".join(source.text().split())
            filtered = flow.filtered_source(source)
            if test.issue_messages is None:
                test.issue_messages = {}
            test.issue_messages[test.line, "guarded-assert"] = (
                f"The checks can all be skipped when filtering the result in `{expression}` leaves no items."
                if filtered else
                f"The checks can all be skipped when the loop over `{expression}` runs zero times.")
        for check in checks:
            site = line(check.node)
            if check.constant(syntax):
                test.issues.append((site, "const-truth"))
            if compares_itself(syntax, check, test.name, line_comments, checks):
                test.issues.append((site, "self-compare"))
            if check.disabled:
                issue = site, "absence-no-control"
                test.issues.append(issue)
                if test.issue_messages is None:
                    test.issue_messages = {}
                test.issue_messages[issue] = (
                    "The fixture disables the collaborator whose absence is asserted. "
                    "Enable the collaborator to exercise the path checked here.")

            for key, present in check.observations:
                if present or key[1] in check.disabled:
                    continue
                related = check.controls.get(key, frozenset({key}))
                captured = next((recorder for channel, recorder in related if channel == "captured-call"), None)
                if captured is not None and (setting := disabled_output(
                        file, body, check.node, key[1], captured.shared[-1])) is not None:
                    issue = site, "absence-no-control"
                    test.issues.append(issue)
                    if test.issue_messages is None:
                        test.issue_messages = {}
                    test.issue_messages[issue] = (
                        f"The output path is disabled by `{setting} = false`. Enable it to exercise the path checked here.")
                    continue
                if not related & (witnesses | checked_reads):
                    absences.append((index, test, site, shared_keys(related)))

    pending = []
    for index, test, site, shared in absences:
        if any(shared & controls for sibling, controls in enumerate(controls_by_test)
               if index != sibling or not any(key[0] in {"mock-method", "mock-type", "captured-mock", "spy-type"} for key in shared)):
            continue
        if shared and collection_controls is not None:
            pending.append((test, site, shared))
        else:
            test.issues.append((site, "absence-no-control"))
    if collection_controls is not None and any(controls_by_test):
        collection_controls.context["jvm", file.module, file.path] = set.union(*controls_by_test)
    if pending:
        def resolve(cache):
            wanted = set.union(*(shared for _, _, shared in pending))
            known = sibling_controls(file, wanted, cache)
            for test, site, shared in pending:
                if not shared & known:
                    test.issues.append((site, "absence-no-control"))
        collection_controls.deferred.append(resolve)
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
