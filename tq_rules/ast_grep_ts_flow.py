"""Track paths through an ECMAScript callback that can finish without checking."""

import re

from . import lint
from .ast_grep import children, line, offset, query, shape, split_operands
from .ast_grep_ts import (
    CHAINS, FUNCTIONS, ITERATORS, TRANSPARENT, Check, arguments, chain, compare, expression,
    formal_parameters, literal, name, text_literal, truth,
)


try:
    from .lint import describes_distinct_inputs, suggests_repeatability
except ImportError:
    describes_distinct_inputs = lint.DISTINCT_INPUTS.search
    suggests_repeatability = lint.REPEATABILITY.search


NEXT, RETURN, BREAK, CONTINUE, THROW = 1, 2, 4, 8, 16


def followed_by(first, second):
    return (first & ~NEXT) | (second if first & NEXT else 0)


def within(node, owner):
    return offset(owner) <= offset(node) < owner.range().end.index


def same(first, second):
    return first is not None and second is not None and (
        offset(first), first.range().end.index) == (offset(second), second.range().end.index)


def is_async(callback):
    return any(child.kind() == "async" for child in callback.children())


class Flow:
    def __init__(self, file, callback, context, active=frozenset(), callbacks=None):
        self.file = file
        self.callback = callback
        self.body = callback.field("body")
        self.context = context
        self.active = active | {offset(callback)}
        self.callbacks = callbacks or {}
        self.checks = []
        self.references = None

    def sequence(self, nodes, conditional=False, catches=False):
        remaining = NEXT
        for node in nodes:
            result = self.visit(node, conditional or remaining != NEXT and bool(remaining), catches)
            remaining = followed_by(remaining, result)
        return remaining

    def record(self, check, conditional, detached=False):
        check.guarded = conditional or detached
        self.checks.append(check)
        return NEXT if detached else 0

    def visit(self, node, conditional=False, catches=False):
        if node is None:
            return NEXT
        kind = node.kind()
        if kind in FUNCTIONS:
            return NEXT
        if kind == "call_expression":
            return self.call(node, conditional, catches)
        if kind == "new_expression" and name(node.field("constructor")) == "Promise":
            args = arguments(node)
            callback = self.callback_target(args[0]) if args else None
            return self.invoke(callback, conditional, catches, detached=not self.completed(node))
        if kind in {"if_statement", "ternary_expression"}:
            condition = node.field("condition")
            consequent = node.field("consequence")
            alternate = node.field("alternative")
            leading = self.visit(condition, conditional, catches)
            operator, left, right = split_operands(expression(condition))
            if (not catches and alternate is None and operator in {"!==", "!="}
                    and name(left) and right.kind() in {"identifier", "undefined"}
                    and right.text() == "undefined" and consequent.kind() == "throw_statement"
                    and shape(left) == shape(next(iter(children(consequent)), None))):
                return followed_by(leading, self.record(Check(node, "throw"), conditional))
            known = truth(condition)
            first = self.visit(consequent, conditional or known is not True, catches)
            second = self.visit(alternate, conditional or known is not False, catches)
            return followed_by(leading, first if known is True else second if known is False else first | second)
        if kind == "binary_expression":
            operator, left, right = split_operands(node)
            if operator in {"&&", "||", "??"}:
                value = literal(left)
                runs = (truth(left) if operator == "&&" else inverse(truth(left)) if operator == "||"
                        else lint.UNKNOWN if value is lint.UNKNOWN else value is None)
                leading = self.visit(left, conditional, catches)
                trailing = self.visit(right, conditional or runs is not True, catches)
                return followed_by(leading, trailing if runs is True else trailing | NEXT)
        if kind == "switch_statement":
            return self.switch(node, conditional, catches)
        if kind in {"for_statement", "for_in_statement", "while_statement", "do_statement"}:
            return self.loop(node, conditional, catches)
        if kind == "try_statement":
            catcher = node.field("handler")
            body = self.visit(node.field("body"), conditional or catcher is not None, catches or catcher is not None)
            if catcher is not None:
                body = (body & ~THROW) | self.visit(catcher, True, catches)
            final = self.visit(node.field("finalizer"), conditional, catches)
            return ((body if final & NEXT else 0) | (final & ~NEXT))
        if kind in {"return_statement", "throw_statement"}:
            result = self.sequence(children(node), conditional, catches)
            if kind == "throw_statement" and not catches:
                return self.record(Check(node, "throw"), conditional)
            return (THROW if kind == "throw_statement" else RETURN) if result else 0
        if kind in {"break_statement", "continue_statement"}:
            return BREAK if kind == "break_statement" else CONTINUE
        if kind == "member_expression" and (check := self.file.property_check(node)) is not None:
            result = self.sequence(children(node), conditional, catches)
            detached = check.asynchronous and not self.completed(node)
            return followed_by(result, self.record(check, conditional, self.optional(node) or detached))
        return self.sequence(children(node), conditional, catches)

    def switch(self, node, conditional, catches):
        leading = self.visit(node.field("value"), conditional, catches)
        cases = []
        default = False
        for case in children(node.field("body")):
            value = case.field("value")
            self.visit(value, True, catches)
            default |= value is None
            cases.append(self.sequence((child for child in children(case) if not same(child, value)), True, catches))
        tail = NEXT
        entries = 0 if default else NEXT
        for arm in reversed(cases):
            tail = followed_by(arm, tail)
            entries |= tail
        if entries & BREAK:
            entries = (entries & ~BREAK) | NEXT
        return followed_by(leading, entries)

    def loop(self, node, conditional, catches):
        kind = node.kind()
        body = node.field("body")
        condition = node.field("condition")
        leading = self.sequence((node.field("initializer"), node.field("right"), condition), conditional, catches)
        if kind == "for_in_statement":
            iterable = node.field("right")
            is_of = any(child.kind() == "of" for child in node.children())
            nonempty = self.nonempty(iterable, keys=not is_of)
        elif kind == "for_statement":
            initializer = node.field("initializer")
            declarations = children(initializer) if initializer is not None else []
            declaration = declarations[0] if len(declarations) == 1 and declarations[0].kind() == "variable_declarator" else None
            op, left, right = split_operands(condition)
            lower = self.file.fixture_number(declaration.field("value")) if declaration is not None else lint.UNKNOWN
            upper = self.file.fixture_number(right)
            nonempty = (declaration is not None and bool(name(left)) and name(left) == name(declaration.field("name"))
                        and lower is not lint.UNKNOWN and upper is not lint.UNKNOWN
                        and compare(op, lower, upper) is True) or truth(condition) is True
        else:
            nonempty = kind == "do_statement" or truth(condition) is True
        checked = self.visit(body, conditional or not nonempty, catches)
        self.visit(node.field("increment"), True, catches)
        if not nonempty or checked & (NEXT | BREAK | CONTINUE):
            checked = (checked & (RETURN | THROW)) | NEXT
        return followed_by(leading, checked)

    def call(self, node, conditional, catches):
        registration = self.file.registrations.get(offset(node))
        if registration is not None:
            if registration.context_call and not registration.ignored:
                return self.record(Check(node, "subtest"), conditional, not self.completed(node))
            return NEXT
        callee = expression(node.field("function"))
        args = arguments(node)
        result = self.sequence([callee, *args], conditional, catches)
        optional = self.optional(node)
        guarded = conditional or optional
        if (check := self.file.check(node, self.context)) is not None:
            detached = check.asynchronous and not self.completed(node)
            coverage = self.record(check, guarded, optional or detached)
            if check.method == "fail" and catches and not optional:
                coverage = THROW
            return followed_by(result, coverage)

        receiver, members = chain(callee)
        method = members[-1] if members else ""
        receiver = callee.field("object") if method else receiver
        if method in ITERATORS | CHAINS:
            threaded = method in CHAINS and self.completed(node)
            for index, callback in enumerate(args[:2] if method == "then" else args[:1]):
                callback = self.callback_target(callback)
                if callback is None:
                    continue
                if method in ITERATORS:
                    nonempty = self.nonempty(receiver) and method not in {"reduce", "reduceRight"}
                    asynchronous = is_async(callback)
                    joined = method in {"map", "flatMap"} and self.completed(node, array=True)
                    detached = asynchronous and not joined
                    bypass = optional or not nonempty or detached
                else:
                    detached = not threaded
                    bypass = optional or detached or method == "catch" or index > 0
                coverage = self.invoke(callback, guarded or bypass, catches, detached=detached)
                if not bypass and not coverage:
                    result = followed_by(result, 0)
            return result

        if callee is not None and callee.kind() in FUNCTIONS:
            detached = is_async(callee) and not self.completed(node)
            return followed_by(result, self.invoke(callee, guarded, catches, detached=detached))

        _, names = self.file.callee(callee)
        if names == ("test", "step") and len(args) >= 2:
            detached = not self.completed(node)
            return followed_by(result, self.invoke(self.callback_target(args[1]), guarded, catches, detached=detached))

        callback = self.callbacks.get(name(callee))
        if callback is not None and self.file.binding(callee) is None:
            detached = is_async(callback) and not self.completed(node)
            coverage = self.invoke(callback, guarded, catches, detached=detached)
            return followed_by(result, NEXT if optional else coverage)

        target = self.file.helper(callee)
        joined = self.completed(node)
        callbacks = {key: callback for key, arg in zip(formal_parameters(target), args)
                     if key and (callback := self.callback_target(arg)) is not None} if target is not None else {}
        if target is not None and offset(target) not in self.active and target.field("body") is not None:
            nested = Flow(self.file, target, (), self.active, callbacks)
            incomplete = nested.visit(nested.body)
            detached = is_async(target) and not joined
            forwarded = [check for check in nested.checks
                         if any(within(check.node, callback) for callback in callbacks.values())]
            for check in forwarded:
                self.record(check, guarded or check.guarded, optional or detached)
            if forwarded:
                return followed_by(result, NEXT if optional or detached or incomplete else 0)
            checks = [check for check in nested.checks if check.method != "result helper"]
            if not checks and len(children(nested.body)) == 1:
                checks = nested.checks
            if checks:
                failure_helper = all(check.method in {"throw", "fail"} for check in checks)
                if not failure_helper or not callbacks:
                    return followed_by(result, self.record(Check(node, "helper"), guarded,
                                                           optional or detached or bool(incomplete) and not failure_helper))
            return result

        if joined and not optional and target is None:
            for arg in args:
                callback = expression(arg)
                if callback is not None and callback.kind() in FUNCTIONS and self.check_callback(callback):
                    count = len(self.checks)
                    coverage = self.invoke(callback, conditional, catches, detached=False)
                    if any(check.method != "throw" for check in self.checks[count:]):
                        result = followed_by(result, coverage)
                    else:
                        del self.checks[count:]
        return result

    def check_callback(self, callback):
        body = callback.field("body")
        if body is None:
            return False
        if body.kind() != "statement_block":
            return body.kind() == "call_expression" and self.file.check(body, self.context) is not None
        # An opaque wrapper may return a failure without ever invoking a result-producing callback.
        return not any(children(node) and not any(
            parent.kind() in FUNCTIONS and within(parent, body) for parent in node.ancestors()
        ) for node in query(body, "return_statement"))

    def invoke(self, callback, conditional, catches, *, detached):
        if callback is None or offset(callback) in self.active or callback.field("body") is None:
            return NEXT
        nested = Flow(self.file, callback, self.context, self.active)
        remaining = nested.visit(nested.body, conditional or detached, catches or detached)
        self.checks.extend(nested.checks)
        return NEXT if detached or remaining else 0

    def callback_target(self, node):
        node = expression(node)
        if node is not None and node.kind() in FUNCTIONS:
            return node
        return self.file.helper(node)

    def optional(self, node):
        current = node
        while current is not None and current.kind() in {"call_expression", "member_expression"}:
            if any(child.kind() == "optional_chain" or child.kind() == "?." for child in current.children()):
                return True
            current = current.field("function") or current.field("object")
        return False

    def references_to(self, node, binding=None):
        if self.references is None:
            self.references = {}
            for reference in query(self.body, "identifier"):
                self.references.setdefault(reference.text(), []).append(reference)
        binding = binding or self.file.binding(node)
        return [ref for ref in self.references.get(name(node), ()) if same(self.file.binding(ref), binding)]

    def completed(self, node, *, array=False, seen=frozenset()):
        """Follow a promise value through return, await and supported forwarding expressions."""
        parent = node.parent()
        while parent is not None:
            kind = parent.kind()
            if not array and kind in {"await_expression", "return_statement"}:
                return True
            if kind in FUNCTIONS:
                return not array and same(node, parent.field("body"))
            if kind == "variable_declarator" and same(node, parent.field("value")):
                target = parent.field("name")
                if not name(target) or offset(parent) in seen or parent.parent().children()[0].kind() != "const":
                    return False
                statement = parent.parent()
                block = statement.parent()
                if block.kind() != "statement_block":
                    return False
                for subsequent in children(block):
                    if offset(subsequent) <= offset(statement):
                        continue
                    expression_node = next(iter(children(subsequent)), None)
                    if subsequent.kind() == "return_statement" or (
                            subsequent.kind() == "expression_statement" and expression_node is not None
                            and expression_node.kind() == "await_expression"):
                        if any(within(ref, subsequent) and self.completed(
                                ref, array=array, seen=seen | {offset(parent)}) for ref in self.references_to(target, parent)):
                            return True
                    if subsequent.kind() in {"if_statement", "try_statement", "return_statement", "throw_statement",
                                             "switch_statement", "break_statement", "continue_statement"}:
                        return False
                return False
            if kind in TRANSPARENT or (kind == "ternary_expression" and not same(node, parent.field("condition"))):
                pass
            elif kind == "binary_expression" and split_operands(parent)[0] in {"&&", "||", "??"}:
                _, _, right = split_operands(parent)
                if not same(node, right):
                    return False
            elif kind == "array" and not array:
                array = True
            elif kind == "arguments":
                call = parent.parent()
                if call is None or call.kind() != "call_expression":
                    return False
                _, names = self.file.callee(call.field("function"))
                if array and names == ("Promise", "all"):
                    array = False
                elif array or names != ("Promise", "resolve"):
                    return False
                parent = call
            elif kind == "member_expression" and not array and same(node, parent.field("object")):
                call = parent.parent()
                if (name(parent.field("property")) not in CHAINS or call is None or
                        not same(parent, call.field("function"))):
                    return False
                parent = call
            else:
                return False
            node, parent = parent, parent.parent()
        return False

    def nonempty(self, node, *, keys=False, supplied=None, seen=frozenset()):
        node, supplied = self.file.fixed(node, supplied)
        if node is None:
            return False
        span = offset(node), node.range().end.index
        if span in seen:
            return False
        seen = seen | {span}
        kind = node.kind()
        if keys and kind == "object":
            return any(item.kind() in {"pair", "method_definition", "shorthand_property_identifier"}
                       for item in children(node))
        if kind == "array":
            return any(self.nonempty(children(item)[0], supplied=supplied, seen=seen) if item.kind() == "spread_element"
                       else True for item in children(node))
        size = self.file.fixture_size(node, supplied)
        if type(size) is int and size > 0:
            return True
        if kind == "call_expression" and not self.optional(node):
            fn = expression(node.field("function"))
            receiver, methods = chain(fn)
            args = arguments(node)
            if name(receiver) == "Object" and methods in {("entries",), ("keys",), ("values",)} and args:
                return self.nonempty(args[0], keys=True, supplied=supplied, seen=seen)
            if name(receiver) == "Array" and methods == ("from",) and args:
                return self.nonempty(args[0], supplied=supplied, seen=seen)
            if methods and methods[-1] in {"map", "values", "entries", "keys", "toReversed", "toSorted"}:
                return self.nonempty(fn.field("object"), keys=keys, supplied=supplied, seen=seen)
            if methods and methods[-1] == "querySelectorAll" and args:
                return self.inserted_selector(fn.field("object"), args[0], node)
        return False

    def inserted_selector(self, receiver, selector_node, query_node):
        selector = text_literal(selector_node)
        if selector is None or re.fullmatch(r"[.#]?[\w-]+", selector) is None:
            return False
        statement = query_node
        while statement.parent() is not None and statement.parent().kind() != "statement_block":
            statement = statement.parent()
        if statement.parent() is None:
            return False
        for previous in reversed(children(statement.parent())):
            if offset(previous) >= offset(statement):
                continue
            if previous.kind() == "lexical_declaration":
                continue
            assignment = next(iter(children(previous)), None)
            if previous.kind() != "expression_statement" or assignment is None or assignment.kind() != "assignment_expression":
                return False
            target = assignment.field("left")
            if name(target.field("property")) != "innerHTML" or shape(target.field("object")) != shape(receiver):
                return False
            markup = literal(assignment.field("right"))
            if not isinstance(markup, str):
                return False
            if selector.startswith("."):
                return any(selector[1:] in classes.split()
                           for classes in re.findall(r"""class=["']([^"']*)["']""", markup))
            if selector.startswith("#"):
                return selector[1:] in re.findall(r"""id=["']([^"']*)["']""", markup)
            return re.search(r"<" + re.escape(selector) + r"(?=[\s/>])", markup, re.I) is not None
        return False

    def compares_itself(self, check, test_name, notes):
        left, right = expression(check.actual), expression(check.expected)
        if not check.operator or left is None or right is None or shape(left) != shape(right):
            return False
        if check.operator in {"!=", "!=="} or not query(left, "call_expression"):
            return True
        site = line(check.node)
        comment = " ".join(notes.get(position, "") for position in (site - 1, site))
        if any(describes_distinct_inputs(text) for text in (test_name, comment)):
            return True
        if any(suggests_repeatability(text) for text in (test_name, comment)):
            return False
        for companion in self.checks:
            if companion is check or not companion.operator:
                continue
            for subject, other in ((companion.actual, companion.expected), (companion.expected, companion.actual)):
                if shape(expression(subject)) == shape(left) and (
                        companion.operator in {"!=", "!=="} and shape(expression(other)) != shape(left)
                        or companion.operator in {"==", "==="} and literal(other) is not lint.UNKNOWN):
                    return False
        return True


def inverse(value):
    return not value if value is not lint.UNKNOWN else value
