"""nondistinguishing-literal facts read from ast-grep nodes for the shared policy in lint.py."""

import builtins
from collections import defaultdict
import sys

from . import lint
from .ast_grep import CALLS, COMMENTS, MEMBERS, NAMES, children, macro_name, offset, parts, unwrap
from .ast_grep_lint import UNKNOWN, body_of, literal

NESTED = {"function_definition", "function_item", "class_definition", "impl_item", "mod_item",
          "attribute_item"} | COMMENTS
OBSERVERS = {"len", "set", "list", "sorted", "tuple", "str", "bool", "int", "bytes",
             "float", "dict", "repr", "iter", "reversed"}
RELATIONS = {"<", "<=", ">", ">="}
COMPARING = RELATIONS | {"==", "!="}
ARITHMETIC = {"+": "+", "-": "-", "*": "*", "/": "/", "//": "/"}
COMPOUND = {"+=": "+", "-=": "-", "*=": "*", "/=": "/"}
RUST_CHECKS = {"assert", "assert_eq", "assert_ne", "prop_assert", "prop_assert_eq", "prop_assert_ne",
               "debug_assert", "debug_assert_eq", "debug_assert_ne"}
PYTHON_COMPARISONS = {
    "assertEqual": "==", "assertEquals": "==", "assertNotEqual": "!=", "assertNotEquals": "!=",
    "assertIs": "is", "assertIsNot": "is not", "assertListEqual": "==", "assertTupleEqual": "==",
    "assertGreater": ">", "assertGreaterEqual": ">=", "assertLess": "<", "assertLessEqual": "<=",
}
ORDERING_CLOSURES = {"is_sorted", "is_sorted_by", "is_sorted_by_key"}


def issues(file, syntax, flow, body, test_name):
    """Lines of nondistinguishing literals in one test, from facts the shared policy judges."""
    return sorted(set(Literals(file, syntax, flow, body, test_name).lines()))


def ident(syntax, node):
    start = syntax.absolute(node)
    return start, node.range().end.index - offset(node), node.kind()


def walk(syntax, node):
    """Every node under node in source order, entering macro arguments but not nested items."""
    stack = [(syntax, node)]
    while stack:
        owner, current = stack.pop()
        yield owner, current
        if current.kind() == "macro_invocation":
            expansion = owner.macro_arguments(current)
            inner = getattr(expansion, "syntax", None) or owner
            stack.extend((inner, argument) for argument in reversed(expansion))
            continue
        stack.extend((owner, child) for child in reversed(children(current)) if child.kind() not in NESTED)


def number(value):
    if isinstance(value, lint.SuffixedNumber):
        value = value.value
    return value if type(value) in {int, float} else None


def legacy_symmetric(value):
    """Rust duplicates are read as plain integers, text, booleans and nesting of those."""
    if isinstance(value, lint.SuffixedNumber):
        value = value.value if type(value.value) is int else UNKNOWN
    if type(value) is float or type(value) is int and value < 0:
        return UNKNOWN
    if isinstance(value, (list, tuple)):
        items = [legacy_symmetric(item) for item in value]
        return UNKNOWN if UNKNOWN in items else type(value)(items)
    return value


class Site:
    """One call: where it is, what it calls, and the literal arguments it passes."""

    def __init__(self, owner, node, call, file):
        self.owner = owner
        self.node = node
        self.call = call
        self.identity = ident(owner, node)
        self.line = file.line_at(owner.absolute(node))
        self.location = (self.line, owner.absolute(node))
        function = node.field("function") if node.kind() in {"call", "call_expression"} else None
        name = children(function)[-1] if function is not None and function.kind() in MEMBERS and children(function) else function
        self.name_line = file.line_at(owner.absolute(name)) if name is not None else self.line
        self.slots = {}
        arguments = call.arguments
        self.inner = getattr(arguments, "syntax", None) or owner
        if owner.language == "python":
            for argument in arguments:
                if argument.kind() == "keyword_argument":
                    self.slots[argument.field("name").text()] = argument.field("value")
                elif argument.kind() in {"list_splat", "dictionary_splat"}:
                    break
                elif not any(isinstance(slot, str) for slot in self.slots):
                    self.slots[len([slot for slot in self.slots if type(slot) is int])] = argument
        else:
            self.slots = dict(enumerate(arguments))
        self.literals = {slot: self.read(argument) for slot, argument in self.slots.items()}
        self.shapes = {slot: shape_of(argument) for slot, argument in self.slots.items()}

    def read(self, argument):
        value = literal(self.inner, argument)
        if value is UNKNOWN and self.owner.language == "python" and argument.kind() == "dictionary":
            return {}
        return value

    @property
    def method(self):
        return self.call.method

    @property
    def macro(self):
        return self.call.macro


def shape_of(node):
    from .ast_grep import shape
    return shape(node)


