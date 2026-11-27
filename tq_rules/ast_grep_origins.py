"""Reaching definitions and value facts for simple local control flow."""

from dataclasses import dataclass, field

from . import lint
from .ast_grep import CALLS, SCOPES, children, offset, parts, query, unwrap


DECLARATIONS = {"assignment", "assignment_expression", "let_declaration", "variable_declarator",
                "property_declaration"}
WRITES = {"augmented_assignment", "compound_assignment_expr", "update_expression", "delete_statement",
          "postfix_expression"}
IF = {"if_statement", "if_expression"}
LOOPS = {"for_statement", "enhanced_for_statement", "for_in_statement", "for_expression",
         "while_statement", "while_expression", "loop_expression", "do_statement"}
UNMODELLED = {"try_statement", "try_expression", "try_with_resources_statement", "match_statement",
              "match_expression", "when_expression", "switch_expression", "with_statement",
              "conditional_expression", "ternary_expression"}
SCOPED = SCOPES | {"function_declaration", "method_declaration", "lambda_expression",
                   "lambda_literal", "arrow_function", "function_expression", "class_declaration"}
CALL_KINDS = CALLS | {"method_invocation"}
COLLECTIONS = {"list", "tuple", "array_expression", "array_initializer"}
ACCESSORS = {"clone", "copy", "len", "get", "as_ref", "as_str", "as_slice", "to_vec",
             "to_owned", "to_string", "iter", "into_iter", "collect", "is_empty"}
SETUP_CALLS = {"fixture", "load_fixture", "read_fixture", "tempdir", "mock", "spy", "stub", "buildString",
               "len", "range", "sorted", "dict", "set", "list", "tuple", "bytes", "int", "str"}


@dataclass(frozen=True)
class Origin:
    kind: str
    # The source offset identifies an invocation even when both operands use aliases.
    site: int = -1
    value: object = lint.UNKNOWN
    source: object = field(default=None, compare=False)


@dataclass(frozen=True)
class Fact:
    origin: Origin | None = None
    minimum: int | None = None


@dataclass
class Paths:
    facts: dict
    joined: set

    def copy(self):
        return Paths(self.facts.copy(), self.joined.copy())

    def bind(self, name, fact):
        self.facts[name] = fact
        self.joined.discard(name)


def merge_facts(facts):
    origins = [fact.origin for fact in facts]
    minimums = [fact.minimum for fact in facts]
    same = all(origin == origins[0] for origin in origins)
    boolean = origins[0].value if origins[0] is not None else None
    same_boolean = type(boolean) is bool and all(
        origin is not None and origin.kind == "literal" and origin.value is boolean
        for origin in origins
    )
    origin = origins[0] if same else Origin("literal", value=boolean) if same_boolean else None
    return Fact(origin, min(minimums) if None not in minimums else None)


def name_of(node):
    if node is not None and node.kind() in {"identifier", "simple_identifier"}:
        return node.text().removeprefix("r#")
    return None


def declaration(node):
    kotlin = node.kind() == "property_declaration"
    if kotlin:
        entries = children(node)
        variable = next((child for child in entries if child.kind() == "variable_declaration"), None)
        if variable is not None:
            return name_of(next(iter(children(variable)), None)), (
                entries[-1] if entries[-1] != variable and node.field("delegate") is None else None
            )
    left = node.field("left")
    if node.kind() == "assignment" and left is None:
        entries = children(node)
        if len(entries) == 2 and entries[0].kind() == "directly_assignable_expression":
            target = children(entries[0])
            if len(target) == 1:
                simple = any(token.kind() == "=" for token in node.children())
                return name_of(target[0]), entries[1] if simple else None
    if node.kind() == "assignment_expression" and left is not None and not any(
        token.kind() == "=" for token in node.children()
    ):
        return name_of(left), None
    name = left or node.field("pattern") or node.field("name")
    return name_of(name), node.field("right") or node.field("value")


