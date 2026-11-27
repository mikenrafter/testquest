"""Advisories for comparisons against rendered output and implementation selectors."""

from functools import lru_cache
import os
import re

from . import lint
from .ast_grep import children, line, offset, query, shape, split_operands
from .ast_grep_origins import Origin
from .ast_grep_ts import ASSERT_UNEQUAL, FUNCTIONS, arguments, chain, expression, literal, name, span
from .ast_grep_ts_recorders import object_member


TESTING_LIBRARY = "@testing-library/"
QUERY = re.compile(r"(?:get|find|query)(?:All)?By[A-Z]\w*\Z")
CLASS_SELECTOR = re.compile(r"(?:\.[\w-]+)+\Z")
VISIBLE_MATCHERS = {"toHaveAccessibleName", "toHaveAccessibleDescription", "toHaveRole", "toHaveTextContent",
                    "toHaveDisplayValue"}
TEXT_PROPERTIES = {"textContent", "innerText", "innerHTML", "outerHTML", "value"}
DOM_SELECTORS = {"querySelector", "querySelectorAll", "getElementById", "getElementsByClassName"}
STATE_CLASS = re.compile(r"(?:is-[\w-]+|[\w-]+-active|row-invalid|sort-(?:asc|desc)|selected|connected|dim|semi|done|tablet|[\w-]+-(?:selecting|hovering))")
CLASS_READ = re.compile(
    r"(?:\b(?:closest|querySelector(?:All)?|matches)(?:<[^>]+>)?\(\s*['\"`]\.([\w-]+)['\"`]"
    r"|\bclassList\.contains\(\s*['\"`]([\w-]+)['\"`])"
)
EQUALITY = {"toBe", "toEqual", "toStrictEqual", "equal", "strictEqual", "deepEqual", "deepStrictEqual"}


def owned_query(node, *kinds):
    """Visit syntax evaluated in this callback, skipping nested function bodies."""
    start = offset(node)
    return (child for child in query(node, *kinds) if not any(
        parent.kind() in FUNCTIONS and offset(parent) >= start and parent.range().end.index <= node.range().end.index
        for parent in child.ancestors()))


def imported_reference(file, node):
    base, members = chain(node)
    if file.binding(base) is not None or file.parameter_scope(base) is not None:
        return "", ()
    source, imported = file.source_imports.get(name(base), ("", ()))
    return source, (*imported, *members)


def reaching_initializer(file, node):
    """Use the visible declaration only while its binding has no intervening writes."""
    if not name(node) or file.parameter_scope(node) is not None:
        return None, ""
    binding = file.binding(node)
    destructured, field = file.destructuring(node)
    if destructured is not None and (binding is None or offset(destructured) > offset(binding)):
        binding = destructured
    else:
        field = ""
    if binding is None or binding.kind() != "variable_declarator":
        return None, ""
    if file.writes is None:
        file.writes = {}
        for write in query(file.syntax.root, "assignment_expression", "augmented_assignment_expression", "update_expression"):
            lhs = expression(write.field("left") or write.field("argument"))
            if name(lhs) and (owner := file.binding(lhs)) is not None:
                file.writes.setdefault(offset(owner), []).append(offset(write))
    if any(site < offset(node) for site in file.writes.get(offset(binding), ())):
        return None, ""
    return binding.field("value"), field


