"""Track which JVM test exits include a check and a positive recorder observation."""

from dataclasses import dataclass, replace
from functools import reduce

from . import lint
from .ast_grep import children, query, shape, unwrap
from .ast_grep_jvm import (
    CALLS, CLASSES, FUNCTIONS, LAMBDAS, bind_arguments, body_of, call_of, child, has_safe_navigation,
    member, name, node_key,
)
from .ast_grep_jvm_controls import declared_value, sized_source
from .ast_grep_jvm_fixtures import counted_list_builder, fixture_truth, java_list_fixture
from .ast_grep_jvm_checks import (
    ITERATING, SYNCHRONOUS, Check, check_call, collect_observations, compare, infix_check, iterable_size,
    literal, negate, predicate, split, truth,
)


@dataclass(frozen=True)
class Evidence:
    checked: bool = False
    witnesses: frozenset = frozenset()
    sizes: frozenset = frozenset()

    def then(self, next_evidence):
        return Evidence(self.checked or next_evidence.checked, self.witnesses | next_evidence.witnesses,
                        self.sizes | next_evidence.sizes)

    def either(self, alternative):
        return Evidence(self.checked and alternative.checked, self.witnesses & alternative.witnesses,
                        self.sizes & alternative.sizes)


def merge(evidence):
    alternatives = iter(evidence)
    first = next(alternatives, Evidence())
    return reduce(Evidence.either, alternatives, first)


NORMAL = {"next": Evidence()}


def alternatives(*branches):
    outcomes = {}
    for branch in branches:
        for reason, evidence in branch.items():
            previous = outcomes.get(reason)
            outcomes[reason] = evidence if previous is None else previous.either(evidence)
    return outcomes


def followed_by(before, after):
    return alternatives({reason: evidence for reason, evidence in before.items() if reason != "next"},
                        {reason: before["next"].then(evidence) for reason, evidence in after.items()})


def finish(exits, *local_exits):
    return alternatives(*({"next" if reason in local_exits else reason: evidence}
                          for reason, evidence in exits.items()))


def throws_assertion_error(node):
    for entry in children(node):
        call = call_of(entry)
        if call is not None and call.method == "AssertionError":
            return True
        if entry.kind() == "object_creation_expression" and name(entry.field("type")) == "AssertionError":
            return True
    return False


def failure_statement(node):
    node = unwrap(node)
    if node is None:
        return False
    if node.kind() == "expression_statement":
        node = children(node)[0]
    call = call_of(node)
    return (call is not None and call.method == "fail"
            or node.kind() in {"jump_expression", "throw_statement"} and throws_assertion_error(node))


def expects_failure(declaration):
    body = body_of(declaration)
    entries = children(body)
    tried = entries[0] if len(entries) == 1 else body
    if tried.kind() == "statements" and len(children(tried)) == 1:
        tried = children(tried)[0]
    if tried.kind() not in {"try_expression", "try_statement"} or child(tried, "catch_block", "catch_clause") is None:
        return False
    block = tried.field("body") or child(tried, "statements", "block")
    steps = children(block) if block is not None else []
    if len(steps) < 2 or not any(failure_statement(step) for step in steps[1:]):
        return False
    call = call_of(steps[0])
    parameters = child(declaration, "function_value_parameters")
    return call is not None and parameters is not None and any(
        name(call.callee) == name(child(parameter, "simple_identifier"))
        and child(parameter, "function_type") is not None for parameter in children(parameters))


def loop_steps(loop):
    body = child(loop, "control_structure_body")
    statements = child(body, "statements")
    return children(statements or body) if body is not None else []


def loop_jumps(loop):
    exits = ["break", "continue"]
    label = loop.prev()
    while label is not None and label.kind() == "label":
        target = label.text().removesuffix("@")
        exits.extend((f"break@{target}", f"continue@{target}"))
        label = label.prev()
    return exits


def timeout_poll(callback):
    body = body_of(callback)
    steps = children(body) if body is not None else []
    if len(steps) != 1 or steps[0].kind() != "while_statement":
        return False
    waiting = loop_steps(steps[0])
    return len(waiting) == 1 and (call := call_of(waiting[0])) is not None and call.method == "delay"


