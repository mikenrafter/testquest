"""Discover and analyze ECMAScript test callbacks through ast-grep."""

from collections import defaultdict
from dataclasses import dataclass
import hashlib

from . import lint
from .ast_grep import Syntax, children, line, offset, opposite, query, shape, split_operands, syntax_errors, unwrap
from .source import Comment, Test


FUNCTIONS = {"arrow_function", "function_expression", "function_declaration", "generator_function",
             "generator_function_declaration", "method_definition"}
SCOPES = FUNCTIONS | {"statement_block", "program"}
SUITES = {"describe", "suite", "context"}
TESTS = {"test", "it", "specify"}
MODIFIERS = {"each", "only", "skip", "todo", "concurrent", "serial", "parallel", "failing", "fixme", "skipIf", "runIf", "fails"}
FACTORIES = {"each", "skipIf", "runIf"}
FRAMEWORKS = {"vitest", "jest", "@jest/globals", "node:test", "mocha", "@playwright/test", "playwright/test",
              "bun:test", "ava", "chai", "fast-check"}
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
        "identifier", "property_identifier", "shorthand_property_identifier_pattern", "this"
    } else ""


def chain(node):
    """Return the AST receiver and the member names in evaluation order."""
    node = expression(node)
    names = []
    while node is not None and node.kind() == "member_expression":
        names.append(name(node.field("property")))
        node = expression(node.field("object"))
    return node, tuple(reversed(names))


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
            part in {"resolves", "rejects", "poll"} for part in self.modifiers)

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

    def observation(self, identity):
        """Identify the observed recorder and whether the check proves presence."""
        method = self.method
        negated = self.negated
        subject = expression(self.actual)
        if method in SPY_MATCHERS:
            presence = not negated if method in {"toHaveBeenCalled", "toBeCalled"} else True if not negated else None
            return ("calls", identity(subject)), presence
        if method in SPY_COUNTS:
            counter = "calls"
        elif method in {"toHaveLength", "length", "lengthOf"}:
            counter = "length"
        elif self.operator in {"===", "==", "!==", "!="} and member(subject, "length"):
            counter = "length"
            subject = expression(subject.field("object"))
            negated = self.operator in {"!==", "!="}
        else:
            return None, None
        value = literal(self.expected)
        if subject is None or subject.kind() in {"array", "string", "template_string"} or type(value) not in {int, float}:
            return None, None
        if counter == "length" and member(subject, "calls"):
            recorder = expression(subject.field("object"))
            if member(recorder, "mock"):
                counter, subject = "calls", expression(recorder.field("object"))
        if value == 0:
            return (counter, identity(subject)), negated
        return (counter, identity(subject)), True if value > 0 and not negated else None


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
    def __init__(self, syntax):
        self.syntax = syntax
        self.imports = {}
        self.imported_helpers = set()
        self.bindings = defaultdict(list)
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

    def index_imports(self):
        for node in query(self.syntax.root, "import_statement"):
            source = text_literal(node.field("source"))
            clause = next((child for child in children(node) if child.kind() == "import_clause"), None)
            if clause is None:
                continue
            if source not in FRAMEWORKS | ASSERT_MODULES:
                if source is not None and source.startswith("."):
                    for specifier in query(clause, "import_specifier"):
                        original = name(specifier.field("name"))
                        if lint.helper(original):
                            self.imported_helpers.add(name(specifier.field("alias")) or original)
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
        if base is not None and base.kind() == "call_expression" and name(base.field("function")) == "require":
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
        if (binding := self.binding(node)) is not None:
            if binding.kind() == "variable_declarator" and binding.parent().children()[0].kind() == "const":
                value = expression(binding.field("value"))
                if name(value):
                    return self.recorder(value)
            return name(node), offset(binding)
        return shape(node)

    def helper(self, node):
        binding = self.binding(node)
        if binding is not None:
            candidate = binding.field("value") if binding.kind() == "variable_declarator" else binding
            if candidate is not None and candidate.kind() in FUNCTIONS:
                return candidate
        return None

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

    def check(self, node, context):
        callee = node.field("function")
        source, names = self.callee(callee)
        args = arguments(node)
        base, members = chain(callee)
        if base is None:
            return None
        if members and base.kind() == "call_expression":
            _, origin = self.callee(base.field("function"))
            if origin in {("expect",), ("expect", "soft"), ("expect", "poll")} and (subject := arguments(base)):
                check = matcher(node, subject[0], members, args)
                if origin == ("expect", "poll"):
                    check.modifiers = ("poll", *check.modifiers)
                return check
        if "should" in members and members[-1] != "should":
            index = members.index("should")
            subject = expression(callee)
            for _ in members[index:]:
                subject = expression(subject.field("object"))
            return matcher(node, subject, members[index + 1:], args)
        if not names:
            return None
        if names in {("expect", "assertions"), ("expect", "hasAssertions")}:
            return Check(node, names[-1])
        receiver, *methods = names
        local = self.binding(base) if name(base) and not source else None
        if (receiver == "assert" and local is None or source in ASSERT_MODULES
                or source == "fast-check" and names[-1] == "assert"
                or receiver == "should" and (source == "chai" or local is None)
                or receiver in context and (methods[:1] == ["assert"] or
                                            len(methods) == 1 and methods[-1] in CONTEXT_CHECKS)):
            method = methods[-1] if methods else receiver
            return assert_check(node, method, args)
        if name(base) in self.imported_helpers and not members and local is None:
            return Check(node, "imported helper")
        return None

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
                self.callee(base.field("function"))[1] == ("expect",) and arguments(base)):
            subject = (arguments(base)[0] if base is not None and base.kind() == "call_expression"
                       and arguments(base) and "should" not in members else base)
            return matcher(node, subject, members, [])
        return None