class OutputOrigins:
    def __init__(self, file):
        self.file = file

    def origin(self, node, fields=(), seen=frozenset()):
        node = expression(node)
        if node is None or span(node) in seen:
            return None
        file = self.file
        seen = seen | {span(node)}
        kind = node.kind()
        if name(node):
            value, field = reaching_initializer(file, node)
            if value is not None:
                return self.origin(value, (field, *fields) if field else fields, seen)
            source, names = imported_reference(file, node)
            if source.startswith(TESTING_LIBRARY) and names == ("screen",):
                return Origin("dom", value=("screen",))
            if name(node) == "document" and file.binding(node) is None and file.parameter_scope(node) is None:
                return Origin("dom", value=("document",))
            return None
        if kind == "object" and fields:
            return self.origin(object_member(node, fields[0]), fields[1:], seen)
        if kind == "member_expression":
            return self.origin(node.field("object"), (name(node.field("property")), *fields), seen)
        if kind in {"await_expression", "subscript_expression", "unary_expression"}:
            return self.origin(node.field("object") or node.field("argument") or next(iter(children(node)), None),
                               fields, seen)
        if kind in {"binary_expression", "template_string", "template_substitution"}:
            operator, left, right = split_operands(node)
            if operator in {"??", "||", "&&"}:
                fallback = literal(right)
                if fallback not in {"", None}:
                    return None
                return self.origin(left, fields, seen)
            parts = [left, right] if operator in {"+", "-", "*", "/", "%", "**"} else (
                children(node) if kind in {"template_string", "template_substitution"} else [])
            origins = []
            for part in parts:
                if part.kind() == "string_fragment" or literal(part) is not lint.UNKNOWN:
                    continue
                origin = self.origin(part, fields, seen)
                if origin is None:
                    return None
                origins.append(origin)
            return origins[0] if origins and all(item == origins[0] for item in origins) else None
        if kind != "call_expression":
            return None
        fn = expression(node.field("function"))
        base, members = chain(fn)
        method = members[-1] if members else name(base)
        args = arguments(node)
        source, names = imported_reference(file, fn)
        if source.startswith(TESTING_LIBRARY) and names and (names[-1] in {"render", "within"} or QUERY.fullmatch(names[-1])):
            return Origin("dom", value=(source, names[-1]))
        if method in DOM_SELECTORS or QUERY.fullmatch(method):
            receiver = self.origin(fn.field("object"), seen=seen)
            if receiver is not None and receiver.kind == "dom":
                return Origin("dom", value=(*receiver.value, method, tuple(shape(arg) for arg in args)))
        target = file.helper(fn) if file.parameter_scope(fn) is None else None
        if target is not None:
            value = return_expression(target)
            return self.origin(value, fields, seen)
        if source.startswith(".") and source and not any(part.startswith(("test", "fixture")) for part in source.split("/")):
            return Origin("production", value=(source, names))
        if method in {"Number", "String", "parseInt", "parseFloat"} and file.binding(base) is None and args and not members:
            return self.origin(args[0], fields, seen)
        if method == "exec" and fn.field("object") is not None and fn.field("object").kind() == "regex" and args:
            return self.origin(args[0], fields, seen)
        if method in {"split", "match", "trim", "slice", "toString"} and members:
            return self.origin(fn.field("object"), fields, seen)
        return None

    def selector(self, node, fields=(), seen=frozenset()):
        """Return the DOM element whose CSS implementation is being asserted."""
        node = expression(node)
        if node is None or span(node) in seen:
            return None
        file = self.file
        seen = seen | {span(node)}
        if name(node):
            value, field = reaching_initializer(file, node)
            return self.selector(value, (field, *fields) if field else fields, seen)
        if node.kind() == "object" and fields:
            return self.selector(object_member(node, fields[0]), fields[1:], seen)
        if node.kind() == "member_expression":
            target = node.field("object")
            prop = name(node.field("property"))
            if prop == "className" or prop == "classList" and fields == ("contains",):
                return node
            return self.selector(target, (name(node.field("property")), *fields), seen)
        operator, left, _ = split_operands(node)
        if operator in {"??", "||"}:
            return self.selector(left, fields, seen)
        if node.kind() != "call_expression":
            return None
        fn = expression(node.field("function"))
        receiver = fn.field("object")
        method = name(fn.field("property"))
        if not fields and method in {"querySelector", "querySelectorAll", "closest"}:
            args = arguments(node)
            selector = literal(args[0]) if args else None
            if isinstance(selector, str) and CLASS_SELECTOR.fullmatch(selector):
                return node
        if method in {"contains", "split"}:
            return self.selector(receiver, (method, *fields), seen)
        target = file.helper(fn) if file.parameter_scope(fn) is None else None
        return self.selector(return_expression(target), fields, seen) if target is not None else None


