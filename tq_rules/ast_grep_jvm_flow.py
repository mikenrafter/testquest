"""Track which JVM test exits include a check and a positive recorder observation."""

from dataclasses import dataclass, replace
from functools import reduce

from . import lint
from .ast_grep import children, shape, unwrap
from .ast_grep_jvm import (
    CALLS, CLASSES, FUNCTIONS, LAMBDAS, body_of, call_of, child, has_safe_navigation, node_key,
)
from .ast_grep_jvm_checks import (
    ITERATING, SYNCHRONOUS, check_call, collect_observations, compare, infix_check, iterable_size,
    literal, predicate, split, truth,
)


@dataclass(frozen=True)
class Evidence:
    checked: bool = False
    witnesses: frozenset = frozenset()

    def then(self, next_evidence):
        return Evidence(self.checked or next_evidence.checked, self.witnesses | next_evidence.witnesses)

    def either(self, alternative):
        return Evidence(self.checked and alternative.checked, self.witnesses & alternative.witnesses)


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


class Flow:
    def __init__(self, file):
        self.file = file
        self.checks = {}
        self.active = set()
        self.invocation = None

    def sequence(self, nodes):
        exits = NORMAL
        for node in nodes:
            if "next" not in exits:
                break
            exits = followed_by(exits, self.visit(node))
        return exits

    def record(self, check):
        site = self.invocation if self.invocation is not None else check.node
        self.checks[node_key(site), node_key(check.node)] = replace(check, node=site)
        return {"next": Evidence(True, frozenset(key for key, present in check.observations if present))}

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
        local = self.file.helper(node, call)
        if local is not None:
            executed = self.helper(node, local)
        else:
            check = check_call(self.file, node, call)
            executed = self.record(check) if check is not None and method != "assertAll" else NORMAL
            if callbacks:
                callback_flow = self.callbacks(call, method, check)
                executed = followed_by(executed, callback_flow)
        executed = followed_by(arguments, executed)
        if has_safe_navigation(call.callee):
            executed = alternatives(NORMAL, executed)
        return followed_by(receiver, executed)

    def helper(self, call, declaration):
        key = node_key(declaration)
        if key in self.active:
            return NORMAL
        previous = self.invocation
        if previous is None:
            self.invocation = call
        self.active.add(key)
        try:
            return finish(self.visit(body_of(declaration)), "return")
        finally:
            self.active.remove(key)
            self.invocation = previous

    def callbacks(self, call, method, check):
        callbacks = call.callbacks()
        if method.startswith(("assertThrows", "assertFails", "shouldThrow")) or method in {
                "assertThatThrownBy", "isThrownBy"}:
            return NORMAL
        if method.startswith(("verify", "coVerify")):
            return NORMAL
        executed = NORMAL
        for callback in callbacks:
            if "next" not in executed:
                break
            body = body_of(callback)
            if body is None:
                continue
            callback_exits = self.visit(body)
            if callback.kind() == "lambda_expression":
                callback_exits = finish(callback_exits, "return")
            executed = followed_by(executed, finish(callback_exits, "local-return"))
        if method == "assumingThat":
            known = truth(self.file.syntax, call.args[0])
            return executed if known is True else alternatives(NORMAL, executed)
        if method in SYNCHRONOUS or check is not None and method == "satisfies":
            return executed
        if method in ITERATING:
            iterable = (self.known_literal(call.args[0]) if method == "repeat"
                        else self.iterable_size(call.receiver))
            if type(iterable) is int and iterable > 0:
                origin = call_of(self.file.immutable_value(call.receiver) or call.receiver)
                if method in {"repeat", "forEach", "forEachIndexed"} or (
                        origin is not None and origin.method != "sequenceOf"):
                    return executed
        return alternatives(NORMAL, finish(executed, "throw", "return", "break", "continue"))

    def known_literal(self, node):
        return literal(self.file.syntax, self.file.immutable_value(unwrap(node)) or node)

    def iterable_size(self, node):
        value = self.file.immutable_value(unwrap(node))
        if value is not None:
            origin = call_of(value)
            if origin is not None and origin.method.startswith("mutable"):
                return None
        return iterable_size(self.file.syntax, value or node)

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
        known = truth(self.file.syntax, condition)
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
            return self.iterate(self.visit(iterable), body, size is not None and size > 0)
        if kind == "enhanced_for_statement":
            iterable = node.field("value")
            size = self.iterable_size(iterable)
            return self.iterate(self.visit(iterable), node.field("body"), size is not None and size > 0)
        if self.file.syntax.language == "kotlin":
            condition, body = (elements + [None, None])[:2]
            if kind == "do_while_statement":
                body, condition = condition, body
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
        return self.iterate(before, body, definite)

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

    def iterate(self, before, body, definite):
        if "next" not in before:
            return before
        executed = finish(self.visit(body), "break", "continue")
        if not definite:
            executed = alternatives(NORMAL, executed)
        return followed_by(before, executed)

    def jump(self, node):
        kind = node.kind()
        token = node.children()[0].kind()
        reason = token if kind == "jump_expression" else kind.removesuffix("_statement")
        if reason.startswith("return@"):
            reason = "local-return"
        evaluated = self.sequence(children(node))
        if "next" in evaluated:
            evaluated = followed_by(evaluated, {reason: Evidence()})
        return evaluated

    def try_block(self, node):
        entries = children(node)
        body = node.field("body") or child(node, "statements", "block")
        tried = self.visit(body)
        catches = [entry for entry in entries if entry.kind() in {"catch_block", "catch_clause"}]
        if catches:
            tried = alternatives(tried, *(self.visit(body_of(catch)) for catch in catches))
        cleanup = child(node, "finally_block", "finally_clause")
        if cleanup is None:
            return tried
        completed = self.sequence(children(cleanup))
        return alternatives(*(
            {reason if exit_kind == "next" else exit_kind: evidence.then(final)} for reason, evidence in tried.items() for exit_kind, final in completed.items()
        ))