def parameters(callback):
    if callback is None:
        return ()
    formal = callback.field("parameters")
    entries = children(formal) if formal is not None else [callback.field("parameter")]
    names = []
    for entry in entries[:1]:
        if entry is not None and entry.kind() in {"required_parameter", "optional_parameter"}:
            entry = children(entry)[0]
        if name(entry):
            names.append(name(entry))
    return tuple(names)


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
    if len(args) >= 2 and method in ASSERT_EQUAL | ASSERT_UNEQUAL and method != "isTrue":
        operator = {"equal": "==", "notEqual": "!=", "deepEqual": "==",
                    "deepStrictEqual": "===", "strictEqual": "==="}.get(
                        method, "===" if method in ASSERT_EQUAL else "!==")
        return Check(node, method, *args[:2], operator=operator)
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


def parse(source, path, language, *, inventory=False, observe=None):
    from .ast_grep_ts_flow import Flow

    syntax = Syntax.parse(source, language)
    file = File(syntax)
    notes = list(comments(syntax))
    line_comments = {comment.line: comment.text for comment in notes}
    entries = list(file.inventory())
    tests = []
    for test, callback, ignored in entries:
        tests.append(test)
        if ignored or callback is None:
            continue
        body = callback.field("body")
        if body is None:
            continue
        flow = Flow(file, callback, parameters(callback))
        incomplete = flow.visit(body)
        test.checks = len(flow.checks)
        if not flow.checks:
            test.issues.append((test.line, "no-assert"))
        test.guarded = bool(flow.checks) and bool(incomplete)
        if inventory:
            test.body_hash = hashlib.sha256(repr(shape(body)).encode()).hexdigest()
        witnesses = set()
        absences = []
        for check in flow.checks:
            site = line(check.node)
            if check.constant():
                test.issues.append((site, "const-truth"))
            if flow.compares_itself(check, test.name, line_comments):
                test.issues.append((site, "self-compare"))
            key, presence = check.observation(file.recorder)
            if presence is True and not check.guarded:
                witnesses.add(key)
            if presence is False:
                absences.append((key, site))
        test.issues.extend((site, "absence-no-control") for key, site in absences if key not in witnesses)
    if observe is not None:
        observe(path, tests, syntax_errors(syntax.root, missing=True), [])
    return tests, notes