def return_expression(callback):
    body = callback.field("body")
    if body is None or body.kind() != "statement_block":
        return body
    returns = list(owned_query(body, "return_statement"))
    if len(returns) == 1 and offset(returns[0].parent()) == offset(body):
        return next(iter(children(returns[0])), None)
    return None


def visible_element(file, check):
    subject = expression(check.actual)
    if check.method in VISIBLE_MATCHERS:
        return subject
    if subject is None:
        return None
    if subject.kind() == "member_expression":
        prop = name(subject.field("property"))
        receiver = subject.field("object")
        if prop in TEXT_PROPERTIES:
            return receiver
        if prop in {"display", "visibility"} and name(receiver.field("property")) == "style":
            return receiver.field("object")
    if subject.kind() != "call_expression":
        return None
    fn = subject.field("function")
    args = arguments(subject)
    method = name(fn.field("property"))
    if method == "getAttribute" and args and literal(args[0]) in {"role", "aria-label", "aria-labelledby", "value", "d", "stroke"}:
        return fn.field("object")
    return subject if QUERY.fullmatch(method) and (
        method.endswith(("ByText", "ByLabelText")) or method.endswith("ByRole")
        and check.method in {"toBeVisible", "toBeInTheDocument"}) else None


def element_key(file, node, seen=frozenset()):
    node = expression(node)
    if node is None or span(node) in seen:
        return None
    if name(node):
        binding = file.binding(node)
        value, field = reaching_initializer(file, node)
        if value is not None and not field:
            return element_key(file, value, seen | {span(node)})
        return name(node), offset(binding) if binding is not None else None
    if node.kind() == "call_expression":
        if not arguments(node) and (target := file.helper(node.field("function"))) is not None:
            value = return_expression(target)
            if value is not None:
                return element_key(file, value, seen | {span(node)})
        return (element_key(file, node.field("function"), seen),
                tuple(shape(expression(arg)) for arg in arguments(node)))
    if node.kind() == "member_expression":
        return element_key(file, node.field("object"), seen), name(node.field("property"))
    return shape(node)


def capture(file, node):
    initializer, field = reaching_initializer(file, expression(node))
    return (offset(initializer), field) if initializer is not None else None


def class_names(selector, subject, expected):
    if selector.kind() == "call_expression":
        return literal(arguments(selector)[0]).strip(".").split(".")
    subject = expression(subject)
    observations = [call for call in query(subject, "call_expression") if (
        name(call.field("function").field("property")) in {"contains", "closest"}
    )]
    if observations:
        names = [literal(args[0]) for call in observations if (args := arguments(call))]
        return [value.removeprefix(".") for value in names if isinstance(value, str)]
    value = literal(expected)
    return value.split() if isinstance(value, str) else []


@lru_cache(maxsize=16)
def product_classes(root):
    readbacks = {}
    if root is None:
        return readbacks
    for directory, folders, files in os.walk(root):
        folders[:] = [folder for folder in folders if not folder.startswith(".") and folder not in {
            "node_modules", "__tests__", "test", "tests", "fixtures", "dist", "coverage", "build",
        }]
        for filename in files:
            path = root / directory / filename
            if (path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
                    or filename.startswith(("test_", "test-")) or re.search(
                        r"\.(?:test|spec|browser|e2e|bench)\.", filename)):
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                for first, second in CLASS_READ.findall(path.read_text()):
                    readbacks.setdefault(first or second, set()).add(path.resolve())
            except (OSError, UnicodeError):
                continue
    return readbacks


def contractual_class(file, test, selector, check):
    classes = class_names(selector, check.actual, check.expected)
    named = set(re.findall(r"[\w-]+", test.name.casefold()))
    if any(STATE_CLASS.fullmatch(css) or "-" in css and css in named for css in classes):
        return True
    if not classes:
        return False
    readbacks = product_classes(file.scan_root)
    path = file.path.resolve()
    return all(readbacks.get(css, set()) - {path} for css in classes)


