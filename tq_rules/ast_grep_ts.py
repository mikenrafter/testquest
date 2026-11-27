"""Discover and analyze ECMAScript test callbacks through ast-grep."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib

from . import ast_grep_micro as micro, lint
from .ast_grep import Syntax, children, line, location, offset, opposite, query, shape, split_operands, syntax_errors, text, unwrap
from .ast_grep_languages import ECMASCRIPT_LANGUAGES
from .source import Comment, Test


FUNCTIONS = {"arrow_function", "function_expression", "function_declaration", "generator_function",
             "generator_function_declaration", "method_definition"}
SCOPES = FUNCTIONS | {"statement_block", "program"}
SUITES = {"describe", "suite", "context"}
TESTS = {"test", "it", "specify"}
MODIFIERS = {"each", "only", "skip", "todo", "concurrent", "serial", "parallel", "failing", "fixme", "skipIf", "runIf", "fails"}
FACTORIES = {"each", "skipIf", "runIf"}
FRAMEWORKS = {"vitest", "jest", "@jest/globals", "node:test", "mocha", "@playwright/test", "playwright/test",
              "bun:test", "ava", "chai", "fast-check", "vite-plus/test", "@alchemy.run/test", "@effect/vitest"}
ASSERT_MODULES = {"assert", "assert/strict", "node:assert", "node:assert/strict"}
ASSERT_EQUAL = {"equal", "equals", "eq", "strictEqual", "deepEqual", "deepStrictEqual", "eql", "eqls",
                "strictEquals", "deepEquals", "is", "toBe", "toEqual", "toStrictEqual"}
ASSERT_UNEQUAL = {"notEqual", "notStrictEqual", "notDeepEqual", "notDeepStrictEqual", "notDeepEquals",
                  "not", "isNot", "notStrictEquals"}
ASSERT_PREDICATES = {"assert", "ok", "isOk", "truthy", "falsy", "isNotOk", "isTrue", "isFalse", "true", "false"}
CONTEXT_CHECKS = (ASSERT_EQUAL | ASSERT_UNEQUAL | ASSERT_PREDICATES |
                  {"pass", "fail", "throws", "throwsAsync", "notThrows", "notThrowsAsync", "regex",
                       "notRegex", "like", "snapshot", "match", "doesNotMatch"})
SPY_MATCHERS = {"toHaveBeenCalled", "toBeCalled", "toHaveBeenCalledWith", "toBeCalledWith",
                "toHaveBeenCalledOnce", "toHaveBeenCalledExactlyOnceWith", "toHaveBeenLastCalledWith",
                "toHaveBeenNthCalledWith", "lastCalledWith", "nthCalledWith"}
SPY_COUNTS = {"toHaveBeenCalledTimes", "toBeCalledTimes"}
EXPECT_MODULES = {"vitest", "vite-plus/test", "jest", "@jest/globals", "chai", "@playwright/test",
                  "playwright/test", "bun:test", "@alchemy.run/test", "@effect/vitest"}
NODE_ASSERT_CHECKS = {"assert", "ok", "equal", "notEqual", "deepEqual", "notDeepEqual", "strictEqual",
                      "notStrictEqual", "deepStrictEqual", "notDeepStrictEqual", "partialDeepStrictEqual",
                      "match", "doesNotMatch", "throws", "doesNotThrow", "rejects", "doesNotReject",
                      "fail", "ifError"}
EXPECT_RECEIVERS = {("expect",), ("expect", "soft"), ("expect", "poll"), ("expect", "element")}
ITERATORS = {"map", "forEach", "flatMap", "filter", "some", "every", "find", "findIndex", "reduce", "reduceRight"}
CHAINS = {"then", "catch", "finally"}
TRANSPARENT = {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression",
               "type_assertion", "instantiation_expression"}


def expression(node):
    while node is not None and node.kind() in TRANSPARENT:
        elements = children(node)
        node = elements[-1] if node.kind() == "type_assertion" else elements[0]
    return unwrap(node)


def name(node):
    node = expression(node)
    return node.text() if node is not None and node.kind() in {
        "identifier", "property_identifier", "shorthand_property_identifier_pattern",
        "shorthand_property_identifier", "this"
    } else ""


def chain(node):
    """Return the AST receiver and the member names in evaluation order."""
    node = expression(node)
    names = []
    while node is not None and node.kind() == "member_expression":
        names.append(name(node.field("property")))
        node = expression(node.field("object"))
    return node, tuple(reversed(names))


def const_declaration(binding):
    return binding.parent().children()[0].kind() == "const"


def plain_array(node):
    elements = [child for child in node.children() if child.kind() != "comment"][1:-1]
    return all(child.kind() != "spread_element" and (
        child.kind() != "," or index % 2 == 1) for index, child in enumerate(elements))


def arguments(node):
    args = node.field("arguments")
    return children(args) if args is not None and args.kind() == "arguments" else []


def text_literal(node):
    if node is None:
        return None
    if node.kind() in {"string", "template_string"}:
        return node.text()[1:-1]
    return None


def literal(node):
    node = expression(node)
    if node is None:
        return lint.UNKNOWN
    kind = node.kind()
    if kind in {"null", "true", "false"}:
        return {"null": None, "true": True, "false": False}[kind]
    if kind == "number":
        raw = node.text().replace("_", "")
        if raw.endswith("n"):
            return lint.UNKNOWN
        try:
            return int(raw, 0)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return lint.UNKNOWN
    if kind == "string" or (kind == "template_string" and not any(
            child.kind() == "template_substitution" for child in children(node))):
        if not any(child.kind() == "escape_sequence" for child in children(node)):
            return text_literal(node)
    if kind == "unary_expression" and node.children()[0].kind() in {"+", "-"}:
        operand = literal(node.field("argument"))
        if type(operand) in {int, float}:
            return operand if node.children()[0].kind() == "+" else -operand
    operator, left, right = split_operands(node)
    if operator in {"===", "!==", "==", "!=", "<", ">", "<=", ">="}:
        lhs, rhs = literal(left), literal(right)
        if lhs is not lint.UNKNOWN and rhs is not lint.UNKNOWN:
            return compare(operator, lhs, rhs)
    return lint.UNKNOWN


def compare(operator, lhs, rhs):
    numeric = type(lhs) in {int, float} and type(rhs) in {int, float}
    same_type = numeric or type(lhs) is type(rhs)
    if operator in {"===", "!=="}:
        return (same_type and lhs == rhs) == (operator == "===")
    if operator in {"==", "!="} and same_type:
        return (lhs == rhs) == (operator == "==")
    if numeric or type(lhs) is type(rhs) is str:
        return {"<": lhs < rhs, ">": lhs > rhs, "<=": lhs <= rhs, ">=": lhs >= rhs}.get(operator, lint.UNKNOWN)
    return lint.UNKNOWN


def truth(node):
    node = expression(node)
    value = literal(node)
    if value is not lint.UNKNOWN:
        return bool(value)
    if node is None:
        return lint.UNKNOWN
    if node.kind() in {"array", "object", "regex", *FUNCTIONS}:
        return True
    negated = opposite(node)
    if negated is not None:
        value = truth(negated)
        return not value if value is not lint.UNKNOWN else value
    operator, left, right = split_operands(node)
    if operator in {"||", "&&"}:
        lhs, rhs = truth(left), truth(right)
        decisive = operator == "||"
        if lhs is decisive or rhs is decisive:
            return decisive
        if lhs is rhs:
            return lhs
    return lint.UNKNOWN


def equality(operator, left, right, negate):
    if operator in {"==", "!=", "===", "!=="} and negate:
        return {"==": "!=", "!=": "==", "===": "!==", "!==": "==="}[operator], left, right
    return operator, left, right


@dataclass
class Check:
    node: object
    method: str = ""
    actual: object = None
    expected: object = None
    modifiers: tuple = ()
    operator: str = ""
    predicate: object = None
    negated: bool = False
    guarded: bool = False

    @property
    def asynchronous(self):
        return self.method in {"rejects", "doesNotReject", "throwsAsync", "notThrowsAsync"} or any(
            part in {"resolves", "rejects", "poll", "element"} for part in self.modifiers)

    def constant(self):
        if self.predicate is not None and self.method in {"isTrue", "isFalse", "true", "false"}:
            return literal(self.predicate) is ((self.method in {"isTrue", "true"}) != self.negated)
        if self.operator:
            lhs, rhs = literal(self.actual), literal(self.expected)
            if lhs is not lint.UNKNOWN and rhs is not lint.UNKNOWN:
                return compare(self.operator, lhs, rhs) is True
        if self.predicate is not None:
            return truth(self.predicate) is (not self.negated)
        return False

    def observation(self):
        """Identify an observed collection or recorder and whether the check proves presence."""
        method = self.method
        negated = self.negated
        subject = expression(self.actual)
        expected = expression(self.expected)
        if method in SPY_MATCHERS:
            presence = not negated if method in {"toHaveBeenCalled", "toBeCalled"} else True if not negated else None
            return "calls", subject, presence
        if method in SPY_COUNTS:
            counter = "calls"
        elif method in {"toHaveLength", "length", "lengthOf"}:
            counter = "length"
        elif member(subject, "length") and (self.operator or method in {"toBeGreaterThan", "toBeGreaterThanOrEqual"}):
            counter = "length"
            subject = expression(subject.field("object"))
            if method in {"toBeGreaterThan", "toBeGreaterThanOrEqual"}:
                value = literal(expected)
                minimum = 0 if method == "toBeGreaterThan" else 1
                counter, subject = observation_subject(counter, subject)
                return counter, subject, True if type(value) in {int, float} and value >= minimum and not negated else None
            negated = self.operator in {"!==", "!="}
        elif (self.operator in {"===", "=="} and expected is not None
              and expected.kind() == "array" and plain_array(expected) and children(expected)):
            counter, subject = observation_subject("length", subject)
            return counter, subject, True
        else:
            return None, None, None
        value = literal(expected)
        if subject is None or type(value) not in {int, float}:
            return None, None, None
        counter, subject = observation_subject(counter, subject)
        if value == 0:
            return counter, subject, negated
        return counter, subject, True if value > 0 and not negated else None


def observation_subject(counter, subject):
    if counter == "length" and member(subject, "calls"):
        mock = expression(subject.field("object"))
        if member(mock, "mock"):
            return "calls", expression(mock.field("object"))
    return counter, subject


def member(node, property_name):
    return node is not None and node.kind() == "member_expression" and name(node.field("property")) == property_name


@dataclass
class Registration:
    kind: str
    title: str
    callback: object
    ignored: bool
    conditional: bool
    context_call: bool


class File:
    def __init__(self, syntax, path, scan_root):
        self.syntax = syntax
        self.path = path
        self.scan_root = scan_root
        self.local_imports = {}
        self.imported_returns = {}
        self.imports = {}
        self.source_imports = {}
        self.hook_values = None
        self.helper_summaries = {}
        self.namespace_imports = set()
        self.bindings = defaultdict(list)
        self.fixture_references = None
        self.destructured = defaultdict(list)
        self.registrations = {}
        self.index_imports()
        for node in query(syntax.root, "variable_declarator", "function_declaration", "generator_function_declaration"):
            key = name(node.field("name"))
            if not key:
                continue
            if node.kind() == "variable_declarator":
                self.index_require(key, node.field("value"))
            scope = next((parent for parent in node.ancestors() if parent.kind() in SCOPES), syntax.root)
            self.bindings[key].append((scope, node))
        for node in query(syntax.root, "variable_declarator"):
            if (target := node.field("name")) is not None and target.kind() == "object_pattern":
                self.index_destructured_require(target, node.field("value"))
                if not const_declaration(node):
                    continue
                scope = next((parent for parent in node.ancestors() if parent.kind() in SCOPES), syntax.root)
                for entry in children(target):
                    if entry.kind() == "pair_pattern":
                        key, local = name(entry.field("key")), name(entry.field("value"))
                    else:
                        key = local = name(entry)
                    if key and local:
                        self.destructured[local].append((scope, node, key))

    def index_imports(self):
        for node in query(self.syntax.root, "import_statement"):
            source = text_literal(node.field("source"))
            clause = next((child for child in children(node) if child.kind() == "import_clause"), None)
            if clause is None:
                continue
            if source not in FRAMEWORKS | ASSERT_MODULES:
                if source is None or any(child.kind() == "type" for child in node.children()):
                    continue
                for child in children(clause):
                    if child.kind() == "identifier":
                        self.source_imports[child.text()] = source, ("default",)
                    elif child.kind() == "namespace_import":
                        local = children(child)[0].text()
                        self.source_imports[local] = source, ()
                        if source.startswith("."):
                            self.namespace_imports.add(local)
                for specifier in query(clause, "import_specifier"):
                    if any(child.kind() == "type" for child in specifier.children()):
                        continue
                    original = name(specifier.field("name"))
                    local = name(specifier.field("alias")) or original
                    self.source_imports[local] = source, (original,)
                    if source.startswith("."):
                        self.local_imports[local] = source, original
                continue
            for child in children(clause):
                if child.kind() == "identifier":
                    self.imports[child.text()] = source, ()
                elif child.kind() == "namespace_import":
                    self.imports[children(child)[0].text()] = source, ()
                elif child.kind() == "named_imports":
                    for specifier in children(child):
                        original = name(specifier.field("name"))
                        self.imports[name(specifier.field("alias")) or original] = source, (original,)

    def require(self, node):
        base, members = chain(node)
        if base is not None and base.kind() == "await_expression":
            base = expression(next(iter(children(base)), None))
        if (base is not None and base.kind() == "call_expression"
                and base.field("function").text() in {"require", "import"}):
            source = next((text_literal(arg) for arg in arguments(base)), None)
            if source in FRAMEWORKS | ASSERT_MODULES:
                return source, members
        return "", ()

    def index_require(self, key, node):
        source, members = self.require(node)
        if source:
            self.imports[key] = source, members

    def index_destructured_require(self, target, node):
        source, members = self.require(node)
        if source:
            for entry in children(target):
                if entry.kind() == "pair_pattern":
                    self.imports[name(entry.field("value"))] = source, (*members, name(children(entry)[0]))
                elif entry.kind() == "shorthand_property_identifier_pattern":
                    self.imports[name(entry)] = source, (*members, name(entry))

    def callee(self, node):
        root, members = chain(node)
        base = name(root)
        source, original = self.imports.get(base, ("", (base,) if base else ()))
        if not original and source in {"node:test", "ava"} and (not members or members[0] in MODIFIERS):
            original = ("test",)
        if not original and source in ASSERT_MODULES:
            original = ("assert",)
        return source, (*original, *members)

    def binding(self, node):
        key = name(node)
        if not key:
            return None
        site = offset(node)
        visible = [(scope, binding) for scope, binding in self.bindings.get(key, ())
                   if offset(scope) <= site < scope.range().end.index and
                   (binding.kind() != "variable_declarator" or offset(binding) < site)]
        if visible:
            return max(visible, key=lambda item: (offset(item[0]), offset(item[1])))[1]
        return None

    def recorder(self, node):
        node = expression(node)
        if node is None:
            return None
        if node.kind() == "member_expression":
            return self.recorder(node.field("object")), name(node.field("property"))
        binding = self.binding(node)
        if (alias := self.recorder_alias(node)) is not None:
            source, member_name = alias
            if source.kind() != "call_expression":
                identity = self.recorder(source)
                return (identity, member_name) if member_name else identity
        declaration, _ = self.destructuring(node)
        if declaration is not None and (binding is None or offset(declaration) > offset(binding)):
            return name(node), offset(declaration)
        return (name(node), offset(binding)) if binding is not None else shape(node)

    def recorder_alias(self, node):
        binding = self.binding(node)
        declaration, key = self.destructuring(node)
        if declaration is None or binding is not None and offset(binding) > offset(declaration):
            declaration, key = binding, ""
        if declaration is None or declaration.kind() != "variable_declarator":
            return None
        source = expression(declaration.field("value"))
        if source is None or offset(source) >= offset(node) or not (name(source) or source.kind() == "member_expression"
                                  or key and source.kind() == "call_expression"):
            return None
        if not self.recorder_alias_stable(node, declaration, source, key):
            return None
        return source, key

    def recorder_alias_stable(self, use, declaration, source, key):
        if self.fixture_references is None:
            self.fixture_references = defaultdict(list)
            for entry in query(self.syntax.root, "identifier"):
                self.fixture_references[name(entry)].append(entry)
        source_root, path = chain(source)
        watched = [(name(use), declaration, ())]
        if name(source_root):
            watched.append((name(source_root), self.binding(source_root), (*path, key) if key else path))
        start, end = offset(declaration), offset(use)
        for identifier, binding, members in watched:
            for reference in self.fixture_references[identifier]:
                if not start < offset(reference) < end:
                    continue
                visible = self.binding(reference)
                destructured, _ = self.destructuring(reference)
                if destructured is not None and (visible is None or offset(destructured) > offset(visible)):
                    visible = destructured
                if (offset(visible) if visible is not None else None) != (
                        offset(binding) if binding is not None else None):
                    continue
                current, parent = reference, reference.parent()
                while parent.kind() in {"member_expression", *TRANSPARENT}:
                    current, parent = parent, parent.parent()
                _, observed = chain(current)
                if observed != members[:len(observed)]:
                    continue
                if parent.kind() in {"assignment_expression", "augmented_assignment_expression", "update_expression"}:
                    target = parent.field("left") or parent.field("argument")
                    if target is not None and offset(target) == offset(current):
                        return False
                if parent.kind() == "arguments":
                    callee = parent.parent().field("function")
                    if not self.expect_origin(callee) and self.assertion_call(callee, ()) is None:
                        return False
        return True

    def helper(self, node):
        binding = self.binding(node)
        if binding is not None:
            candidate = binding.field("value") if binding.kind() == "variable_declarator" else binding
            if candidate is not None and candidate.kind() in FUNCTIONS:
                return candidate
        return None

    def imported_return_type(self, node):
        key = name(node)
        if key not in self.local_imports or self.binding(node) is not None or self.scan_root is None:
            return None
        source, exported = self.local_imports[key]
        target = self.path.parent / source
        if source not in self.imported_returns:
            self.imported_returns[source] = self.read_return_types(target)
        return self.imported_returns[source].get(exported)

    def read_return_types(self, target):
        candidates = [target]
        if target.suffix in {".js", ".jsx"}:
            candidates.extend(target.with_suffix(suffix) for suffix in (".ts", ".tsx"))
        if not target.suffix:
            candidates.extend(target.with_suffix(suffix) for suffix in (".ts", ".tsx"))
            candidates.extend(target / filename for filename in ("index.ts", "index.tsx"))
        for candidate in candidates:
            language = ECMASCRIPT_LANGUAGES.get(candidate.suffix)
            if not language:
                continue
            try:
                candidate = candidate.resolve()
                if not candidate.is_relative_to(self.scan_root.resolve()) or not candidate.is_file():
                    continue
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            syntax = Syntax.parse(source, language)
            return {name(declaration.field("name")): declaration.field("return_type")
                    for entry in children(syntax.root) if entry.kind() == "export_statement"
                    for declaration in children(entry) if declaration.kind() == "function_declaration"}
        return {}

    def destructuring(self, node):
        use = offset(node)
        visible = [(declaration, key) for scope, declaration, key in self.destructured.get(name(node), ())
                   if offset(scope) <= use < scope.range().end.index and offset(declaration) < use]
        return max(visible, key=lambda item: offset(item[0]), default=(None, ""))

    def sibling_control(self, node, seen=frozenset()):
        """Identify a recorder or output that another test can observe independently."""
        node = expression(node)
        if node is None:
            return None
        kind = node.kind()
        if kind == "await_expression":
            return self.sibling_control(next(iter(children(node)), None), seen)
        if kind == "member_expression":
            root = self.sibling_control(node.field("object"), seen)
            return ("member", root, name(node.field("property"))) if root is not None else None
        if kind == "call_expression":
            callee = node.field("function")
            args = arguments(node)
            _, names = self.callee(callee)
            if names in {("vi", "spyOn"), ("jest", "spyOn")} and len(args) >= 2:
                method = literal(args[1])
                if name(args[0]) in self.namespace_imports and isinstance(method, str):
                    return "spy", name(args[0]), method
                return None
            if names in {("vi", "fn"), ("jest", "fn")}:
                return None
            method = names[-1] if names else ""
            if method.startswith("mock") and member(callee, method):
                return self.sibling_control(callee.field("object"), seen)
            detector = self.sibling_control(callee, seen)
            if method in {"querySelectorAll", "getElementsByClassName", "qa", "pw"}:
                selector = literal(args[-1]) if args else None
                return ("selector", detector or shape(callee), selector) if isinstance(selector, str) else None
            if method in {"filter", "flatMap", "map", "slice"}:
                return None
            if name(callee) and self.destructuring(callee)[0] is not None:
                return None
            if detector is None and name(callee) and self.binding(callee) is None:
                detector = "named call", name(callee)
            return ("call", detector) if detector is not None else None
        if not name(node):
            return None
        binding = self.binding(node)
        if (alias := self.recorder_alias(node)) is not None:
            source, key = alias
            root = self.sibling_control(source, seen)
            return ("member", root, key) if key and root is not None else root
        if binding is None or offset(binding) in seen:
            return None
        site = offset(binding)
        if binding.kind() == "variable_declarator" and const_declaration(binding):
            value = expression(binding.field("value"))
            if value is not None and value.kind() not in {"identifier", "member_expression"}:
                root = self.sibling_control(value, seen | {site})
                if root is not None:
                    return root
        if not any(parent.kind() in FUNCTIONS for parent in binding.ancestors()):
            return "binding", site
        return None

    def control_group(self, node, *, calls):
        node = expression(node)
        if node is None:
            return None
        if (name(node) and (binding := self.binding(node)) is not None
                and binding.kind() == "variable_declarator" and const_declaration(binding)):
            value = expression(binding.field("value"))
            if member(value, "length"):
                node = value
        if node.kind() == "member_expression":
            receiver = node.field("object")
            if name(node.field("property")) == "length":
                return self.control_group(receiver, calls=calls)
            if name(receiver) or receiver.kind() == "member_expression":
                if calls:
                    root, _ = chain(receiver)
                    binding = self.binding(root)
                    if binding is None or not any(parent.kind() in FUNCTIONS for parent in binding.ancestors()):
                        return None
                return self.recorder(node if calls else receiver)
        return None

    def indexed_collection(self, node):
        node = expression(node)
        while node is not None:
            if any(child.kind() in {"?.", "optional_chain"} for child in node.children()):
                return None
            if member(node, "length"):
                return None
            if node.kind() == "subscript_expression":
                index = literal(node.field("index"))
                return node.field("object") if type(index) is int and index >= 0 else None
            if node.kind() == "member_expression":
                node = expression(node.field("object"))
                continue
            binding = self.binding(node)
            if binding is not None and binding.kind() == "variable_declarator" and const_declaration(binding):
                node = expression(binding.field("value"))
                continue
            return None
        return None

    def fixed(self, node, supplied=None, seen=frozenset()):
        """Resolve a fixture expression together with the helper arguments it closes over."""
        node = expression(node)
        supplied = supplied or {}
        if node is None:
            return None, {}
        span = offset(node), node.range().end.index
        if span in seen:
            return node, supplied
        seen = seen | {span}
        if supplied and (owner := self.parameter_scope(node)) is not None:
            if (key := (offset(owner), name(node))) in supplied and self.fixture_unchanged(node, owner, parameter=True):
                return supplied[key]
            return node, supplied
        if (binding := self.binding(node)) is not None and binding.kind() == "variable_declarator":
            if const_declaration(binding) and self.fixture_unchanged(node, binding):
                return self.fixed(binding.field("value"), supplied, seen)
        if node.kind() in {"member_expression", "subscript_expression"}:
            receiver, bindings = self.fixed(node.field("object"), supplied, seen)
            if receiver is not None and receiver.kind() == "object":
                key = name(node.field("property")) or text_literal(node.field("index"))
                item = self.object_field(receiver, key, bindings, seen)
                if item is not None:
                    return item
            if receiver is not None and receiver.kind() == "array":
                index = literal(node.field("index"))
                values = children(receiver)
                if type(index) is int and 0 <= index < len(values) and plain_array(receiver):
                    return self.fixed(values[index], bindings, seen)
        if node.kind() == "call_expression":
            callee = node.field("function")
            if not any(child.kind() == "?." for child in node.children()) and (target := self.helper(callee)) is not None:
                body = expression(target.field("body"))
                if body is not None and body.kind() == "statement_block":
                    statements = children(body)
                    body = next(iter(children(statements[0])), None) if (
                        len(statements) == 1 and statements[0].kind() == "return_statement") else None
                if body is not None:
                    arguments_by_name = {**supplied, **self.fixture_arguments(target, arguments(node), supplied, seen)}
                    return self.fixed(body, arguments_by_name, seen)
        return node, supplied

    def parameter_scope(self, node):
        key = name(node)
        if not key:
            return None
        owner = next((parent for parent in node.ancestors()
                      if parent.kind() in FUNCTIONS and key in formal_parameters(parent)), None)
        binding = self.binding(node) if owner is not None else None
        return owner if binding is None or offset(binding) < offset(owner) else None

    def fixture_arguments(self, target, args, supplied, seen=frozenset()):
        return {(offset(target), key): self.fixed(arg, supplied, seen)
                for key, arg in zip(formal_parameters(target), args) if key}

    def object_field(self, node, key, supplied, seen):
        if node is None or node.kind() != "object":
            return None
        span = offset(node), node.range().end.index
        if span in seen:
            return None
        seen = seen | {span}
        for field in reversed(children(node)):
            if field.kind() == "pair" and (name(field.field("key")) or text_literal(field.field("key"))) == key:
                return self.fixed(field.field("value"), supplied, seen)
            if field.kind() == "spread_element":
                spread, bindings = self.fixed(children(field)[0], supplied, seen)
                return self.object_field(spread, key, bindings, seen) if spread is not None else None
        return None

    def fixture_unchanged(self, use, binding, *, parameter=False):
        key = name(use)
        if self.fixture_references is None:
            self.fixture_references = defaultdict(list)
            for reference in query(self.syntax.root, "identifier"):
                self.fixture_references[name(reference)].append(reference)
        for reference in self.fixture_references[key]:
            if not offset(binding) < offset(reference) < offset(use):
                continue
            owner = self.parameter_scope(reference) if parameter else self.binding(reference)
            if owner is None or offset(owner) != offset(binding):
                continue
            current, parent = reference, reference.parent()
            while parent.kind() in {"member_expression", "subscript_expression", *TRANSPARENT}:
                current, parent = parent, parent.parent()
            if parent.kind() == "arguments":
                function = parent.parent().field("function")
                receiver, methods = chain(function)
                if self.callee(function)[1] in EXPECT_RECEIVERS or (
                        name(receiver) == "Object" and methods in {("keys",), ("values",), ("entries",)}
                        and self.binding(receiver) is None and self.parameter_scope(receiver) is None):
                    continue
            if parent.kind() in {"assignment_expression", "augmented_assignment_expression", "update_expression",
                                 "arguments", "unary_expression", "return_statement", "variable_declarator",
                                 "pair", "array", "spread_element"}:
                return False
            if parent.kind() == "call_expression" and offset(parent.field("function")) == offset(current):
                if name(current.field("property")) not in {"map", "filter", "forEach", "every", "some", "find",
                                                          "includes", "join", "slice", "entries", "values", "keys"}:
                    return False
        return True

    def fixture_number(self, node, supplied=None, seen=frozenset()):
        node, bindings = self.fixed(node, supplied)
        if node is None:
            return lint.UNKNOWN
        span = offset(node), node.range().end.index
        if span in seen:
            return lint.UNKNOWN
        seen = seen | {span}
        if member(node, "length"):
            return self.fixture_size(node.field("object"), bindings, seen)
        operator, base, exponent = split_operands(node)
        if operator == "**":
            base = literal(base)
            exponent = self.fixture_number(exponent, bindings, seen)
            if type(base) is int and 0 <= base <= 2**16 and type(exponent) is int and 0 <= exponent <= 32:
                return base ** exponent
        return literal(node)

    def fixture_size(self, node, supplied=None, seen=frozenset()):
        node, bindings = self.fixed(node, supplied)
        if node is None:
            return lint.UNKNOWN
        span = offset(node), node.range().end.index
        if span in seen:
            return lint.UNKNOWN
        seen = seen | {span}
        kind = node.kind()
        value = literal(node)
        if isinstance(value, str):
            return len(value.encode("utf-16-le")) // 2
        if kind == "array" and plain_array(node):
            return len(children(node))
        if kind != "call_expression" or any(child.kind() == "?." for child in node.children()):
            return lint.UNKNOWN
        function = expression(node.field("function"))
        receiver, methods = chain(function)
        args = arguments(node)
        if name(receiver) == "Array" and methods == ("from",) and args and self.binding(receiver) is None:
            source, bindings = self.fixed(args[0], bindings)
            if source is not None and source.kind() == "object":
                length = self.object_field(source, "length", bindings, frozenset())
                if length is not None:
                    size = self.fixture_number(*length, seen=seen)
                    if type(size) is int and 0 <= size <= 2**32 - 1:
                        return size
            return self.fixture_size(source, bindings, seen)
        method = methods[-1] if methods else ""
        if method in {"map", "sort", "toReversed", "toSorted"} or method == "slice" and not args:
            return self.fixture_size(function.field("object"), bindings, seen)
        return lint.UNKNOWN

    def constant_length(self, check):
        if check.method in {"toHaveLength", "length", "lengthOf"}:
            actual = self.fixture_size(check.actual)
            expected = literal(check.expected)
            return type(actual) is int and type(expected) is int and (actual == expected) != check.negated
        if check.operator and member(expression(check.actual), "length"):
            actual = self.fixture_number(check.actual)
            expected = literal(check.expected)
            return actual is not lint.UNKNOWN and expected is not lint.UNKNOWN and compare(check.operator, actual, expected) is True
        return False

    def registration(self, node, context):
        callee = expression(node.field("function"))
        factory = callee is not None and callee.kind() == "call_expression"
        if factory:
            callee = callee.field("function")
        _, names = self.callee(callee)
        if names[:2] == ("test", "describe"):
            names = names[1:]
        context_call = len(names) >= 2 and names[0] in context and names[1] == "test"
        if context_call:
            names = names[1:]
        if not names:
            return None
        kind, *modifiers = names
        if kind in {"fit", "xit", "xtest", "fdescribe", "xdescribe"}:
            if kind.startswith("x"):
                modifiers.append("skip")
            kind = kind[1:]
        if (kind not in TESTS | SUITES or not all(modifier in MODIFIERS for modifier in modifiers)
                or factory != bool(FACTORIES.intersection(modifiers))):
            return None
        args = arguments(node)
        title = next((title for arg in args if (title := text_literal(arg)) is not None), None)
        callback = next((arg for arg in reversed(args) if arg.kind() in FUNCTIONS), None)
        if callback is None and args and "todo" not in modifiers and "skip" not in modifiers:
            candidate = self.binding(args[-1])
            if candidate is not None:
                callback = candidate.field("value") if candidate.kind() == "variable_declarator" else candidate
                if callback is not None and callback.kind() not in FUNCTIONS:
                    callback = None
        ignored = bool({"skip", "todo", "fixme"}.intersection(modifiers))
        for arg in args:
            if arg.kind() == "object":
                ignored |= any(child.kind() == "pair" and name(child.field("key")) in {"skip", "todo"}
                               and truth(child.field("value")) is True for child in children(arg))
        if title is None or callback is None and not ignored:
            return None
        return Registration(kind, title, callback, ignored, bool({"runIf", "skipIf"}.intersection(modifiers)),
                            context_call)

    def inventory(self):
        for node in query(self.syntax.root, "call_expression"):
            parents = []
            context = ()
            for ancestor in node.ancestors():
                registration = self.registrations.get(offset(ancestor)) if ancestor.kind() == "call_expression" else None
                if (registration is not None and registration.callback is not None
                        and offset(registration.callback) <= offset(node) < registration.callback.range().end.index):
                    parents.append(registration)
                    if not context and registration.kind in TESTS:
                        context = parameters(registration.callback)
            registration = self.registration(node, context)
            if registration is None:
                continue
            self.registrations[offset(node)] = registration
            if registration.kind in SUITES:
                continue
            callback = registration.callback
            body = callback.field("body") if callback is not None else None
            span = (body or node).range()
            sites = tuple(parent.title for parent in reversed(parents))
            qualified = "::".join((*sites, registration.title))
            test = Test(registration.title, registration.title, line(node), span.start.line + 1,
                        span.end.line + 1, span.start.column, qualified=qualified,
                        span_columns=(span.start.column, span.end.column))
            test.conditional = registration.conditional or any(parent.conditional for parent in parents)
            ignored = registration.ignored or any(parent.ignored for parent in parents)
            yield test, callback, ignored

    def assertion_target(self, callee):
        root, _ = chain(callee)
        source, names = self.callee(callee)
        binding = self.binding(root)
        if self.parameter_scope(root) is not None or name(root) in self.source_imports:
            return "", ()
        if binding is not None and (binding.kind() != "variable_declarator"
                                    or self.require(binding.field("value"))[0] != source or not source):
            return "", ()
        return source, names

    def expect_origin(self, callee):
        source, names = self.assertion_target(callee)
        if names in EXPECT_RECEIVERS and (not source or source in EXPECT_MODULES):
            if names != ("expect", "soft") or source in {"", "vitest", "vite-plus/test",
                                                         "@playwright/test", "playwright/test", "@effect/vitest"}:
                return names
        return ()

    def assertion_call(self, callee, context):
        source, names = self.assertion_target(callee)
        if source in ASSERT_MODULES:
            if names[:2] == ("assert", "strict"):
                names = ("assert", *names[2:])
            elif names[:1] == ("strict",):
                names = ("assert", *names[1:])
            method = names[-1] if names else ""
            if len(names) <= 2 and method in NODE_ASSERT_CHECKS:
                return method
        if names and source in {"chai", "vitest", "vite-plus/test", "@alchemy.run/test", "@effect/vitest"} and names[0] in {"assert", "should"}:
            return names[-1] if names != ("should",) else None
        if names in {("expect", "assertions"), ("expect", "hasAssertions"), ("expect", "fail")}:
            return names[-1]
        if names and source == "fast-check" and names[-1] == "assert":
            return "assert"
        if names and not source and names[0] in {"assert", "should"}:
            return names[-1]
        root, members = chain(callee)
        if name(root) in context and (members[:1] == ("assert",) or
                                      len(members) == 1 and members[-1] in CONTEXT_CHECKS):
            return members[-1]
        return None

    def check(self, node, context):
        callee = node.field("function")
        args = arguments(node)
        base, members = chain(callee)
        if base is None:
            return None
        if members and base.kind() == "call_expression":
            origin = self.expect_origin(base.field("function"))
            if origin and (subject := arguments(base)):
                check = matcher(node, subject[0], members, args)
                check.modifiers = (*origin[1:], *check.modifiers)
                return check
        if "should" in members and members[-1] != "should":
            index = members.index("should")
            subject = expression(callee)
            for _ in members[index:]:
                subject = expression(subject.field("object"))
            return matcher(node, subject, members[index + 1:], args)
        method = self.assertion_call(callee, context)
        return assert_check(node, method, args) if method is not None else None

    def property_check(self, node):
        parent = node.parent()
        if parent is not None and (
                parent.kind() == "member_expression" or parent.kind() == "call_expression"
                and offset(parent.field("function")) == offset(node)):
            return None
        base, members = chain(node)
        if not members or members[-1] not in {
                "ok", "true", "false", "null", "undefined", "exist", "exists", "empty", "called", "calledOnce"}:
            return None
        if "should" in members or base is not None and base.kind() == "call_expression" and (
                self.expect_origin(base.field("function")) and arguments(base)):
            subject = (arguments(base)[0] if base is not None and base.kind() == "call_expression"
                       and arguments(base) and "should" not in members else base)
            return matcher(node, subject, members, [])
        return None


def formal_parameters(callback):
    formal = callback.field("parameters")
    entries = children(formal) if formal is not None else [callback.field("parameter")]
    return tuple(name(entry.field("pattern") or entry) if entry is not None else "" for entry in entries)


def parameters(callback):
    return tuple(key for key in formal_parameters(callback)[:1] if key)


def matcher(node, subject, members, args):
    if not members:
        return Check(node)
    method = members[-1]
    negated = "not" in members[:-1]
    expected = args[0] if args else None
    operator = "===" if method in ASSERT_EQUAL else "!=" if method in ASSERT_UNEQUAL else ""
    operator, _, _ = equality(operator, subject, expected, negated) if expected is not None else ("", None, None)
    predicate = subject if method in {"toBeTruthy", "toBeFalsy", "ok", "true", "false"} else None
    if method == "toBeFalsy":
        negated = not negated
    return Check(node, method, subject, expected, members[:-1], operator, predicate, negated)


def assert_check(node, method, args):
    if method in {"doesNotThrow", "doesNotReject", "notThrows", "notThrowsAsync"} and args:
        return Check(node, method, args[0])
    if len(args) >= 2 and method in ASSERT_EQUAL | ASSERT_UNEQUAL and method != "isTrue":
        operator = {"equal": "==", "notEqual": "!=", "deepEqual": "==",
                    "deepStrictEqual": "===", "strictEqual": "==="}.get(
                        method, "===" if method in ASSERT_EQUAL else "!==")
        return Check(node, method, *args[:2], operator=operator)
    if method == "snapshot" and args:
        return Check(node, method, args[0])
    predicate = args[0] if args and method in ASSERT_PREDICATES else None
    operator, left, right = split_operands(expression(predicate))
    negated = method in {"falsy", "isNotOk", "isFalse", "false"}
    if operator in {"==", "!=", "===", "!=="}:
        operator, left, right = equality(operator, left, right, negated)
        return Check(node, method, left, right, operator=operator)
    return Check(node, method, predicate=predicate, negated=negated)


def comments(syntax):
    for node in query(syntax.root, "comment"):
        raw = node.text()
        loc = node.range().start
        start = syntax.source.rfind("\n", 0, loc.index) + 1
        standalone = not syntax.source[start:loc.index].strip()
        content = raw[2:-2] if raw.startswith("/*") else raw[2:]
        for index, entry in enumerate(content.splitlines()):
            text = entry.strip().removeprefix("*").strip().removeprefix("#").strip()
            if text:
                yield Comment(loc.line + index + 1, loc.column if not index else len(entry) - len(entry.lstrip()),
                              text, standalone or index > 0)


def string_needle(node):
    node = expression(node)
    return node is not None and (
        node.kind() == "string" or node.kind() == "template_string" and
        not any(child.kind() == "template_substitution" for child in children(node))
    )


def collection_type(annotation):
    if annotation is None:
        return False
    kind = annotation.kind()
    if kind in {"array_type", "tuple_type"}:
        return True
    if kind in {"type_annotation", "readonly_type", "parenthesized_type"}:
        return any(collection_type(child) for child in children(annotation))
    if kind == "generic_type":
        return text(annotation.field("name")) in {"Array", "ReadonlyArray", "Set", "ReadonlySet", "Map", "ReadonlyMap"}
    return False


def membership_collection(file, node):
    binding = file.binding(node)
    if binding is not None and collection_type(binding.field("type")):
        return True
    value = expression(binding.field("value")) if binding is not None and binding.kind() == "variable_declarator" else node
    if value is not None and value.kind() == "call_expression":
        callee = value.field("function")
        helper = file.helper(callee)
        annotation = helper.field("return_type") if helper is not None else file.imported_return_type(callee)
        if collection_type(annotation):
            return True
    fixed, _ = file.fixed(node)
    if fixed is None:
        return False
    if fixed.kind() == "array":
        return True
    if fixed.kind() == "new_expression":
        constructor = fixed.field("constructor")
        return name(constructor) in {"Array", "Set", "Map"} and file.binding(constructor) is None
    if fixed.kind() != "call_expression":
        return False
    receiver, methods = chain(fixed.field("function"))
    return (name(receiver) in {"Array", "Object"} and methods in {
                ("from",), ("keys",), ("values",), ("entries",),
            } or methods[-1:] in {("split",), ("map",), ("flatMap",), ("filter",)})


def string_fixture(file, node):
    node = expression(node)
    while (binding := file.binding(node)) is not None and binding.kind() == "variable_declarator":
        if not const_declaration(binding) or not file.fixture_unchanged(node, binding):
            return None
        node = expression(binding.field("value"))
    return node if string_needle(node) else None


def constant_membership(file, check):
    if check.method not in {"toContain", "toMatch"} or not string_needle(check.expected):
        return False
    haystack, needle = literal(string_fixture(file, check.actual)), literal(check.expected)
    return isinstance(haystack, str) and isinstance(needle, str) and (needle in haystack) != check.negated


def micro_snapshot(file, test, checks):
    reads = micro.Reads(check.node for check in checks)
    fragments = []
    whole = []

    def key(node, subject):
        identity = file.recorder(subject)
        position = reads.assignment(node, subject.text() if subject is not None else "")
        return reads.key(node, identity, position, reread=micro.has_call(subject))

    for check in checks:
        actual = expression(check.actual)
        if actual is None:
            continue
        if check.method in {"toContain", "toMatch"} and string_needle(check.expected):
            binding = file.binding(actual)
            initial = expression(binding.field("value")) if binding is not None else actual
            if (string_fixture(file, actual) is not None or check.method == "toContain" and membership_collection(file, actual)
                    or micro.excluded(text(actual), text(initial) if initial is not None else "", test.name)
                    or "rejects" in check.modifiers):
                continue
            fragments.append(micro.Fragment(location(check.node), text(actual), key(check.node, actual),
                                            text(check.expected), check.negated))
        elif check.operator in {"==", "==="}:
            whole.extend((check.node, subject) for subject in (actual, expression(check.expected))
                         if subject is not None)
        elif not check.negated and check.method in {"snapshot", "toMatchSnapshot", "toMatchInlineSnapshot",
                                                    "toMatchFileSnapshot"}:
            whole.append((check.node, actual))
    micro.report(test, fragments, whole, key)


def completion_check(flow, check):
    from .ast_grep_ts_flow import Flow

    matched = (check.method in {"doesNotThrow", "doesNotReject", "notThrows", "notThrowsAsync"}
               or check.method in {"toThrow", "toThrowError"} and check.modifiers == ("not",)
               and check.expected is None)
    subject = expression(check.actual)
    if (not matched or check.guarded or subject is None
            or subject.kind() not in FUNCTIONS | {"identifier", "member_expression"}
            and check.method != "doesNotReject"):
        return False
    callback = flow.callback_target(subject)
    if callback is None:
        return True
    nested = Flow(flow.file, callback, flow.context, flow.active)
    nested.visit(nested.body)
    return not nested.checks and not query(nested.body, "catch_clause")


def no_crash_test(flow, test, notes):
    if flow.checks:
        return all(completion_check(flow, check) for check in flow.checks)
    body = flow.body
    if not lint.claims_completion(test, notes) or query(body, "catch_clause"):
        return False
    steps = children(body) if body.kind() == "statement_block" else [body]
    for step in steps:
        target = next(iter(children(step)), None) if step.kind() == "expression_statement" else step
        if target is not None and target.kind() == "await_expression":
            target = next(iter(children(target)), None)
        if target is not None and target.kind() == "call_expression" and not flow.optional(target):
            return True
    return False


def parse(source, path, language, *, scan_root=None, inventory=False, observe=None):
    from .ast_grep_ts_flow import Flow
    from .ast_grep_ts_recorders import fixture_origin

    syntax = Syntax.parse(source, language)
    file = File(syntax, path, scan_root)
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    entries = list(file.inventory())
    tests = []
    sibling_witnesses = set()
    test_absences = []
    for test, callback, ignored in entries:
        tests.append(test)
        if ignored or callback is None:
            continue
        body = callback.field("body")
        if body is None:
            continue
        flow = Flow(file, callback, parameters(callback))
        incomplete = flow.visit(body)
        micro_snapshot(file, test, flow.checks)
        test.checks = len(flow.checks)
        lint.record_completion(test, not flow.checks, no_crash_test(flow, test, notes))
        test.guarded = bool(flow.checks) and bool(incomplete)
        if inventory:
            test.body_hash = hashlib.sha256(repr(shape(body)).encode()).hexdigest()
        witnesses = set()
        groups = set()
        absences = []
        for check in flow.checks:
            site = line(check.node)
            if flow.compares_itself(check, test.name, line_comments):
                test.issues.append((site, "self-compare"))
            if check.constant() or file.constant_length(check) or constant_membership(file, check):
                test.issues.append((site, "const-truth"))
                continue
            counter, subject, presence = check.observation()
            expected = literal(check.expected)
            if not check.guarded and check.operator in {"===", "=="} and expected is not lint.UNKNOWN and expected is not None:
                indexed = file.indexed_collection(check.actual)
                if indexed is not None:
                    witnesses.add(("length", file.recorder(indexed)))
                    if (group := file.control_group(indexed, calls=False)) is not None:
                        groups.add(("length", group))
            if presence is None:
                continue
            local = counter, file.recorder(subject)
            sibling = fixture_origin(file, subject) or file.sibling_control(subject)
            sibling = (counter, sibling) if sibling is not None else None
            group = file.control_group(subject, calls=counter == "calls")
            if presence is True and not check.guarded:
                if counter == "calls" and (collection := file.indexed_collection(subject)) is not None:
                    witnesses.add(("length", file.recorder(collection)))
                    if not test.conditional and (origin := fixture_origin(file, collection)) is not None:
                        sibling_witnesses.add(("length", origin))
                witnesses.add(local)
                if sibling is not None and not test.conditional:
                    sibling_witnesses.add(sibling)
                if group is not None:
                    groups.add((counter, group))
            elif presence is False:
                absences.append((local, sibling, group, site))
        for local, sibling, group, site in absences:
            if local not in witnesses and (group is None or (local[0], group) not in groups):
                test_absences.append((test, sibling, site))
    for test, sibling, site in test_absences:
        if sibling not in sibling_witnesses:
            test.issues.append((site, "absence-no-control"))
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