def direct_return(body):
    while body is not None and body.kind() in {"block", "function_body", "statements", "expression_statement"}:
        entries = children(body)
        if len(entries) != 1:
            return None
        body = entries[0]
    if body is not None and body.kind() in {"return_statement", "return_expression"}:
        return next(iter(children(body)), None)
    return body


class ReachingFacts:
    """Track simple local definitions. A missing origin or minimum conveys no proof."""

    def __init__(self, syntax, body, literal, *, call=None, helper=None, captures=()):
        self.syntax = syntax
        self.literal = literal
        self.call = call or (lambda node: stage_call(syntax, node))
        self.helper = helper
        self.captures = set(captures)
        self.incoming = {}
        self.branched = {}
        self.suspended = False
        self.nonlocals = {
            identifier.text() for declaration in query(body, "nonlocal_statement") for identifier in children(declaration)
        } if syntax.language == "python" else set()
        self.visit(body, Paths({}, set()))

    def origin(self, node, paths, *, bound=None, seen=frozenset()):
        node = unwrap(node)
        if node is None:
            return None
        value = self.literal(self.syntax, node)
        if value is not lint.UNKNOWN:
            return Origin("literal", offset(node), value if type(value) is bool else lint.literal_key(value))
        kind = node.kind()
        if name := name_of(node):
            if name in self.nonlocals:
                return None
            if name in paths.facts:
                return paths.facts[name].origin
            return Origin("fixture")
        if kind in {"attribute", "field_expression", "member_expression", "navigation_expression",
                    "subscript", "index_expression", "reference_expression", "await_expression"}:
            base = node.field("object") or node.field("value") or next(iter(children(node)), None)
            return self.origin(base, paths, bound=bound, seen=seen)
        if kind not in CALL_KINDS:
            return None
        call = self.call(node)
        if call is None:
            return None
        name, receiver, args = call
        if not name:
            return None
        if receiver is not None and name[-1] in ACCESSORS:
            return self.origin(receiver, paths, bound=bound, seen=seen)
        if name[-1] in SETUP_CALLS or any(part in {"fixtures", "test_support", "mockito"} for part in name):
            return Origin("fixture")
        if len(name) == 1 and name[0] in paths.facts:
            return None
        helper = self.helper(node, name) if self.helper is not None else None
        if helper is not None:
            function, parameters, result = helper
            site = offset(function)
            if result is not None and len(parameters) == len(args) and site not in seen and None not in parameters:
                arguments = {parameter: Fact(self.origin(arg, paths, bound=bound, seen=seen))
                             for parameter, arg in zip(parameters, args)}
                origin = self.origin(result, Paths(arguments, set()), bound=self.syntax.absolute(node) if bound is None else bound,
                                     seen=seen | {site})
                if origin is not None:
                    return origin
        return Origin("product", self.syntax.absolute(node) if bound is None else bound, source=node)

    def eventual_origin(self, node, site):
        paths = self.incoming.get(site)
        return self.origin(node, paths) if paths is not None else None

    def path_minimum(self, name, site):
        paths = self.branched.get(site)
        if paths is not None and name in paths.joined:
            return True, paths.facts.get(name, Fact()).minimum
        return False, None

    def branch_boolean(self, node, site):
        paths = self.incoming.get(site) or self.branched.get(site)
        name = name_of(unwrap(node))
        if paths is None or name in self.nonlocals or name not in paths.joined:
            return lint.UNKNOWN
        origin = paths.facts.get(name, Fact()).origin
        if origin is not None and origin.kind == "literal" and type(origin.value) is bool:
            return origin.value
        return lint.UNKNOWN

    def minimum(self, node):
        node = unwrap(node)
        if node is None:
            return None
        if node.kind() in COLLECTIONS:
            elements = children(node)
            if not any(child.kind() in {"list_splat", "spread_element"} for child in elements):
                if node.kind() == "array_expression" and any(child.kind() == ";" for child in node.children()):
                    size = self.literal(self.syntax, elements[-1])
                    return size if type(size) is int and size >= 0 else None
                return len(elements)
        if node.kind() == "macro_invocation" and parts(node.field("macro")) == ("vec",):
            tree = next((child for child in children(node) if child.kind() == "token_tree"), None)
            if tree is not None and not children(tree):
                return 0
            if tree is not None and not any(child.kind() == ";" for child in tree.children()):
                return 1
        return None

    def merge(self, paths, incoming, *, branch=False):
        facts = lint.agree_on_paths([path.facts for path in incoming], merge_facts)
        joined = set.union(*(path.joined for path in incoming))
        if branch:
            names = set.union(*(set(path.facts) for path in incoming))
            joined.update(name for name in names if any(
                path.facts.get(name) != paths.facts.get(name) for path in incoming
            ))
        paths.joined = joined
        paths.facts = facts

    def branch(self, node, paths):
        self.visit(node.field("condition"), paths)
        branches = []
        branch = paths.copy()
        self.visit(node.field("consequence"), branch)
        branches.append(branch)
        alternate = paths.copy()
        alternatives = [child for child in children(node) if child.kind() in {"elif_clause", "else_clause"}]
        if alternatives:
            for alternative in alternatives:
                self.visit(alternative.field("condition"), alternate)
                branch = alternate.copy()
                arm = alternative.field("consequence") or alternative.field("body")
                self.visit(arm or next(iter(reversed(children(alternative))), None), branch)
                branches.append(branch)
            if alternatives[-1].kind() != "else_clause":
                branches.append(alternate)
        else:
            self.visit(node.field("alternative"), alternate)
            branches.append(alternate)
        self.merge(paths, branches, branch=True)

    def visit(self, node, paths):
        if node is None or node.kind() in SCOPED:
            return
        kind = node.kind()
        site = self.syntax.absolute(node)
        if not self.suspended and kind in CALL_KINDS | {"assert_statement"} and site in self.captures:
            self.incoming[site] = paths.copy()
        if kind in IF and node.field("consequence") is not None:
            if not self.suspended:
                self.branched[site] = paths.copy()
            self.branch(node, paths)
            return
        if kind in LOOPS:
            if not self.suspended:
                self.branched[site] = paths.copy()
                iterator = node.field("right") or node.field("value")
                if iterator is not None:
                    self.branched[self.syntax.absolute(iterator)] = self.branched[site]
            uncertain = paths.copy()
            if self.syntax.language == "python" and (target := node.field("left")) is not None:
                for entry in [target, *children(target)]:
                    self.invalidate(entry, uncertain)
            for child in children(node):
                self.visit(child, uncertain)
            self.merge(paths, [paths, uncertain])
            return
        if kind in UNMODELLED:
            incoming = [paths.copy()]
            suspended = self.suspended
            self.suspended = True
            try:
                for child in children(node):
                    uncertain = paths.copy()
                    self.visit(child, uncertain)
                    incoming.append(uncertain)
            finally:
                self.suspended = suspended
            self.merge(paths, incoming)
            return
        if kind in DECLARATIONS:
            name, value = declaration(node)
            target = node.field("left")
            origin = self.origin(value, paths)
            minimum = self.minimum(value)
            self.visit(value, paths)
            if (alias := name_of(unwrap(value))) in paths.facts and paths.facts[alias].minimum is not None:
                self.invalidate(unwrap(value), paths)
            if name is not None:
                if kind == "let_declaration" and name in paths.facts:
                    # In a Rust block, a shadow can cease to exist when the block ends.
                    paths.bind(name, Fact())
                else:
                    paths.bind(name, Fact(origin, minimum))
            elif target is not None:
                self.invalidate(target, paths)
            else:
                pattern = node.field("pattern")
                if pattern is not None and pattern.kind() == "tuple_pattern":
                    for position, child in enumerate(children(pattern)):
                        if member := name_of(child):
                            paths.bind(member, Fact(Origin("tuple-member", site, position, value)
                                                    if value is not None else None))
                elif pattern is not None:
                    for child in query(pattern, "identifier"):
                        paths.bind(child.text(), Fact())
            return
        if kind in WRITES:
            for child in children(node):
                self.invalidate(child, paths)
        if kind in CALL_KINDS and (kind != "macro_invocation" or parts(node.field("macro"))[-1:] in {
            ("assert_eq",), ("assert_ne",), ("assert",), ("prop_assert_eq",), ("prop_assert_ne",),
        }):
            self.record_call(node, paths)
        for child in children(node):
            self.visit(child, paths)

    def invalidate(self, node, paths):
        if node is None:
            return
        names = parts(node)
        if not names:
            names = parts(node.field("value") or next(iter(children(node)), None))
        if names and names[0] in paths.facts:
            paths.facts[names[0]] = Fact()

    def record_call(self, node, paths):
        call = self.call(node)
        if call is None:
            return
        name, receiver, args = call
        local = name_of(receiver)
        if local in paths.facts:
            fact = paths.facts[local]
            method = name[-1] if name else ""
            if len(args) == 1 and fact.minimum is not None and (
                method in {"append", "push"} or method == "extend" and self.syntax.language == "rust"
            ):
                added = 1 if method != "extend" else self.minimum(args[0]) or 0
                paths.facts[local] = Fact(fact.origin, fact.minimum + added)
            elif method not in ACCESSORS and (fact.minimum is not None or (
                fact.origin is not None and fact.origin.kind == "product"
            )):
                self.invalidate(receiver, paths)
        for argument in args:
            if node.kind() == "macro_invocation":
                for borrow in query(argument, "reference_expression"):
                    if any(child.kind() == "mutable_specifier" for child in children(borrow)):
                        self.invalidate(borrow.field("value"), paths)
            argument = unwrap(argument)
            if argument is not None and argument.kind() == "reference_expression":
                elements = children(argument)
                mutable = any(child.kind() == "mutable_specifier" for child in elements)
                argument = argument.field("value") or next(iter(reversed(elements)), None)
                if mutable:
                    self.invalidate(argument, paths)
                continue
            local = parts(argument)[:1]
            if local and local[0] in paths.facts and paths.facts[local[0]].minimum is not None:
                if name[-1:] not in {("len",), ("assert_eq",), ("assert_ne",)}:
                    self.invalidate(argument, paths)


