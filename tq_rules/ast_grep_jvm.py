"""Discovery and source locations for Kotlin and Java tests."""

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib

from . import lint
from .ast_grep import Syntax, children, line, location, offset, query, shape, syntax_errors, text, unwrap
from .source import Comment, Test


FUNCTIONS = {"method_declaration", "function_declaration"}
CLASSES = {"class_declaration", "object_declaration", "record_declaration", "enum_declaration"}
LAMBDAS = {"lambda_literal", "lambda_expression", "anonymous_function"}
CALLS = {"call_expression", "method_invocation"}
IDENTIFIERS = {"identifier", "simple_identifier", "type_identifier"}
TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestTemplate", "TestFactory", "Theory"}
DISABLED_ANNOTATIONS = {"Ignore", "Disabled"}
TEST_NAMESPACES = ("org.junit.", "kotlin.test.", "androidx.test.", "android.test.")
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


class File:
    def __init__(self, syntax):
        self.syntax = syntax
        self.imports = {}
        self.wildcard_imports = set()
        self.functions = defaultdict(list)
        self.bindings = defaultdict(list)
        self.styles = {}
        self.registrations = {}
        kotlin = syntax.language == "kotlin"
        for entry in query(syntax.root, "import_header" if kotlin else "import_declaration"):
            qualified = child(entry, "identifier", "scoped_identifier")
            if qualified is not None:
                if any(token.kind() in {"*", "asterisk"} for token in entry.children()):
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
        if call.receiver is not None and call.receiver.text() != "this":
            return None
        ancestors = {node_key(parent) for parent in node.ancestors()}
        visible = [(scope, function) for scope, function in self.functions.get(call.method, ())
                   if node_key(scope) in ancestors]
        if not visible:
            return None
        closest = max(offset(scope) for scope, _ in visible)
        candidates = [fn for scope, fn in visible if offset(scope) == closest]
        matching = [fn for fn in candidates if self.arity(fn) == len(call.args)]
        return matching[0] if len(matching) == 1 else candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def arity(node):
        parameters = node.field("parameters") or child(node, "function_value_parameters")
        return len(children(parameters)) if parameters is not None else 0

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
                owner = next((parent for parent in node.ancestors() if parent.kind() in CLASSES), None)
                return identifier, node_key(owner) if owner is not None else None
            base = self.recorder(receiver)
            return (base, identifier) if base is not None else None
        scope, binding = self.visible_binding(node, identifier)
        if scope is not None and scope.kind() not in CLASSES:
            return identifier, node_key(binding)
        owner = next((parent for parent in node.ancestors() if parent.kind() in CLASSES), None)
        return identifier, node_key(owner) if owner is not None else None

    def visible_binding(self, node, identifier):
        position = offset(node)
        candidates = self.bindings.get(identifier)
        if not candidates:
            return None, None
        ancestors = {node_key(parent) for parent in node.ancestors()}
        visible = [(scope, binding) for scope, binding in candidates
                   if node_key(scope) in ancestors and (offset(binding) < position or scope.kind() in CLASSES)]
        return max(visible, key=lambda entry: (offset(entry[0]), offset(entry[1])), default=(None, None))

    def immutable_value(self, node):
        if not (identifier := name(node)):
            return None
        scope, binding = self.visible_binding(node, identifier)
        if binding is None:
            return None
        if self.syntax.language == "kotlin" and binding.kind() == "variable_declaration":
            declaration = binding.parent()
            if declaration.kind() == "property_declaration":
                binding_kind = child(declaration, "binding_pattern_kind")
                if binding_kind is not None and binding_kind.text() == "val":
                    entries = children(declaration)
                    if (len(entries) == 3 and entries[1].kind() == "variable_declaration"
                            and any(token.kind() == "=" for token in declaration.children())):
                        return entries[-1]
        if self.syntax.language == "java" and binding.kind() == "variable_declarator":
            declaration = binding.parent()
            modifiers = child(declaration, "modifiers")
            if modifiers is not None and any(token.kind() == "final" for token in modifiers.children()):
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


def parse(source, path, language, *, inventory=False, observe=None):
    from .ast_grep_jvm_checks import compares_itself
    from .ast_grep_jvm_flow import Flow, merge

    from .ast_grep_jvm_micro import micro_snapshot

    syntax = Syntax.parse(source, language)
    file = File(syntax)
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    tests = []
    for test, body, ignored, expected_exception in file.inventory():
        tests.append(test)
        if inventory:
            test.body_hash = hashlib.sha256(repr(shape(body)).encode()).hexdigest()
        if ignored:
            continue
        flow = Flow(file)
        exits = flow.visit(body_of(body) if body.kind() in LAMBDAS else body)
        evidence = merge(exits.values())
        checks = list(flow.checks.values())
        micro_snapshot(file, test, flow.checks)
        test.checks = len(checks) + int(expected_exception)
        if not test.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(checks) and not (evidence.checked or expected_exception)
        for check in checks:
            site = line(check.node)
            if check.constant(syntax):
                test.issues.append((site, "const-truth"))
            if compares_itself(syntax, check, test.name, line_comments, checks):
                test.issues.append((site, "self-compare"))
            if any(not present and recorder not in evidence.witnesses for recorder, present in check.observations):
                test.issues.append((site, "absence-no-control"))
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