class Literals:
    def __init__(self, file, syntax, flow, body, test_name):
        self.file = file
        self.syntax = syntax
        self.flow = flow
        self.body = body
        self.test_name = test_name
        self.rust = syntax.language == "rust"
        self.scope = flow.scope
        self.sites = []
        self.assertions = []
        self.statements = []
        self.by_identity = {}
        self.owner_of = {}
        self.bindings = defaultdict(list)
        self.definitions = {}
        self.collect()

    # --- collection -------------------------------------------------------------------------

    def collect(self):
        for owner, node in walk(self.syntax, self.body):
            kind = node.kind()
            if kind in {"let_declaration", "assignment"}:
                self.bind(owner, node)
            if kind == "assert_statement":
                predicate = next(iter(children(node)), None)
                if predicate is not None:
                    self.assertions.append((owner, node, [predicate], "assert"))
                    self.owner_of[id(node)] = owner
            elif kind in CALLS:
                call = owner.call(node)
                site = Site(owner, node, call, self.file)
                self.sites.append(site)
                self.by_identity[site.identity] = site
                if lint.assertion(call.method) and (not call.macro or call.method in RUST_CHECKS
                                                    or call.method.startswith(("assert", "prop_assert"))):
                    self.assertions.append((site.inner, node, list(call.arguments), call.method))
                    self.owner_of[id(node)] = owner

    def bind(self, owner, node):
        pattern = node.field("pattern") or node.field("left")
        value = node.field("value") or node.field("right")
        if pattern is not None and pattern.kind() == "mut_pattern":
            pattern = next(iter(children(pattern)), None)
        if (pattern is not None and pattern.kind() == "identifier" and value is not None
                and node.field("alternative") is None):
            self.bindings[pattern.text()].append((owner.absolute(node), value))

    def resolve(self, owner, node):
        """Follow a name bound exactly once before its use, as the legacy readers do."""
        seen = set()
        while node is not None:
            node = unwrap(node)
            if node is None or node.kind() not in NAMES:
                break
            name = parts(node)
            if len(name) != 1 or name in seen:
                break
            before = owner.absolute(node)
            earlier = [value for site, value in self.bindings.get(name[0], ()) if site < before]
            if len(earlier) != 1 or earlier[0] is None:
                break
            seen.add(name)
            node, owner = earlier[0], self.syntax
        return owner, node

    def site_for(self, owner, node):
        if node is None or node.kind() not in CALLS:
            return None
        return self.by_identity.get(ident(owner, node))

    # --- observed calls -----------------------------------------------------------------------

    def observed(self, owner, node, *, follow=True, depth=0):
        """Calls whose result the check inspects, following receivers, wrappers and bindings."""
        if node is None or depth > 8:
            return
        owner, node = self.resolve(owner, node)
        node = unwrap(node)
        if node is None:
            return
        kind = node.kind()
        if self.rust:
            yield from self.observed_rust(owner, node, kind, depth)
            return
        if kind == "call":
            site = self.site_for(owner, node)
            if site is not None:
                yield site
            if follow:
                function = unwrap(node.field("function"))
                if function is not None and function.kind() == "attribute":
                    yield from self.observed(owner, children(function)[0], depth=depth + 1)
                elif (function is not None and function.kind() == "identifier"
                      and function.text() in OBSERVERS and len(children(node.field("arguments"))) == 1):
                    yield from self.observed(owner, children(node.field("arguments"))[0], depth=depth + 1)
        elif kind in {"attribute", "subscript"}:
            target = node.field("object") or node.field("value")
            yield from self.observed(owner, target, follow=follow, depth=depth + 1)
        elif kind in {"await", "unary_operator", "not_operator"}:
            for child in children(node)[-1:]:
                yield from self.observed(owner, child, follow=follow, depth=depth + 1)
        elif kind in {"comparison_operator", "boolean_operator"}:
            for child in children(node):
                yield from self.observed(owner, child, follow=follow, depth=depth + 1)

    def observed_rust(self, owner, node, kind, depth):
        if kind in {"reference_expression", "unary_expression", "try_expression", "await_expression",
                    "parenthesized_expression", "type_cast_expression"}:
            inner = children(node)
            if inner:
                yield from self.observed(owner, inner[0] if kind != "reference_expression" else inner[-1],
                                         depth=depth + 1)
        elif kind == "binary_expression":
            inner = children(node)
            if inner:
                yield from self.observed(owner, inner[0], depth=depth + 1)
        elif kind in CALLS:
            site = self.site_for(owner, node)
            if site is not None and not site.macro:
                yield site
                receiver = site.call.receiver
                if receiver is not None:
                    yield from self.observed(owner, receiver, depth=depth + 1)
            elif site is not None:
                return
        elif kind in {"field_expression", "index_expression"}:
            inner = children(node)
            if inner:
                yield from self.observed(owner, inner[0], depth=depth + 1)

    # --- checks -------------------------------------------------------------------------------

    def leaves(self, owner, node):
        """Split a Rust predicate into comparisons joined by || and &&."""
        node = unwrap(node) if node is not None and node.kind() == "parenthesized_expression" else node
        if node is not None and node.kind() == "binary_expression":
            operator = node.field("operator").text() if node.field("operator") is not None else ""
            left, right = node.field("left"), node.field("right")
            if operator in {"||", "&&"}:
                yield from self.leaves(owner, left)
                yield from self.leaves(owner, right)
                return
            if operator in COMPARING:
                yield operator, left, right
                return
        yield "", node, None

    def predicates(self):
        """Comparisons made by Rust assertion macros, as (owner, operator, left, right)."""
        for owner, node, arguments, method in self.assertions:
            if not self.rust or method not in RUST_CHECKS or not arguments:
                continue
            if method.endswith(("_eq", "_ne")) and len(arguments) >= 2:
                yield owner, "==" if method.endswith("_eq") else "!=", arguments[0], arguments[1]
            elif not method.endswith(("_eq", "_ne")):
                for operator, left, right in self.leaves(owner, arguments[0]):
                    yield owner, operator, left, right

    def check_nodes(self):
        """Every node that a check looks at, so calls inside it count as checked."""
        for owner, node, arguments, method in self.assertions:
            for argument in arguments:
                yield owner, argument

    def constructors(self, owner, node):
        for inner_owner, inner in walk(owner, node):
            if inner.kind() in CALLS:
                site = self.site_for(inner_owner, inner)
                if site is not None and self.constructor(site):
                    yield site.identity

    def constructor(self, site):
        name = site.call.name
        if not name:
            return False
        if name[-1][:1].isupper():
            return True
        return self.rust and len(name) >= 2 and name[-1] == "new" and name[-2][:1].isupper()

    def expectations(self):
        expected = set()
        for owner, node, arguments, method in self.assertions:
            if self.rust:
                if method in RUST_CHECKS and method.endswith("_eq") and len(arguments) >= 2:
                    expected.update(self.constructors(owner, arguments[1]))
                elif method in RUST_CHECKS and method in {"assert", "debug_assert", "prop_assert"} and arguments:
                    for operator, left, right in self.leaves(owner, arguments[0]):
                        if operator == "==" and right is not None:
                            expected.update(self.constructors(owner, right))
                continue
            if method == "assertEqual":
                keywords = [argument.field("value") for argument in arguments
                            if argument.kind() == "keyword_argument" and argument.field("name").text() == "second"]
                positional = [argument for argument in arguments if argument.kind() != "keyword_argument"]
                for argument in positional[1:2] + keywords:
                    expected.update(self.constructors(owner, argument))
        for owner, node, arguments, method in self.assertions:
            if self.rust:
                continue
            for argument in arguments:
                for inner_owner, inner in walk(owner, argument):
                    if inner.kind() == "comparison_operator":
                        for left, operator, right in self.chain(inner):
                            if operator == "==":
                                expected.update(self.constructors(inner_owner, right))
        return expected

    def chain(self, node):
        """Pairs of neighbouring operands in a Python comparison, with their operator."""
        previous, operator = None, ""
        for child in node.children():
            if child.kind() in COMMENTS:
                continue
            if child.is_named():
                if previous is not None and operator:
                    yield previous, operator, child
                previous, operator = child, ""
            else:
                operator = child.text()

    # --- the evidence --------------------------------------------------------------------------

    def lines(self):
        self.expected = self.expectations()
        claims = lint.argument_claims(self.test_name)
        checked, observed, direct, fixtures = set(), set(), set(), set()
        bound_checks = []
        for owner, argument in self.check_nodes():
            for inner_owner, inner in walk(owner, argument):
                if inner.kind() in CALLS:
                    site = self.site_for(inner_owner, inner)
                    if site is not None:
                        checked.add(site.identity)
                elif inner.kind() in NAMES and self.is_value(inner):
                    resolved_owner, resolved = self.resolve(inner_owner, inner)
                    if resolved is not None and resolved.kind() in {"await", "await_expression"}:
                        resolved = children(resolved)[0]
                    site = self.site_for(resolved_owner, resolved)
                    if site is not None and ident(resolved_owner, resolved) != ident(inner_owner, inner):
                        checked.add(site.identity)
            if not self.rust:
                for site in self.observed(owner, argument, follow=False):
                    direct.add(site.identity)
                for site in self.observed(owner, argument):
                    observed.add(site.identity)
                    for slot_argument in site.slots.values():
                        fixture = self.resolved_site(site.inner, slot_argument)
                        if fixture is not None:
                            fixtures.add(fixture.identity)
                bound_checks.extend(self.python_bounds(owner, argument))
        if not self.rust:
            for owner, node, arguments, method in self.assertions:
                positional = [argument for argument in arguments if argument.kind() != "keyword_argument"]
                if node.kind() != "assert_statement" and method in PYTHON_COMPARISONS and len(positional) >= 2:
                    bound_checks.extend(self.bounds_of(owner, PYTHON_COMPARISONS[method], positional[0], positional[1]))
        if self.rust:
            for owner, operator, left, right in self.predicates():
                for operand, other in ((left, right), (right, left)):
                    if operand is None:
                        continue
                    for site in self.observed(owner, operand):
                        observed.add(site.identity)
                        for slot_argument in site.slots.values():
                            fixtures.update(inner.identity for inner in self.observed(site.inner, slot_argument))
                    site = self.resolved_site(owner, operand)
                    if site is not None and not site.macro and self.simple(site):
                        direct.add(site.identity)
                        threshold = number(literal(owner, other)) if other is not None else None
                        bounded = operator in RELATIONS and threshold is not None
                        exempt = bounded and (self.measures_files(site) or threshold in self.returns(site))
                        bound_checks.append((self.key(site), site.location, self.inputs(site, signed=True),
                                             bounded, exempt))
        sites = [site for site in self.sites if not site.macro]
        yield from self.fixtures_and_symmetry(sites, checked, observed, direct, fixtures, claims)
        yield from lint.loose_bounds(bound_checks)
        if not self.rust:
            yield from self.unchecked_item_order()
        yield from self.fixed_outputs_and_orders()

    def is_value(self, node):
        parent = node.parent()
        if parent is None:
            return True
        kind = parent.kind()
        if kind in {"attribute"} and parent.field("attribute") is not None and parent.field("attribute").text() == node.text() \
                and offset(parent.field("attribute")) == offset(node):
            return False
        if kind in {"field_expression"}:
            field = parent.field("field")
            if field is not None and offset(field) == offset(node):
                return False
        if kind in {"call", "call_expression"}:
            function = parent.field("function")
            if function is not None and offset(function) == offset(node):
                return False
        if kind in {"scoped_identifier", "keyword_argument"}:
            return kind == "keyword_argument" and parent.field("value") is not None \
                and offset(parent.field("value")) == offset(node)
        return True

    def resolved_site(self, owner, node):
        owner, resolved = self.resolve(owner, node)
        resolved = unwrap(resolved)
        if resolved is None:
            return None
        return self.site_for(owner, resolved)

    # --- callee facts --------------------------------------------------------------------------

    def key(self, site):
        function = site.node.field("function")
        if function is None:
            return None
        function = unwrap(function)
        text = "".join(function.text().split())
        if self.rust and function.kind() == "field_expression":
            receiver = children(function)[0] if children(function) else None
            if receiver is None or not parts(receiver) and receiver.kind() != "self":
                return None
        return text

    def simple(self, site):
        return self.key(site) is not None

    def definition(self, site):
        """The function a call reaches, found by the file index; None when unknown."""
        identity = site.identity
        if identity not in self.definitions:
            found = None
            name = site.call.name
            if name and not (self.rust and site.call.receiver is not None):
                found = self.file.fixture_function(name, self.scope)
            self.definitions[identity] = found
        return self.definitions[identity]

    def parameter_names(self, site):
        found = self.definition(site)
        if found is None:
            return {}
        _, target, function = found
        names = {}
        parameters = function.field("parameters")
        if parameters is None:
            return {}
        position = 0
        bound = (not self.rust and site.call.name[:1] in {("self",), ("cls",)}
                 and any(parent.kind() == "class_definition" for parent in function.ancestors()))
        skipped = 0
        keyword_only = False
        for parameter in children(parameters):
            kind = parameter.kind()
            if kind in {"self_parameter", "variadic_parameter", "attribute_item"}:
                continue
            if kind in {"keyword_separator", "list_splat_pattern"}:
                keyword_only = True
                continue
            name = self.parameter_name(parameter)
            if not name:
                position += 1
                continue
            if bound and skipped == 0:
                skipped = 1
                continue
            if not keyword_only:
                names[position - skipped] = name
            names[name] = name
            position += 1
        return names

    def parameter_name(self, parameter):
        kind = parameter.kind()
        if kind == "identifier":
            return parameter.text()
        if kind == "parameter":
            pattern = parameter.field("pattern")
            return pattern.text() if pattern is not None and pattern.kind() == "identifier" else ""
        if kind in {"typed_parameter", "default_parameter", "typed_default_parameter"}:
            name = parameter.field("name") or next(iter(children(parameter)), None)
            return name.text() if name is not None and name.kind() == "identifier" else ""
        return ""

    def inputs(self, site, *, signed=False):
        names = self.parameter_names(site)
        ignored = {slot for slot, name in names.items() if name.startswith("_")}
        return {slot: value for slot, value in site.literals.items() if slot not in ignored}

    # --- operators on parameters --------------------------------------------------------------

    def operators(self, function, *, fields=None):
        """Arithmetic operators applied to each parameter-rooted path in a function body."""
        operations = defaultdict(set)
        body = body_of(function)
        if body is None:
            return operations
        stack = [body]
        while stack:
            node = stack.pop()
            kind = node.kind()
            if kind in {"binary_operator", "binary_expression"}:
                operator = node.field("operator").text() if node.field("operator") is not None else ""
                if operator in ARITHMETIC:
                    for operand in (node.field("left"), node.field("right")):
                        path = self.path_of(operand)
                        if path:
                            operations[path].add(ARITHMETIC[operator])
            elif kind in {"augmented_assignment", "compound_assignment_expr"}:
                operator = node.field("operator").text() if node.field("operator") is not None else ""
                if operator.endswith("=") and operator[:-1] in ARITHMETIC:
                    path = self.path_of(node.field("left"))
                    if path:
                        operations[path].add(ARITHMETIC[operator[:-1]])
            stack.extend(children(node))
        return operations

    def path_of(self, node):
        node = unwrap(node)
        while node is not None and node.kind() in {"type_cast_expression", "reference_expression"}:
            inner = children(node)
            node = unwrap(inner[0] if node.kind() == "type_cast_expression" else inner[-1]) if inner else None
        if node is None:
            return ()
        kind = node.kind()
        if kind in {"identifier", "self"}:
            return (node.text(),)
        if kind in {"attribute", "field_expression"}:
            elements = children(node)
            base = self.path_of(elements[0]) if elements else ()
            tail = parts(elements[-1]) if len(elements) == 2 else ()
            return (*base, *tail) if base and tail else ()
        if kind in {"subscript", "index_expression"}:
            elements = children(node)
            return self.path_of(elements[0]) if elements else ()
        return ()

    def relevant(self, site):
        """Values among a callee's fixed arguments that an arithmetic operator could neutralise."""
        found = self.definition(site)
        callee = site.call.name
        if self.rust and (site.call.receiver is not None or "." in (self.key(site) or ".")):
            return lambda fixed: ()
        if found is None:
            return lambda fixed: list(fixed.values())
        file, target, function = found
        names = self.parameter_names(site)
        if self.rust and target[-1] == "new" and len(target) > 1 and not any(
                other.identity in self.direct and self.key(other) == self.key(site) for other in self.sites):
            operations = self.stored_operands(function)
        else:
            operations = defaultdict(set)
            destructured = self.destructured(function)
            for path, operators in self.operators(function).items():
                operations[destructured.get(path[0], path[0])] |= operators
        return lambda fixed: [value for position, value in fixed.items()
                              if lint.fixed_operand(value, operations.get(names.get(position), set()))]

    def destructured(self, function):
        """Names a body unpacks from a parameter: `let (a, b) = pair;` maps a and b to pair."""
        names = {}
        body = body_of(function)
        for statement in query_nodes(body) if body is not None else ():
            if statement.kind() != "let_declaration":
                continue
            pattern, value = statement.field("pattern"), statement.field("value")
            if (pattern is not None and pattern.kind() == "tuple_pattern" and value is not None
                    and value.kind() == "identifier"):
                for part in children(pattern):
                    if part.kind() == "identifier":
                        names[part.text()] = value.text()
        return names

    def stored_operands(self, constructor):
        """Operators applied to fields that a plain struct constructor stores from its parameters."""
        body = body_of(constructor)
        statements = children(body) if body is not None else []
        if len(statements) != 1:
            return {}
        record = unwrap(statements[0])
        if record.kind() == "expression_statement":
            record = children(record)[0]
        if record.kind() != "struct_expression":
            return {}
        fields = {}
        listing = next((child for child in children(record) if child.kind() == "field_initializer_list"), None)
        for initializer in children(listing) if listing is not None else ():
            if initializer.kind() == "shorthand_field_initializer":
                fields[initializer.text()] = initializer.text()
            elif initializer.kind() == "field_initializer":
                value = initializer.field("value")
                if value is None or value.kind() != "identifier":
                    return {}
                fields[initializer.field("field").text()] = value.text()
            else:
                return {}
        operands = defaultdict(set)
        impl = next((parent for parent in constructor.ancestors() if parent.kind() == "impl_item"), None)
        if impl is None:
            return operands
        for method in impl.find_all(kind="function_item"):
            if offset(method) == offset(constructor):
                continue
            for path, operators in self.operators(method).items():
                if len(path) >= 2 and path[0] == "self" and path[1] in fields:
                    operands[fields[path[1]]] |= operators
        return operands

    # --- bounds -------------------------------------------------------------------------------

    def python_bounds(self, owner, argument):
        for inner_owner, inner in walk(owner, argument):
            if inner.kind() == "comparison_operator":
                for left, operator, right in self.chain(inner):
                    yield from self.bounds_of(inner_owner, operator, left, right)

    def bounds_of(self, owner, operator, left, right):
        for operand, other in ((left, right), (right, left)):
            site = self.resolved_site(owner, operand)
            if site is None:
                continue
            threshold = literal(owner, other)
            bounded = operator in RELATIONS and type(threshold) in {int, float}
            exempt = bounded and (self.counts_file_text(site) or threshold in self.returns(site))
            yield (self.key(site), site.location, dict(site.literals), bounded, exempt)

    def returns(self, site):
        """Numbers the callee returns as literals."""
        found = self.definition(site)
        if found is None:
            return ()
        _, _, function = found
        values = []
        body = body_of(function)
        if body is None:
            return ()
        stack = [body]
        while stack:
            node = stack.pop()
            kind = node.kind()
            if kind in {"function_definition", "function_item", "closure_expression", "lambda"}:
                continue
            if kind in {"return_statement", "return_expression"}:
                inner = children(node)
                if inner and number(literal(self.file.syntax, inner[0])) is not None:
                    values.append(number(literal(self.file.syntax, inner[0])))
            stack.extend(children(node))
        if self.rust:
            tail = self.tail(body)
            if tail is not None and number(literal(self.file.syntax, tail)) is not None:
                values.append(number(literal(self.file.syntax, tail)))
            values.extend(self.branch_tails(body))
        return values

    def tail(self, block):
        statements = children(block)
        if not statements:
            return None
        last = statements[-1]
        return None if last.kind() in {"expression_statement", "let_declaration"} and last.kind() != "expression_statement" \
            else (children(last)[0] if last.kind() == "expression_statement" and not last.text().endswith(";") else last)

    def branch_tails(self, block):
        values = []
        tail = self.tail(block)
        if tail is not None and tail.kind() == "if_expression":
            for branch in (tail.field("consequence"), tail.field("alternative")):
                if branch is None:
                    continue
                if branch.kind() == "else_clause":
                    inner = children(branch)
                    branch = inner[0] if inner else None
                if branch is not None and branch.kind() == "block":
                    end = self.tail(branch)
                    if end is not None and number(literal(self.file.syntax, end)) is not None:
                        values.append(number(literal(self.file.syntax, end)))
        return values

    def counts_file_text(self, site):
        function = unwrap(site.node.field("function"))
        if function is not None and function.kind() == "attribute" and function.field("attribute").text() == "count":
            return self.reads_file_text(site.owner, children(function)[0])
        if site.call.name == ("len",) and len(site.slots) == 1:
            matches = self.resolved_site(site.owner, site.slots[0])
            if matches is not None and matches.call.name[-1:] == ("findall",) and 1 in matches.slots:
                return self.reads_file_text(site.owner, matches.slots[1])
        return False

    def reads_file_text(self, owner, source):
        owner, source = self.resolve(owner, source)
        source = unwrap(source)
        if source is not None and source.kind() == "attribute" and parts(source)[:1] in {("self",), ("cls",)}:
            attribute = parts(source)[-1]
            candidates = []
            for setup in ("setUp", "setUpClass"):
                for name, function in self.file.function_items:
                    if name[-1] == setup:
                        for assignment in function.find_all(kind="assignment"):
                            target = assignment.field("left")
                            if target is not None and parts(target)[-1:] == (attribute,) and len(parts(target)) == 2:
                                candidates.append(assignment.field("right"))
            if len(candidates) == 1:
                source = unwrap(candidates[0])
        call = self.call_at(owner, source)
        if call is None:
            return False
        if call.method == "read_text":
            return True
        if call.method == "read" and call.receiver is not None:
            opened = self.call_at(owner, call.receiver)
            return opened is not None and opened.name == ("open",)
        return False

    def measures_files(self, site):
        name = site.call.name
        if len(name) != 1:
            return False
        _, value = self.resolve(site.owner, unwrap(site.node.field("function")))
        if value is None or value.kind() != "closure_expression":
            return False
        for node in value.find_all(kind="scoped_identifier"):
            text = "".join(node.text().split())
            if text in {"std::fs::read_dir", "std::fs::read_to_string"}:
                return True
        return False

    # --- calls ---------------------------------------------------------------------------------

    def fixtures_and_symmetry(self, sites, checked, observed, direct, fixtures, claims):
        self.direct = direct
        grouped = defaultdict(list)
        for site in sites:
            key = self.key(site)
            grouped[key if key is not None else ("?", site.method)].append(site)
        unvaried, coverage = [], []
        calls = defaultdict(list)
        for site in sites:
            key = self.key(site)
            if key is None and self.rust:
                continue
            if lint.assertion(site.method) or site.identity in self.expected:
                if not self.rust:
                    calls[key].append(site)
                continue
            calls[key].append(site)
            inputs = self.inputs(site)
            unvaried.append((key, site.location, inputs, site.identity in observed))
            if any(claims):
                coverage.append((key, site.location, inputs, site.identity in observed, site.identity in fixtures))
        # a callee seen only through assertion helpers never reaches the policy
        for key, group in calls.items():
            nested = [(site, self.inputs(site)) for site in group
                      if site.identity in fixtures and site.identity not in self.expected
                      and (not self.rust or site.identity not in observed)
                      and not lint.assertion(site.method)]
            if lint.fixed_boundary([inputs for _, inputs in nested]):
                yield min(site.location for site, _ in nested)[0]
        for key, group in grouped.items():
            peers = [(site.shapes, self.symmetric_values(site)) for site in group
                     if site.identity in checked and site.identity not in self.expected]
            for site in group:
                if not self.symmetric_candidate(site):
                    continue
                if lint.symmetric_input(self.callee_name(site), self.symmetric_values(site), site.shapes, peers,
                                        observed=site.identity in direct):
                    if self.rust and site.identity not in checked and site.identity not in observed:
                        if self.formats(site) or self.local_binding(site):
                            continue
                    yield site.name_line if self.rust else site.line
        by_key = {}
        for site in sites:
            by_key.setdefault(self.key(site), site)
        yield from lint.unvaried_arguments(
            unvaried, lambda callee, line, fixed: self.relevant(by_key[callee])(fixed))
        yield from lint.named_arguments(coverage, claims, lambda callee: self.parameter_names(by_key[callee]))

    def symmetric_values(self, site):
        if not self.rust:
            return site.literals
        return {slot: legacy_symmetric(value) for slot, value in site.literals.items()}

    def callee_name(self, site):
        return site.method

    def symmetric_candidate(self, site):
        name = site.method
        if not name or name[0].isupper() or lint.helper(name):
            return False
        if self.rust:
            recognized, _ = self.file.check_call(site.call, self.scope)
            return not recognized and site.call.method not in {"vec"}
        qualified = site.call.name
        if not qualified:
            return False
        root = qualified[:-1]
        module = (self.file.imports.get((root[0],), root[:1]) or root[:1])[0] if root else ""
        recognized, _ = self.file.check_call(site.call, self.scope)
        if (recognized and not lint.helper(name)) or module in {"self", "tests"} or module in sys.stdlib_module_names:
            return False
        if not root and (name in self.local_helpers() or hasattr(builtins, name)):
            return False
        return True

    def local_helpers(self):
        return {name[0] for name, function in self.file.function_items
                if len(name) == 1 and not name[0].startswith("test")}

    def local_binding(self, site):
        name = site.call.name
        return len(name) == 1 and name[0] in self.bindings

    def formats(self, site):
        found = self.definition(site)
        if found is None:
            return False
        body = body_of(found[2])
        statements = children(body) if body is not None else []
        first = statements[0] if statements else None
        while first is not None and first.kind() == "expression_statement":
            inner = children(first)
            first = inner[0] if inner else None
        return first is not None and first.kind() == "macro_invocation" and macro_name(first) == ("format",)

    # --- order and output ----------------------------------------------------------------------

    def unchecked_item_order(self):
        # r5 judged the legacy per-item-order form false (scalar checks always run): not ported.
        return ()

    def guarded(self, owner, node):
        home = self.owner_of.get(id(node), owner)
        for check in self.flow.checks:
            if check.syntax is home and offset(check.source) == offset(node):
                return check.guarded
        return False

    def mentions(self, owner, node, name):
        return any(inner.kind() in NAMES and inner.text() == name for _, inner in walk(owner, node))

    def fixed_outputs_and_orders(self):
        for owner, node, arguments, method in self.assertions:
            line = self.file.line_at(self.owner_of[id(node)].absolute(node))
            if self.rust:
                if method in {"assert", "debug_assert", "prop_assert"} and arguments and self.rust_fixed_output(
                        owner, arguments[0]):
                    yield line
                elif self.rust_order(owner, node, method, arguments, line):
                    yield line
            else:
                predicate = self.python_predicate(owner, node, method, arguments)
                if predicate is None:
                    continue
                operator, left, right = predicate
                if (operator == "==" and self.zero_origin(owner, left, right)
                        or operator == "" and self.python_fixed_output(owner, left)
                        or operator == "==" and self.python_order(owner, node, left, right, line)):
                    yield line

    # Python ----------------------------------------------------------------------------------

    def python_predicate(self, owner, node, method, arguments):
        """The (operator, left, right) of a Python check; operator '' means a bare predicate."""
        if node.kind() == "assert_statement":
            return self.python_comparison(arguments[0])
        positional = [argument for argument in arguments if argument.kind() != "keyword_argument"]
        if method in PYTHON_COMPARISONS and len(positional) >= 2:
            return PYTHON_COMPARISONS[method], positional[0], positional[1]
        if method in {"assertTrue", "assert_"} and positional:
            return self.python_comparison(positional[0])
        return None

    def python_comparison(self, node):
        node = unwrap(node)
        if node is not None and node.kind() == "comparison_operator":
            pairs = list(self.chain(node))
            if len(pairs) == 1:
                return pairs[0][1], pairs[0][0], pairs[0][2]
        return "", node, None

    def python_order(self, owner, node, left, right, line):
        if "sorted" in self.local_helpers() or ("sorted",) in self.file.imports:
            return False
        guards = self.guarded(owner, node)
        for subject, ordered in ((left, right), (right, left)):
            ordered_owner, ordered = self.resolve(owner, ordered)
            if subject.kind() != "identifier" or ordered is None or ordered.kind() != "call":
                continue
            call = ordered_owner.call(ordered)
            if call.name != ("sorted",) or len(call.arguments) != 1 or shape_of(call.arguments[0]) != shape_of(subject):
                continue
            name = subject.text()
            _, source = self.resolve(owner, subject)
            if source is None or source.kind() not in {"call", "list_comprehension", "attribute", "subscript"}:
                continue
            projection = None
            if source.kind() == "list_comprehension":
                clause = next((child for child in children(source) if child.kind() == "for_in_clause"), None)
                projection = clause.field("right") if clause is not None else None
            if not any(self.independent_order(other_owner, other_node, name, projection, subject)
                       for other_owner, other_node, other_arguments, other_method in self.assertions
                       if other_node is not node
                       and (not self.guarded(other_owner, other_node) or self.guarded(other_owner, other_node) == guards)):
                return True
        return False

    def independent_order(self, owner, node, name, projection, subject):
        other_arguments = [argument for _, argument in self.leaf_arguments(owner, node)]
        predicate = self.python_predicate(owner, node, self.method_of(node), self.arguments_of(owner, node))
        if predicate is None:
            return False
        operator, first, second = predicate
        if operator == "==" and first is not None and second is not None:
            lengths = []
            for part in (first, second):
                part = unwrap(part)
                call = owner.call(part) if part is not None and part.kind() == "call" else None
                lengths.append(call is not None and call.name == ("len",) and len(call.arguments) == 1 and call)
            if all(lengths):
                mentioned = [self.mentions(owner, part.arguments[0], name)
                             or self.mentions(*self.resolve(owner, part.arguments[0]), name) for part in lengths]
                return any(self.mentions(owner, part, name) for part in (first, second)) and not all(mentioned)
        return any(self.mentions(owner, argument, name) for argument in other_arguments[:2])

    def leaf_arguments(self, owner, node):
        for argument in self.arguments_of(owner, node):
            yield owner, argument

    def arguments_of(self, owner, node):
        for other_owner, other_node, arguments, method in self.assertions:
            if other_node is node:
                return arguments
        return []

    def method_of(self, node):
        for other_owner, other_node, arguments, method in self.assertions:
            if other_node is node:
                return method
        return ""

    def zero_origin(self, owner, left, right):
        """An expected list of (time, time, ...) rows that is the zero-based input list shifted by nothing."""
        actual_owner, actual = self.resolve(owner, left)
        expected = unwrap(right)
        if actual is None or actual.kind() != "call" or expected is None or expected.kind() not in {"list", "tuple"}:
            return False
        outputs = self.times(owner, expected, expected_rows=True)
        if len(outputs) < 2 or not all(type(value) in {int, float} and value > 0 for value in outputs):
            return False
        for argument in actual_owner.call(actual).arguments:
            argument_owner, resolved = self.resolve(actual_owner, argument)
            if resolved is not None and self.times(argument_owner, unwrap(resolved)) == (0, *outputs):
                return True
        return False

    def times(self, owner, sequence, *, expected_rows=False):
        if sequence is None or sequence.kind() not in {"list", "tuple"}:
            return ()
        values = []
        for element in children(sequence):
            if element.kind() != "call" or not owner.call(element).arguments:
                return ()
            arguments = owner.call(element).arguments
            first = literal(owner, arguments[0])
            if expected_rows and (len(arguments) < 2 or literal(owner, arguments[1]) != first):
                return ()
            values.append(first)
        return tuple(values)

    def python_fixed_output(self, owner, predicate):
        """`"a" in x or "b" in x` where x's function always starts its text with one of the needles."""
        predicate = unwrap(predicate)
        if predicate is None or predicate.kind() != "boolean_operator":
            return False
        operands = []

        def flatten(node):
            node = unwrap(node)
            if node is not None and node.kind() == "boolean_operator" and node.field("operator").text() == "or":
                flatten(node.field("left"))
                flatten(node.field("right"))
            else:
                operands.append(node)
        flatten(predicate)
        if len(operands) < 2:
            return False
        needles, receivers = [], []
        for operand in operands:
            if operand is None or operand.kind() != "comparison_operator":
                return False
            pairs = list(self.chain(operand))
            if len(pairs) != 1 or pairs[0][1] != "in":
                return False
            needle = literal(owner, pairs[0][0])
            if not isinstance(needle, str) or not needle:
                return False
            needles.append(needle)
            receivers.append(self.resolve(owner, pairs[0][2]))
        first_owner, first = receivers[0]
        if first is None or first.kind() != "call" or any(
                shape_of(unwrap(node)) != shape_of(first) for _, node in receivers[1:]):
            return False
        site = self.site_for(first_owner, first)
        found = self.definition(site) if site is not None else None
        if found is None:
            return False
        body = body_of(found[2])
        statements = children(body) if body is not None else []
        last = statements[-1] if statements else None
        if last is None or last.kind() != "return_statement" or not children(last) or children(last)[0].kind() != "identifier":
            return False
        name = children(last)[0].text()
        file_syntax = found[0].syntax
        initial, appended, stores = None, [], 0
        for part in query_nodes(body):
            kind = part.kind()
            if kind == "return_statement" and part is not last and offset(part) != offset(last):
                return False
            if kind in {"assignment", "augmented_assignment"}:
                target = part.field("left")
                if target is not None and target.kind() == "identifier" and target.text() == name:
                    stores += 1
                    value = part.field("right")
                    if kind == "assignment":
                        initial = literal(file_syntax, value)
                    else:
                        if part.field("operator").text() != "+=":
                            return False
                        appended.extend(self.text_pieces(file_syntax, value))
        if stores != 1 + len(self.augmentations(body, name)) or not isinstance(initial, str):
            return False
        return lint.prefix_satisfies(needles, [initial], appended)

    def augmentations(self, body, name):
        return [part for part in query_nodes(body) if part.kind() == "augmented_assignment"
                and part.field("left") is not None and part.field("left").text() == name]

    def text_pieces(self, syntax, value):
        value = unwrap(value)
        if value is None:
            return []
        if value.kind() == "string":
            return [child.text() for child in children(value) if child.kind() == "string_content"]
        text = literal(syntax, value)
        return [text] if isinstance(text, str) else []

    # Rust ------------------------------------------------------------------------------------

    def mutated(self, name):
        return bool(self.flow.writes.get((name,))) or len(self.bindings.get(name, ())) > 1

    def rust_fixed_output(self, owner, predicate):
        needles, receivers = [], []
        alternatives = [leaf for operator, leaf, _ in self.leaves(owner, predicate) if operator == ""]
        if len(list(self.leaves(owner, predicate))) != len(alternatives) or len(alternatives) < 2:
            return False
        for alternative in alternatives:
            alternative = unwrap(alternative)
            site = self.site_for(owner, alternative) if alternative is not None else None
            if site is None or site.method != "contains" or len(site.slots) != 1 or site.call.receiver is None:
                return False
            needle = site.literals.get(0)
            if not isinstance(needle, str) or not needle:
                return False
            needles.append(needle)
            receivers.append(self.resolve(owner, site.call.receiver))
        first_owner, first = receivers[0]
        first = unwrap(first)
        if first is None or first.kind() != "call_expression" or any(
                shape_of(unwrap(node)) != shape_of(first) for _, node in receivers[1:]):
            return False
        site = self.site_for(first_owner, first)
        found = self.definition(site) if site is not None else None
        if found is None:
            return False
        file, target, function = found
        prefix, dynamic = self.output_parts(file, function)
        return lint.prefix_satisfies(needles, prefix, dynamic)

    def output_parts(self, file, function):
        body = body_of(function)
        statements = children(body) if body is not None else []
        if len(statements) < 2:
            return [], []
        tail = statements[-1]
        if tail.kind() == "expression_statement":
            inner = children(tail)
            tail = inner[0] if inner and not tail.text().endswith(";") else tail
        if tail.kind() != "identifier":
            return [], []
        name = tail.text()
        first = statements[0]
        if first.kind() != "let_declaration" or parts(first.field("pattern")) != (name,) and not (
                first.field("pattern") is not None and first.field("pattern").kind() == "mut_pattern"
                and first.field("pattern").text().split()[-1] == name) \
                or "".join((first.field("value").text() if first.field("value") is not None else "").split()) != "String::new()":
            return [], []
        prefix, dynamic = [], []
        leading = True
        for statement in statements[1:-1]:
            call = self.push_str(file, statement, name)
            if call is not None and leading:
                prefix.extend(call)
                continue
            leading = False
            for _, node in walk(file.syntax, statement):
                if node.kind() == "return_expression":
                    return [], []
                if node.kind() == "identifier" and node.text() == name:
                    owner_call = self.enclosing_push(node)
                    if owner_call is None:
                        return [], []
                    dynamic.extend(self.fragments(file, owner_call))
        return prefix, dynamic

    def push_str(self, file, statement, name):
        node = statement
        if node.kind() == "expression_statement":
            inner = children(node)
            node = inner[0] if inner else None
        if node is None or node.kind() != "call_expression":
            return None
        function = node.field("function")
        if function is None or function.kind() != "field_expression" or function.field("field").text() != "push_str":
            return None
        receiver = function.field("value")
        if receiver is None or receiver.text() != name:
            return None
        return self.fragments(file, node)

    def enclosing_push(self, node):
        parent = node.parent()
        if parent is not None and parent.kind() == "field_expression" and parent.field("field").text() == "push_str":
            call = parent.parent()
            if call is not None and call.kind() == "call_expression":
                return call
        return None

    def fragments(self, file, call):
        arguments = children(call.field("arguments"))
        if len(arguments) != 1:
            return []
        return self.output_fragments(file.syntax, arguments[0])

    def output_fragments(self, syntax, node):
        node = unwrap(node)
        while node is not None and node.kind() == "reference_expression":
            node = unwrap(children(node)[-1])
        if node is None:
            return []
        value = literal(syntax, node)
        if isinstance(value, str):
            return [value]
        if node.kind() == "string_literal":
            content = node.text()[1:-1]
            return [decode_escapes(content)] if decode_escapes(content) is not None else []
        if node.kind() != "macro_invocation" or macro_name(node) != ("format",):
            return []
        arguments = syntax.macro_arguments(node)
        inner = getattr(arguments, "syntax", None) or syntax
        if not arguments:
            return []
        template = self.output_fragments(inner, arguments[0])
        raw = arguments[0].text()
        if len(template) != 1:
            return []
        pieces, fields = split_template(template[0])
        if len(fields) == len(arguments) - 1 and "{{" not in raw and "}}" not in raw and all(
                simple_field(field) for field in fields):
            pieces.extend(text for argument in arguments[1:] if isinstance(text := literal(inner, argument), str))
        return pieces

    def rust_order(self, owner, node, method, arguments, line):
        if method not in {"assert", "debug_assert", "prop_assert"} and not (
                method in RUST_CHECKS and method.endswith("_eq")):
            return False
        subject = None
        if line in self.window_order():
            subject = self.window_order()[line]
        elif method.endswith("_eq") and len(arguments) >= 2:
            for original, copy in ((arguments[0], arguments[1]), (arguments[1], arguments[0])):
                original = self.strip(original)
                copy = unwrap(copy)
                while copy is not None and copy.kind() == "reference_expression":
                    copy = unwrap(children(copy)[-1])
                if copy is None or copy.kind() != "identifier":
                    continue
                copy_owner, source = self.resolve(owner, copy)
                if source is None or source.kind() != "call_expression":
                    continue
                site = self.site_for(copy_owner, source)
                if site is None or site.method != "clone" or site.call.receiver is None or site.slots:
                    continue
                if shape_of(site.call.receiver) != shape_of(original):
                    continue
                start, end = copy_owner.absolute(source), owner.absolute(copy)
                if any(start < other.location[1] < end and other.method in {"sort", "sort_unstable"}
                       and not other.slots and other.call.receiver is not None
                       and other.call.receiver.text() == copy.text() for other in self.sites):
                    subject = original
                    break
        elif method in {"assert", "debug_assert", "prop_assert"} and arguments:
            value_owner, value = self.resolve(owner, arguments[0])
            site = self.site_for(value_owner, unwrap(value)) if value is not None else None
            if site is None or site.call.receiver is None:
                return False
            if site.method == "all" and site.slots:
                inner = self.site_for(site.inner, unwrap(site.call.receiver))
                if inner is None or inner.method != "windows" or number(inner.literals.get(0)) != 2 or inner.call.receiver is None:
                    return False
                subject = inner.call.receiver
            elif site.method in ORDERING_CLOSURES:
                inner = self.site_for(site.inner, unwrap(site.call.receiver))
                if inner is not None and inner.method in {"iter", "into_iter"} and not inner.slots and inner.call.receiver is not None:
                    subject = inner.call.receiver
                else:
                    subject = site.call.receiver
            else:
                return False
        if subject is None:
            return False
        return self.lacks_cardinality(owner, node, subject, line)

    def strip(self, node):
        node = unwrap(node)
        while node is not None and node.kind() in {"reference_expression", "unary_expression"}:
            inner = children(node)
            if not inner:
                break
            node = unwrap(inner[-1])
        return node

    def path_nodes(self, node):
        node = unwrap(node)
        if node is not None and node.kind() in {"identifier", "self"}:
            return (node.text(),)
        if node is not None and node.kind() == "field_expression":
            base = self.path_nodes(node.field("value"))
            return (*base, node.field("field").text()) if base else ()
        return ()

    def lacks_cardinality(self, owner, node, subject, line):
        path = self.path_nodes(subject)
        if not path or self.mutated(path[0]):
            return False
        guards = self.guarded(owner, node)
        subjects = {path[0]}
        if len(path) == 1:
            value_owner, value = self.resolve(owner, subject)
            value = unwrap(value)
            if value is None or shape_of(value) == shape_of(subject):
                return False
            text = "".join(value.text().split())
            if text.startswith(("Vec::new(", "vec![")):
                return False
            if value.kind() == "call_expression":
                site = self.site_for(value_owner, value)
                if site is not None and site.method == "clone":
                    return False
            if self.fixed_projection(value_owner, value):
                return False
            seen = 0
            while (source := self.projection_source(value_owner, value)) is not None and seen < 8:
                seen += 1
                if source in subjects:
                    break
                subjects.add(source)
                value_owner, value = self.resolve(value_owner, self.identifier_node(source, value))
                value = unwrap(value)
                if value is None:
                    break
            for alias, entries in self.bindings.items():
                if len(entries) == 1 and self.projection_source(self.syntax, entries[0][1]) in subjects:
                    subjects.add(alias)
        for other_owner, other_node, other_arguments, other_method in self.assertions:
            if other_node is node or not other_arguments:
                continue
            if self.guarded(other_owner, other_node) and not guards:
                continue
            if other_method in RUST_CHECKS and other_method.endswith("_eq") and len(other_arguments) >= 2:
                first, second = other_arguments[:2]
                if all(self.method_name(other_owner, part) == "len" for part in (first, second)):
                    continue
            for argument in other_arguments[:2]:
                for _, inner in walk(other_owner, argument):
                    if inner.kind() == "identifier" and inner.text() in subjects and self.is_value(inner):
                        return False
        return True

    def identifier_node(self, name, fallback):
        return fallback

    def method_name(self, owner, node):
        node = unwrap(node)
        site = self.site_for(owner, node) if node is not None else None
        return site.method if site is not None else ""

    def call_at(self, owner, node):
        node = unwrap(node) if node is not None else None
        if node is None or node.kind() not in CALLS or node.kind() == "macro_invocation":
            return None
        return owner.call(node)

    def projection_source(self, owner, value):
        """`rows.iter().map(..).collect()` reads from rows; return that name."""
        value = unwrap(value)
        names = []
        while value is not None and value.kind() == "call_expression":
            call = owner.call(value)
            if call.receiver is None:
                return None
            names.append(call.method)
            value = unwrap(call.receiver)
        if (value is not None and value.kind() == "identifier" and len(names) >= 3
                and names[0] == "collect" and names[-1] in {"iter", "into_iter"}
                and set(names) <= {"iter", "into_iter", "map", "collect"} and "map" in names):
            return value.text()
        return None

    def fixed_projection(self, owner, value):
        """A result built from a literal range, so its length never depends on the code under test."""
        call = self.call_at(owner, value)
        if call is not None and call.receiver is None and call.name:
            found = self.file.fixture_function(call.name, self.scope)
            if found is not None:
                body = body_of(found[2])
                statements = children(body) if body is not None else []
                if statements:
                    tail = statements[-1]
                    if tail.kind() == "expression_statement" and children(tail):
                        tail = children(tail)[0]
                    value, owner = tail, found[0].syntax
        names = []
        while value is not None and value.kind() == "call_expression":
            inner = owner.call(value)
            if inner.receiver is None:
                return False
            names.append(inner.method)
            value = inner.receiver
        return (names == ["collect", "map"] and value is not None and value.kind() == "parenthesized_expression"
                and any(child.kind() == "range_expression" for child in children(value)))

    def window_order(self):
        if not hasattr(self, "_window_order"):
            self._window_order = {}
            for owner, node in walk(self.syntax, self.body):
                if node.kind() != "for_expression":
                    continue
                pattern, value, body = node.field("pattern"), node.field("value"), node.field("body")
                if pattern is None or pattern.kind() != "identifier" or value is None or body is None:
                    continue
                site = self.site_for(owner, unwrap(value))
                if (site is None or site.method != "windows" or number(site.literals.get(0)) != 2
                        or site.call.receiver is None):
                    continue
                for statement in children(body):
                    inner = children(statement)[0] if statement.kind() == "expression_statement" and children(statement) else statement
                    if inner.kind() != "macro_invocation" or macro_name(inner) not in {
                            ("assert",), ("debug_assert",), ("prop_assert",)}:
                        continue
                    arguments = owner.macro_arguments(inner)
                    if arguments and self.adjacent_pair(pattern.text(), arguments[0]):
                        self._window_order[self.file.line_at(owner.absolute(inner))] = site.call.receiver
        return self._window_order

    def adjacent_pair(self, pair, predicate):
        predicate = unwrap(predicate)
        if predicate is None or predicate.kind() != "binary_expression":
            return False
        operator = predicate.field("operator").text()
        if operator not in RELATIONS:
            return False
        sides = []
        for side in (predicate.field("left"), predicate.field("right")):
            suffix = []
            side = unwrap(side)
            while side is not None and side.kind() == "field_expression":
                suffix.append(side.field("field").text())
                side = unwrap(side.field("value"))
            if side is None or side.kind() != "index_expression":
                return False
            base, index = children(side)[:2] if len(children(side)) >= 2 else (None, None)
            if base is None or base.text() != pair or number(literal(self.syntax, index)) not in {0, 1}:
                return False
            sides.append((number(literal(self.syntax, index)), tuple(suffix)))
        return {sides[0][0], sides[1][0]} == {0, 1} and sides[0][1] == sides[1][1]


def query_nodes(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(child for child in children(current) if child.kind() not in {"function_definition", "lambda"})


def decode_escapes(content):
    table = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    out, index = [], 0
    while index < len(content):
        char = content[index]
        if char == "\\":
            if index + 1 >= len(content) or content[index + 1] not in table:
                return None
            out.append(table[content[index + 1]])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def split_template(template):
    """Literal pieces between format placeholders, and the placeholders themselves."""
    pieces, fields, current, index = [], [], [], 0
    while index < len(template):
        char = template[index]
        if char == "{" and template[index:index + 2] != "{{":
            end = template.find("}", index)
            if end < 0:
                break
            if current:
                pieces.append("".join(current))
                current = []
            fields.append(template[index:end + 1])
            index = end + 1
        else:
            current.append(char)
            index += 1
    if current:
        pieces.append("".join(current))
    return pieces, fields


def simple_field(field):
    inner = field[1:-1]
    if not inner:
        return True
    if not inner.startswith(":"):
        return False
    spec = inner[1:]
    if spec[:1] in {"<", ">", "^"}:
        spec = spec[1:]
    return spec.isdigit()