def stage_call(syntax, node):
    call = syntax.call(node)
    return call.name, call.receiver, call.arguments


def stage_helpers(file, scope):
    def resolve(node, name):
        target = file.resolve_function(name, scope)
        if target is None:
            return None
        function = file.functions[target]
        if file.syntax.language == "python" and function.parent().kind() == "decorated_definition":
            return None
        parameters = function.field("parameters")
        if parameters is None:
            return None
        args = [name_of(parameter.field("pattern") or parameter) for parameter in children(parameters)]
        return function, args, direct_return(function.field("body"))
    return resolve


def same_product(facts, check):
    first, second = (facts.eventual_origin(node, check.location) for node in (check.left, check.right))
    return first is not None and first.kind == "product" and first == second


def different_python_products(file, flow, check):
    def callee(operand):
        origin = flow.reaching_facts.eventual_origin(operand, check.location)
        if origin is None or origin.kind != "product" or origin.source.kind() != "call":
            return (), None
        call = flow.syntax.call(origin.source)
        if not call.name:
            return (), None
        for depth in range(len(flow.scope), -1, -1):
            if imported := file.imports.get((*flow.scope[:depth], call.name[0])):
                path = (*imported, *call.name[1:])
                if not {"test", "tests", "fixtures"} & set(path[:-1]):
                    return path, origin
                break
        return (), None

    actual, first = callee(check.left)
    expected, second = callee(check.right)
    if first is None or second is None or len(actual) < 2 or len(expected) < 2:
        return False
    return (actual != expected and actual[:-1] == expected[:-1] and not lint.inverse_codecs(actual, expected)
            and [arg.text() for arg in flow.syntax.call(first.source).arguments] ==
                [arg.text() for arg in flow.syntax.call(second.source).arguments])