def relative_render(file, check, actual, expected):
    if actual != expected:
        return False
    left = expression(check.actual)
    right = expression(check.expected)
    if name(right):
        right, _ = reaching_initializer(file, right)
    right = expression(right)
    if right is None:
        return False
    if right.kind() != "template_string" and element_key(file, left) == element_key(file, right):
        return True
    operator, baseline, delta = split_operands(right)
    if operator in {"+", "-"} and type(literal(delta)) in {int, float}:
        initial, _ = reaching_initializer(file, expression(baseline))
        return initial is not None and element_key(file, initial) == element_key(file, left)
    return False


def contrasted_limit(file, actual, expected, checks):
    if actual.kind() != "call_expression" or expected.kind() != "call_expression":
        return False
    inputs = arguments(actual)
    if len(inputs) < 2 or type(literal(inputs[-1])) not in {int, float}:
        return False
    prefix = [element_key(file, arg) for arg in inputs[:-1]]
    if prefix != [element_key(file, arg) for arg in arguments(expected)]:
        return False
    callee = element_key(file, actual.field("function"))
    limit = literal(inputs[-1])
    for check in checks:
        candidate = expression(check.actual)
        if (check.guarded or check.negated or check.method not in {"toBeUndefined", "toBeNull"}
                or candidate is None or candidate.kind() != "call_expression"
                or element_key(file, candidate.field("function")) != callee):
            continue
        args = arguments(candidate)
        if (len(args) == len(inputs) and type(other := literal(args[-1])) in {int, float} and other < limit
                and [element_key(file, arg) for arg in args[:-1]] == prefix):
            return True
    return False


def ui_issues(file, test, checks):
    origins = OutputOrigins(file)
    semantic = [(line(check.node), element_key(file, element)) for check in checks
                if (element := visible_element(file, check)) is not None]
    pinned = {element_key(file, operand) for check in checks if not check.guarded and check.operator in {"==", "==="}
              for operand, value in ((check.actual, check.expected), (check.expected, check.actual))
              if operand is not None and literal(value) is not lint.UNKNOWN}
    contrasted = {key for check in checks if not check.guarded and (
        check.method in ASSERT_UNEQUAL or check.negated and check.method in {"toBe", "toEqual", "toStrictEqual"})
        for operand in (check.actual, check.expected) if (key := capture(file, operand)) is not None}
    for check in checks:
        subject = expression(check.actual)
        if subject is None:
            continue
        expect = expression(check.node.field("function"))
        root, _ = chain(expect)
        if root is not None and root.kind() == "call_expression" and file.expect_origin(root.field("function")):
            element = origins.selector(subject)
            if element is not None and not contractual_class(file, test, element, check):
                selected = element if element.kind() == "call_expression" else element.field("object")
                key = element_key(file, selected)
                if not any(other == key and abs(site - line(check.node)) <= 3 for site, other in semantic):
                    yield check.node, "implementation-selector"
        if check.method not in EQUALITY or check.negated or check.expected is None:
            continue
        if capture(file, check.expected) in contrasted or any(
                element_key(file, operand) in pinned for operand in (check.actual, check.expected)):
            continue
        expected = origins.origin(check.expected)
        actual = origins.origin(subject)
        if expected is None or actual is None or expected.kind != actual.kind:
            continue
        describes_relation = lint.suggests_repeatability(test.name) or lint.describes_distinct_inputs(test.name)
        if expected.kind == "production":
            left, right = expression(check.actual), expression(check.expected)
            if (left.kind() == right.kind() == "call_expression" and actual.value[0] == expected.value[0]
                    and actual.value[1] != expected.value[1] and not describes_relation
                    and not contrasted_limit(file, left, right, checks)):
                yield check.node, "self-derived-expected"
        elif expected == actual and not relative_render(file, check, actual, expected) and (
                not describes_relation or template_or_calculation(file, check.expected)):
            yield check.node, "self-derived-expected"


def template_or_calculation(file, node):
    node = expression(node)
    while node is not None and name(node):
        node, field = reaching_initializer(file, node)
        if field:
            return False
        node = expression(node)
    return node is not None and node.kind() in {"template_string", "binary_expression"}