def deadline_poll(loop):
    condition = next(iter(children(loop)), None)
    if negate(condition) is None:
        return False
    steps = loop_steps(loop)
    if len(steps) < 2 or steps[0].kind() != "if_expression":
        return False
    branches = children(steps[0])
    if len(branches) != 2 or len(children(branches[1])) != 1:
        return False
    operator, left, right = split(branches[0])
    clock = left if operator in {">", ">="} else right if operator in {"<", "<="} else None
    call = call_of(clock)
    return (call is not None and call.method in {"currentTimeMillis", "nanoTime", "elapsedRealtime"}
            and failure_statement(children(branches[1])[0]))


class Flow:
    def __init__(self, file, sizes=None):
        self.file = file
        self.sizes = sizes or {}
        self.fixture_item = None
        self.checks = {}
        self.active = set()
        self.invocation = None
        self.bound = {}
        self.opaque_check = False
        self.loop_checks = {}

    def sequence(self, nodes):
        exits = NORMAL
        for node in nodes:
            if "next" not in exits:
                break
            exits = followed_by(exits, self.visit(node))
        return exits

    def record(self, check):
        site = self.invocation if self.invocation is not None else check.node
        self.checks[node_key(site), (*node_key(check.node), str(self.file.path))] = replace(check, node=site)
        return {"next": Evidence(True, frozenset(key for key, present in check.observations if present), frozenset(check.sizes))}

    def visit(self, node):
        node = unwrap(node)
        if node is None:
            return NORMAL
        kind = node.kind()
        if kind in FUNCTIONS | CLASSES | LAMBDAS | {"modifiers", "class_body"}:
            return NORMAL
        if kind in CALLS:
            return self.call(node)
        if kind in {"if_expression", "if_statement", "ternary_expression"}:
            return self.branch(node)
        if kind in {"for_statement", "enhanced_for_statement", "while_statement", "do_while_statement", "do_statement"}:
            return self.loop(node)
        if kind in {"when_expression", "switch_expression", "switch_statement"}:
            return self.selection(node)
        if kind in {"try_expression", "try_statement", "try_with_resources_statement"}:
            return self.try_block(node)
        if kind in {"jump_expression", "return_statement", "break_statement", "continue_statement",
                    "throw_statement", "yield_statement"}:
            return self.jump(node)
        if kind == "assert_statement":
            entries = children(node)
            if entries:
                evaluated = self.visit(entries[0])
                if "next" in evaluated:
                    return followed_by(evaluated, self.record(collect_observations(
                        self.file, predicate(node, entries[0], False))))
                return evaluated
        if kind == "infix_expression" and (check := infix_check(self.file, node)) is not None:
            evaluated = self.sequence(children(node))
            return followed_by(evaluated, self.record(check)) if "next" in evaluated else evaluated
        operator, left, right = split(node)
        if operator in {"&&", "||", "?:"}:
            evaluated = self.visit(left)
            if "next" not in evaluated:
                return evaluated
            known = truth(self.file.syntax, left)
            decisive = operator == "||"
            if operator != "?:" and known is decisive:
                return evaluated
            second = self.visit(right)
            return followed_by(evaluated, second if operator != "?:" and known is not lint.UNKNOWN
                               else alternatives(NORMAL, second))
        return self.sequence(children(node))

    def call(self, node):
        call = call_of(node)
        if call is None:
            return self.sequence(children(node))
        receiver = self.visit(call.receiver)
        if "next" not in receiver:
            return receiver
        arguments = self.sequence(call.args)
        if "next" not in arguments:
            return followed_by(receiver, arguments)
        method = self.file.resolve_name(call.method)
        callbacks = call.callbacks()
        bound = self.bound_argument(call)
        if bound is not None and bound[1].kind() in LAMBDAS:
            executed = self.invoke_callback(*bound)
        elif (local := self.file.helper(node, call)) is not None:
            executed = self.helper(node, call, *local)
        else:
            check = check_call(self.file, node, call)
            executed = self.record(check) if check is not None and method != "assertAll" else NORMAL
            if callbacks:
                callback_flow = self.callbacks(node, call, method, check)
                executed = followed_by(executed, callback_flow)
            if method == "fail":
                executed = followed_by(executed, {"assertion-failure": Evidence()})
        executed = followed_by(arguments, executed)
        if has_safe_navigation(call.callee):
            executed = alternatives(NORMAL, executed)
        return followed_by(receiver, executed)

    def bound_argument(self, call):
        target = call.callee if call.receiver is None else (
            call.receiver if call.method in {"invoke", "run", "call", "accept", "apply", "get", "test"} else None)
        return self.argument_binding(self.file, target)

    def argument_binding(self, file, node):
        _, binding = file.visible_binding(node, name(node))
        if binding is not None:
            return self.bound.get((file.path, node_key(binding)))
        return None

    def helper(self, node, call, file, declaration):
        key = file.path, node_key(declaration)
        if key in self.active:
            return NORMAL
        previous, caller = self.invocation, self.file
        if previous is None:
            self.invocation = node
        slots = {}
        for parameter, argument, supplied in bind_arguments(declaration, call):
            origin = caller if supplied else file
            forwarded = self.argument_binding(origin, argument)
            slots[file.path, node_key(parameter)] = forwarded or (origin, argument, previous)
        self.bound.update(slots)
        self.file = file
        self.active.add(key)
        try:
            if expects_failure(declaration):
                return self.record(Check(node))
            return finish(self.visit(body_of(declaration)), "return")
        finally:
            self.active.remove(key)
            for slot in slots:
                del self.bound[slot]
            self.file = caller
            self.invocation = previous

    def invoke_callback(self, file, callback, invocation):
        previous, caller = self.invocation, self.file
        self.file, self.invocation = file, invocation
        try:
            exits = self.visit(body_of(callback))
            return finish(exits, "return", "local-return") if callback.kind() == "lambda_expression" else finish(
                exits, "local-return")
        finally:
            self.file, self.invocation = caller, previous

    def callbacks(self, node, call, method, check):
        callbacks = call.callbacks()
        if method.startswith(("assertThrows", "assertFails", "shouldThrow")) or method in {
                "assertThatThrownBy", "isThrownBy"}:
            return NORMAL
        if method.startswith(("verify", "coVerify")):
            return NORMAL
        if method == "waitUntil" and call.receiver is not None and len(callbacks) == 1:
            body = body_of(callbacks[0])
            condition = children(body)[-1] if body is not None and children(body) else None
            return self.record(collect_observations(self.file, predicate(node, condition, False)))
        synchronous = method in SYNCHRONOUS or check is not None and method == "satisfies"
        if not synchronous and method not in ITERATING | {"launch", "async", "allSatisfy", "anySatisfy",
                                                           "noneSatisfy"}:
            self.opaque_check |= node.parent().kind() == "function_body" or any(
                self.has_callback_check(callback, node.kind()) for callback in callbacks)
            return NORMAL
        executed = NORMAL
        previous_checks = set(self.checks)
        for callback in callbacks:
            if "next" not in executed:
                break
            callback_exits = self.invoke_callback(self.file, callback, self.invocation)
            if method == "withTimeout" and timeout_poll(callback):
                callback_exits = followed_by(self.record(Check(node)), callback_exits)
            executed = followed_by(executed, callback_exits)
        if method == "assumingThat":
            known = truth(self.file.syntax, call.args[0])
            return executed if known is True else alternatives(NORMAL, executed)
        if synchronous:
            return executed
        if method in ITERATING:
            iterable = (self.known_literal(call.args[0]) if method == "repeat" and call.args
                        else self.iterable_size(self.loop_receiver(call.receiver)))
            source = self.loop_receiver(call.receiver)
            split_call = call_of(source)
            if method in {"forEach", "forEachIndexed"} and self.file.syntax.language == "kotlin" and (
                    split_call is not None and split_call.receiver is not None and split_call.method == "split"
                    and len(split_call.args) == 1 and literal(self.file.syntax, split_call.args[0]) is not lint.UNKNOWN):
                return executed
            if type(iterable) is int and iterable > 0:
                origin = call_of(self.file.immutable_value(call.receiver) or call.receiver)
                if method in {"repeat", "forEach", "forEachIndexed"} or (
                        origin is not None and origin.method != "sequenceOf"):
                    return executed
            source = call.args[0] if method == "repeat" and call.args else source
            self.remember_loop(previous_checks, source)
        return alternatives(NORMAL, finish(executed, "throw", "assertion-failure", "return", "break", "continue"))

    def loop_receiver(self, receiver):
        node = unwrap(receiver)
        if self.file.syntax.language != "kotlin" or node is None or node.kind() != "call_expression":
            return receiver
        parts = children(node)
        if len(parts) != 2 or parts[0].kind() != "call_expression" or parts[-1].kind() != "call_suffix" or (
                parts[0].range().end.line >= parts[1].range().start.line):
            return receiver
        declaration = next((entry for entry in node.ancestors() if entry.kind() == "property_declaration"), None)
        if declaration is None or declaration.range().start.column != parts[1].range().start.column:
            return receiver
        arguments = child(parts[-1], "value_arguments")
        if arguments is not None and len(children(arguments)) == 1:
            value = child(children(arguments)[0], "range_expression")
            if value is not None:
                return value
        return receiver

    def has_callback_check(self, callback, call_kind):
        if any(check_call(self.file, entry, inner) is not None
               for entry in query(callback, call_kind) if (inner := call_of(entry)) is not None):
            return True
        if self.file.syntax.language == "java":
            return bool(query(callback, "assert_statement"))
        return any(infix_check(self.file, entry) is not None for entry in query(callback, "infix_expression"))

    def remember_loop(self, previous_checks, source):
        if source is not None:
            for key in self.checks:
                if key in previous_checks:
                    continue
                self.loop_checks.setdefault(key, source)

    def known_literal(self, node, file=None, bindings=None, active=frozenset()):
        file = file or self.file
        node = unwrap(node)
        if node is not None and (identifier := name(node)):
            scope, binding = file.visible_binding(node, identifier)
            if binding is not None:
                if (bound := self.bound_value(node, file, bindings)) is not None:
                    origin, argument = bound
                    return literal(origin.syntax, origin.immutable_value(unwrap(argument)) or argument)
                node = file.stable_initializer(node, scope, binding) or node
        receiver, measurement = member(node)
        called = call_of(node)
        if called is not None and not called.args:
            receiver, measurement = called.receiver, called.method
        if measurement in {"size", "length", "count"} and receiver is not None:
            if (size := self.iterable_size(receiver, file, bindings, active)) is not None:
                return size
        return literal(file.syntax, node)

    def iterable_size(self, node, file=None, bindings=None, active=frozenset()):
        file = file or self.file
        node = unwrap(node)
        if node is None:
            return None
        key = file.path, node_key(node)
        if key in active:
            return None
        active = active | {key}
        if (bound := self.bound_value(node, file, bindings)) is not None:
            defining_file, value = bound
            return self.iterable_size(value, defining_file, bindings, active)
        if self.sizes and (source := sized_source(file, node)) in self.sizes:
            return self.sizes[source]
        value = file.immutable_value(node) or file.local_array(node)
        if value is not None:
            origin = call_of(value)
            if origin is not None and origin.method.startswith("mutable"):
                return None
            return self.iterable_size(value, file, bindings, active)
        call = call_of(node)
        receiver, component = (call.receiver, call.method) if call is not None else member(node)
        if component in {"values", "entries"} and (identifier := name(receiver))[:1].isupper():
            target = file.type_named(identifier)
            if target is not None and (body := child(target[1], "enum_class_body", "enum_body")) is not None:
                return sum(entry.kind() in {"enum_entry", "enum_constant"} for entry in children(body))
        if call is None and receiver is not None:
            if component in {"values", "keys", "entries"}:
                size = self.iterable_size(receiver, file, bindings, active)
                if size is not None:
                    return size
            if (property_value := file.object_property(receiver, component)) is not None:
                defining_file, value = property_value
                return self.iterable_size(value, defining_file, bindings, active)
        if call is not None and file.syntax.language == "kotlin":
            if call.method == "List" and call.receiver is None and call.args and call.callbacks():
                count = self.known_literal(call.args[0], file, bindings)
                return count if type(count) is int and count >= 0 else None
            if call.method in {"map", "mapIndexed"} and call.receiver is not None:
                return self.iterable_size(call.receiver, file, bindings, active)
            if call.method == "chunked" and call.receiver is not None and call.args:
                size = self.iterable_size(call.receiver, file, bindings, active)
                width = self.known_literal(call.args[0], file, bindings)
                if size is not None and type(width) is int and width > 0:
                    return (size + width - 1) // width
            if call.method in {"let", "run"} and len(call.callbacks()) == 1:
                body = body_of(call.callbacks()[0])
                steps = children(body) if body is not None else []
                if len(steps) == 1:
                    return self.iterable_size(steps[0], file, bindings, active)
            if call.named and (count := counted_list_builder(file, call)) is not None:
                return count
            if (local := file.helper(node, call)) is not None:
                defining_file, declaration = local
                key = defining_file.path, node_key(declaration)
                if key in active:
                    return None
                body = body_of(declaration)
                steps = children(body)
                if len(steps) == 1 and steps[0].kind() == "statements":
                    steps = children(steps[0])
                    if len(steps) == 1 and steps[0].kind() == "jump_expression" and (
                            steps[0].children()[0].kind() == "return"):
                        steps = children(steps[0])
                    else:
                        return None
                if len(steps) == 1:
                    slots = dict(bindings or {})
                    slots.update({(defining_file.path, node_key(parameter)): (file if supplied else defining_file, value)
                                  for parameter, value, supplied in bind_arguments(declaration, call)})
                    return self.iterable_size(steps[0], defining_file, slots, active | {key})
        if node.kind() in {"range_expression", "infix_expression"}:
            entries = children(node)
            if len(entries) in {2, 3}:
                operator = (name(entries[1]) if len(entries) == 3 else
                            next((token.kind() for token in node.children() if token.kind() in {"..", "..<"}), ""))
                first, last = (self.known_literal(entry, file, bindings, active)
                               for entry in (entries[0], entries[-1]))
                if type(first) is type(last) is int:
                    if operator in {"..", "..<", "until"}:
                        return len(range(first, last + int(operator == ".."))) if abs(first - last) < 2**63 - 1 else None
                    if operator == "downTo":
                        return len(range(first, last - 1, -1)) if abs(first - last) < 2**63 - 1 else None
        return iterable_size(file.syntax, node)

    def filtered_source(self, source):
        seen = set()
        while source is not None and node_key(source) not in seen:
            seen.add(node_key(source))
            called = call_of(source)
            if called is not None:
                if called.method in {"filter", "filterNot", "filterNotNull", "mapNotNull", "mapIndexedNotNull"}:
                    return True
                source = called.receiver
            else:
                receiver, _ = member(source)
                source = receiver or declared_value(self.file, source)[2]
        return False

    def bound_value(self, node, file, bindings):
        if not (identifier := name(node)):
            return None
        _, binding = file.visible_binding(node, identifier)
        if binding is None:
            return None
        key = file.path, node_key(binding)
        if bindings is not None and key in bindings:
            return bindings[key]
        if key in self.bound:
            argument_file, argument, _ = self.bound[key]
            return argument_file, argument
        return None

    def branch(self, node):
        if node.kind() in {"if_statement", "ternary_expression"}:
            condition = node.field("condition")
            positive = node.field("consequence")
            negative = node.field("alternative")
        else:
            elements = children(node)
            condition, positive, negative = (elements + [None] * 3)[:3]
        evaluated = self.visit(condition)
        if "next" not in evaluated:
            return evaluated
        known = (fixture_truth(self.file, condition, *self.fixture_item) if self.fixture_item is not None
                 else truth(self.file.syntax, condition))
        branches = []
        if known is not False:
            branches.append(self.visit(positive))
        if known is not True:
            branches.append(self.visit(negative))
        return followed_by(evaluated, alternatives(*branches))

    def selection(self, node):
        if node.kind() == "when_expression":
            subject = child(node, "when_subject")
            cases = [entry for entry in children(node) if entry.kind() == "when_entry"]
            exhaustive = any(any(token.kind() == "else" for token in entry.children()) for entry in cases)
            branches = [self.visit(child(entry, "control_structure_body")) for entry in cases]
        else:
            subject = node.field("condition")
            block = node.field("body")
            cases = children(block) if block is not None else []
            exhaustive = any(any(token.kind() == "default" for token in label.children())
                             for case in cases for label in children(case) if label.kind() == "switch_label")
            branches = []
            tail = NORMAL
            for case in reversed(cases):
                branch = self.sequence(children(case))
                if case.kind() == "switch_block_statement_group" and "next" in branch:
                    branch = followed_by(branch, tail)
                tail = branch
                branches.append(branch)
        if not exhaustive:
            branches.append(NORMAL)
        evaluated = self.visit(subject)
        return followed_by(evaluated, finish(alternatives(*branches), "break", "yield")) if "next" in evaluated else evaluated

    def loop(self, node):
        kind = node.kind()
        elements = children(node)
        if kind == "for_statement" and self.file.syntax.language == "kotlin":
            if len(elements) < 3:
                return self.sequence(elements)
            iterable, body = elements[-2:]
            size = self.iterable_size(iterable)
            return self.iterate(node, self.visit(iterable), body, size is not None and size > 0, iterable)
        if kind == "enhanced_for_statement":
            iterable = node.field("value")
            size = self.iterable_size(iterable)
            before = self.visit(iterable)
            body = node.field("body")
            if "next" in before and (fixtures := java_list_fixture(self.file, iterable)):
                return followed_by(before, self.fixture_loop(node, body, fixtures))
            return self.iterate(node, before, body, size is not None and size > 0, iterable)
        if self.file.syntax.language == "kotlin":
            condition, body = (elements + [None, None])[:2]
            if kind == "do_while_statement":
                body, condition = condition, body
            if kind == "while_statement" and deadline_poll(node):
                return followed_by(self.record(Check(node)), self.iterate(node, self.visit(condition), body, False))
        else:
            condition, body = node.field("condition"), node.field("body")
        if kind in {"do_while_statement", "do_statement"}:
            executed = finish(self.visit(body), "continue")
            if "next" in executed:
                executed = followed_by(executed, self.visit(condition))
            return finish(executed, "break")
        init = node.field("init")
        before = self.sequence([init, condition])
        definite = truth(self.file.syntax, condition) is True
        if kind == "for_statement" and self.file.syntax.language == "java":
            definite |= condition is None or self.first_iteration(init, condition)
        return self.iterate(node, before, body, definite, condition if kind == "for_statement" else None)

    def fixture_loop(self, loop, body, fixtures):
        previous = self.fixture_item
        exits = NORMAL
        try:
            for item in fixtures:
                if "next" not in exits:
                    break
                self.fixture_item = loop, item
                exits = followed_by(exits, finish(self.visit(body), "continue"))
                active = exits.get("next")
                if active is not None and active.checked:
                    break
        finally:
            self.fixture_item = previous
        return finish(exits, "break")

    def first_iteration(self, init, condition):
        operator, left, right = split(condition)
        declaration = child(init, "variable_declarator")
        if declaration is None or operator == "":
            return False
        if shape(left) == shape(declaration.field("name")):
            left = declaration.field("value")
        elif shape(right) == shape(declaration.field("name")):
            right = declaration.field("value")
        else:
            return False
        return compare(operator, self.known_literal(left), self.known_literal(right)) is True

    def iterate(self, loop, before, body, definite, source=None):
        if "next" not in before:
            return before
        previous_checks = set(self.checks)
        executed = finish(self.visit(body), *loop_jumps(loop))
        if not definite:
            self.remember_loop(previous_checks, source)
            executed = alternatives(NORMAL, executed)
        return followed_by(before, executed)

    def jump(self, node):
        kind = node.kind()
        token = node.children()[0].kind()
        reason = token if kind == "jump_expression" else kind.removesuffix("_statement")
        if reason.startswith("return@"):
            reason = "local-return"
        elif reason in {"break@", "continue@"} and (label := child(node, "label")) is not None:
            reason += label.text()
        evaluated = self.sequence(children(node))
        if "next" in evaluated:
            if reason == "throw" and throws_assertion_error(node):
                evaluated = followed_by(evaluated, self.record(Check(node)))
                reason = "assertion-failure"
            evaluated = followed_by(evaluated, {reason: Evidence()})
        return evaluated

    def try_block(self, node):
        entries = children(node)
        body = node.field("body") or child(node, "statements", "block")
        tried = self.visit(body)
        catches = [entry for entry in entries if entry.kind() in {"catch_block", "catch_clause"}]
        if catches:
            expected = self.expected_failure(body, catches)
            tried = alternatives(tried, *(followed_by(expected, self.visit(body_of(catch))) for catch in catches))
        cleanup = child(node, "finally_block", "finally_clause")
        if cleanup is None:
            return tried
        completed = self.sequence(children(cleanup))
        return alternatives(*(
            {reason if exit_kind == "next" else exit_kind: evidence.then(final)} for reason, evidence in tried.items() for exit_kind, final in completed.items()
        ))

    def expected_failure(self, body, catches):
        steps = children(body) if body is not None else []
        if len(steps) < 2 or not failure_statement(steps[-1]):
            return NORMAL
        for catch in catches:
            caught = child(catch, "user_type") or child(child(catch, "catch_formal_parameter"), "catch_type")
            if caught is None or any(entry.text().rsplit(".", 1)[-1] in {"Error", "Throwable", "AssertionError"}
                                     for entry in children(caught)):
                return NORMAL
        return {"next": Evidence(checked=True)}
