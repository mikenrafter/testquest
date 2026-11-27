"""Track which JVM test exits include a check and a positive recorder observation."""

from collections import defaultdict
from dataclasses import dataclass, replace
from functools import reduce
import re

from . import lint
from .ast_grep import children, offset, query, shape, unwrap
from .ast_grep_jvm import (
    CALLS, CLASSES, FUNCTIONS, LAMBDAS, MODULE_MARKERS, _repo_package_directories, _seeded_then_drained,
    bind_arguments, body_of, call_of, child, declaration_name, has_safe_navigation, member, name, node_key,
    read_file,
)
from .ast_grep_jvm_signatures import aggregate, executable_callbacks, signature
from .source import project_root
from .ast_grep_micro import branch_path, statement
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


def _platform_kind(file, condition):
    call = call_of(unwrap(condition))
    if call is None or call.method != "isAbsolute" or call.args or call.receiver is None:
        return ""
    base = unwrap(call.receiver)
    arg = None
    if base.kind() == "object_creation_expression" and name(base.field("type")) == "File":
        arg = next(iter(children(child(base, "argument_list"))), None)
    elif (factory := call_of(base)) is not None and name(factory.receiver) in {"Path", "Paths"} and factory.method in {
            "of", "get"} and len(factory.args) == 1:
        arg = factory.args[0]
    text = literal(file.syntax, arg)
    if not isinstance(text, str):
        return ""
    if re.match(r"^(?:[A-Za-z]:[/\\]|\\\\)", text):
        return "windows"
    return "unix" if text.startswith("/") else ""


def _block_added_count(file, block, ident, flow=None, start=0, end=None):
    count = 0
    for stmt in children(block):
        pos = offset(stmt)
        if pos < start or (end is not None and pos >= end):
            continue
        if stmt.kind() == "expression_statement":
            called = call_of(next(iter(children(stmt)), None))
            if called is not None and name(called.receiver) == ident and called.method in {
                    "add", "offer", "push", "put", "putIfAbsent"}:
                count += 1
        elif flow is not None and stmt.kind() == "for_statement" and (end is None or stmt.range().end.index <= end):
            cond = stmt.field("condition")
            if cond is None or flow.first_iteration(stmt.field("init"), cond):
                body = stmt.field("body")
                if body is not None:
                    count += _block_added_count(file, body, ident, flow)
        elif flow is not None and stmt.kind() == "enhanced_for_statement" and (end is None or stmt.range().end.index <= end):
            size = flow.iterable_size(stmt.field("value"), file)
            if type(size) is int and size > 0:
                body = stmt.field("body")
                if body is not None:
                    count += _block_added_count(file, body, ident, flow)
    return count


def _local_added_count(file, node, ident, flow=None):
    scope, binding = file.visible_binding(node, ident)
    if binding is None or scope is None or scope.kind() in CLASSES:
        return 0
    init = file.initializer(binding)
    if init is None or init.kind() != "object_creation_expression":
        return 0
    created_node = init.field("type")
    created = created_node.text().split("<", 1)[0].rsplit(".", 1)[-1] if created_node is not None else ""
    if not created.endswith(("List", "Set", "Map", "Queue", "Deque", "Collection")):
        return 0
    start, end = offset(binding), offset(node)
    for entry in query(scope, "method_invocation"):
        if not start < offset(entry) < end:
            continue
        called = call_of(entry)
        if called is not None and name(called.receiver) == ident and called.method in {
                "clear", "remove", "removeAll", "removeIf", "poll", "pop", "retainAll"}:
            return 0
    return _block_added_count(file, scope, ident, flow, start, end)


def _guarded_equals_check(condition, positive, pos_result):
    cond = unwrap(condition)
    if cond is None or cond.kind() != "unary_expression" or not cond.text().lstrip().startswith("!"):
        return False
    inner = unwrap(cond.field("operand") or next(reversed(children(cond)), None))
    call = call_of(inner)
    if call is None:
        return False
    if call.method == "equals" and call.receiver is not None and len(call.args) == 1:
        pair = {shape(call.receiver), shape(call.args[0])}
    elif call.method == "equals" and name(call.receiver) == "Objects" and len(call.args) == 2:
        pair = {shape(call.args[0]), shape(call.args[1])}
    else:
        return False
    if not (pos_result.get("next", Evidence()).checked or "assertion-failure" in pos_result):
        return False
    for entry in query(positive, "method_invocation"):
        asserted = call_of(entry)
        if asserted is None:
            continue
        if asserted.method in {"fail", "assertEquals", "assertArrayEquals"}:
            return True
    return False


def throws_assertion_error(node):
    for entry in children(node):
        call = call_of(entry)
        if call is not None and call.method == "AssertionError":
            return True
        if entry.kind() == "object_creation_expression" and name(entry.field("type")) == "AssertionError":
            return True
    return False


def failure_statement(node, *, allow_error=False):
    node = unwrap(node)
    if node is None:
        return False
    if node.kind() == "expression_statement":
        node = children(node)[0]
    call = call_of(node)
    return (call is not None and (
                call.method in {"fail", "failBecauseExceptionWasNotThrown", "failWithMessage", "expectUnreached"}
                or call.method.startswith(("expectAssertion", "shouldHaveThrown", "shouldHaveFailed"))
                or allow_error and call.method == "error" and call.receiver is None and len(call.args) == 1)
            or node.kind() in {"jump_expression", "throw_statement"} and throws_assertion_error(node))


def _is_param_call(step, param_names):
    call = call_of(step)
    if call is None:
        return False
    return call.method in param_names or call.receiver is not None and call.method == "invoke" and name(call.receiver) in param_names


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
    parameters = child(declaration, "function_value_parameters")
    if parameters is None:
        return False
    param_names = {name(child(parameter, "simple_identifier"))
                   for parameter in children(parameters)
                   if child(parameter, "function_type") is not None}
    return bool(param_names) and any(_is_param_call(step, param_names) for step in steps[:-1])


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
        self.buffered_checks = defaultdict(list)
        self.active = set()
        self.invocation = None
        self.bound = {}
        self.opaque_check = False
        self.throwing_fake_check = False
        self.loop_checks = {}
        self.last_branch_positive = None
        self.unjoined_jobs = {}
        self.last_returned_job = None
        self.structured_scope = 0
        self.structured_launches = []

    def _early_return_guards(self, loop_node):
        guards = set()
        for entry in query(loop_node, "if_expression"):
            parts = children(entry)
            if len(parts) < 2:
                continue
            body = body_of(parts[1]) or parts[1]
            steps = children(body) if body.kind() in {"statements", "control_structure_body", "block"} else [body]
            if any(s.kind() == "jump_expression" and s.children()[0].kind() == "return" for s in steps):
                for ident in query(parts[0], "simple_identifier"):
                    guards.add(ident.text())
        return guards

    def _tail_asserts_subject(self, file, tail, subjects):
        if not subjects:
            return False
        for stmt in tail:
            for call_node in query(stmt, "call_expression"):
                c = call_of(call_node)
                if c is not None and check_call(file, call_node, c) is not None:
                    if any(ident.text() in subjects for ident in query(call_node, "simple_identifier")):
                        return True
        return False

    def sequence(self, nodes):
        exits = NORMAL
        platform_checked = set()
        node_list = list(nodes)
        for index, node in enumerate(node_list):
            if "next" not in exits:
                break
            os_kind = ""
            if node is not None and node.kind() == "if_statement" and node.field("alternative") is None:
                os_kind = _platform_kind(self.file, node.field("condition"))
            self.last_branch_positive = None
            step = self.visit(node)
            is_loop = node is not None and (
                node.kind() in {"for_statement", "enhanced_for_statement"}
                or self.file.syntax.language == "kotlin" and (c := call_of(node)) is not None and c.method == "repeat")
            if (is_loop and "return" in step and not step["return"].checked
                    and "next" in step and not step["next"].checked):
                tail = [n for n in node_list[index + 1:] if n is not None and n.kind() not in {"comment", "line_comment", "block_comment"}]
                if tail and (any(failure_statement(n) for n in tail)
                             or self.file.syntax.language == "kotlin"
                             and self._tail_asserts_subject(self.file, tail, self._early_return_guards(node))):
                    step["return"] = replace(step["return"], checked=True)
            if os_kind and step.get("next", Evidence()).checked is False:
                pos_ev = self.last_branch_positive
                if pos_ev is not None and pos_ev.get("next", Evidence()).checked:
                    platform_checked.add(os_kind)
                    if {"windows", "unix"} <= platform_checked and "next" in step:
                        step["next"] = replace(step["next"], checked=True)
            if node is not None and node.kind() == "property_declaration":
                decl = child(node, "variable_declaration")
                init = self.file.initializer(decl) if decl is not None else None
                if decl is not None and init is not None and node_key(init) in self.unjoined_jobs:
                    self.unjoined_jobs[self.file.path, node_key(decl)] = self.unjoined_jobs[node_key(init)]
            exits = followed_by(exits, step)
        return exits

    def _filter_check_observations(self, check):
        if not check.observations or self.file.syntax.language not in {"java", "kotlin"}:
            return check
        drop = set()
        rebound = {}
        owner_fn = next((parent for parent in check.node.ancestors() if parent.kind() in FUNCTIONS | LAMBDAS), None)
        owner_body = body_of(owner_fn) if owner_fn is not None else None
        for key, present in check.observations:
            if present or key[0] != "size":
                continue
            related = check.controls.get(key, frozenset({key}))
            local_recs = {rec.identity for ch, rec in related if ch == "size" and rec.shared is None}
            if owner_body is not None and local_recs and _seeded_then_drained(self.file, owner_body, check.node, local_recs):
                drop.add(key)
                continue
            if self.bound:
                dropped = False
                for ch, rec in related:
                    if (ch == "size" and rec.identity[0] == self.file.path
                            and rec.identity[1][0] in {"formal_parameter", "parameter"}
                            and rec.identity[:2] in self.bound and self.bound[rec.identity[:2]][0] is not None):
                        bfile, barg, _ = self.bound[rec.identity[:2]]
                        arg_rec = None if isinstance(barg, tuple) else bfile.collection_recorder(barg)
                        if arg_rec is None:
                            drop.add(key)
                            dropped = True
                            break
                        if arg_rec.shared is not None:
                            rebound[key] = (ch, arg_rec)
                if dropped:
                    continue
        if not drop and not rebound:
            return check
        observations = tuple(
            (rebound[k], p) if k in rebound else (k, p)
            for k, p in check.observations if k not in drop)
        controls = {}
        for k, v in check.controls.items():
            if k in drop:
                continue
            if k in rebound:
                new_k = rebound[k]
                controls[new_k] = (v - {k}) | {new_k}
            else:
                controls[k] = v
        return replace(check, observations=observations, controls=controls)

    def record(self, check):
        check = self._filter_check_observations(check)
        site = self.invocation if self.invocation is not None else check.node
        self.checks[node_key(site), (*node_key(check.node), str(self.file.path))] = replace(check, node=site)
        return {"next": Evidence(True, frozenset(key for key, present in check.observations if present), frozenset(check.sizes))}

    def visit(self, node):
        node = unwrap(node)
        if node is None:
            return NORMAL
        kind = node.kind()
        if kind == "class_body" and node.parent() is not None and node.parent().kind() in {
                "object_literal", "object_creation_expression"}:
            call_kind = "call_expression" if self.file.syntax.language == "kotlin" else "method_invocation"
            throw_kind = "jump_expression" if self.file.syntax.language == "kotlin" else "throw_statement"
            if any(failure_statement(entry) for entry in query(node, throw_kind, call_kind)):
                self.opaque_check = True
                self.throwing_fake_check = True
            elif self.has_callback_check(node, call_kind):
                self.opaque_check = True
            return NORMAL
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
        callbacks = self.bound_callbacks(call)
        bound = self.bound_argument(call)
        direct_check = None
        local = None
        if bound is not None and bound[1].kind() in LAMBDAS:
            executed = self.invoke_callback(*bound)
        elif bound is not None and bound[1].kind() in {"callable_reference", "method_reference"}:
            executed = self.invoke_reference(node, call, *bound)
        else:
            direct_check = check_call(self.file, node, call)
            if not (direct_check is not None and direct_check.owner == "turbine"):
                candidate = self.file.helper(node, call)
                if candidate is not None and callbacks and method in SYNCHRONOUS and candidate[0] is not self and not any(
                        p in {"test", "tests", "commonTest", "jvmTest", "androidTest", "nonJvmTest", "nonWasmTest",
                              "jsTest", "nativeTest", "test-utils", "testing"} or p.endswith(("Test", "Tests"))
                        for p in candidate[0].path.parts[:-1]):
                    candidate = None
                local = candidate
        if bound is not None and bound[1].kind() in LAMBDAS | {"callable_reference", "method_reference"}:
            pass
        elif local is not None:
            executed = self.helper(node, call, *local)
        else:
            check = direct_check
            executed = NORMAL
            if (check is None and self.invocation is not None and self.file.syntax.language == "kotlin"
                    and method == "require" and call.receiver is None and call.args):
                check = collect_observations(self.file, predicate(node, call.args[0], False))
            if check is not None:
                if check.soft_receiver is None:
                    executed = self.record(check)
                else:
                    self.buffered_checks[check.soft_receiver].append(check)
            mode = aggregate(self.file, call) if method in {"assertAll", "assertSoftly"} else ""
            if mode:
                run = executable_callbacks(self.file, call.args) if mode == "executables" else call.callbacks()
                executed = self.aggregate_callbacks(run, mode)
                if mode == "executables" and any(
                        (inner := call_of(arg)) is not None and inner.method == "map" and inner.receiver is not None
                        and not (type(sz := self.iterable_size(inner.receiver)) is int and sz > 0)
                        for arg in call.args):
                    executed = alternatives(NORMAL, executed)
            elif method == "assertAll" and not call.args and signature(self.file, call)[0] == "soft-assertj":
                executed = self.flush_soft_checks(node, call.receiver)
            elif callbacks and method not in {"assertAll", "assertSoftly"}:
                callback_flow = self.callbacks(node, call, method, check)
                executed = followed_by(executed, callback_flow)
            elif self.file.syntax.language == "java" and call.receiver is not None and self.receiver_throws_assertion_error(
                    call.receiver, call.method):
                self.opaque_check = True
            elif (self.file.syntax.language == "kotlin" and not call.args and call.method in {"join", "await"}
                  and call.receiver is not None):
                job_ev = self.unjoined_jobs.get(node_key(unwrap(call.receiver)))
                if job_ev is None and (rname := name(unwrap(call.receiver))):
                    _, rbind = self.file.visible_binding(call.receiver, rname)
                    if rbind is not None:
                        job_ev = self.unjoined_jobs.get((self.file.path, node_key(rbind)))
                if job_ev is not None:
                    executed = followed_by(executed, {"next": job_ev})
            if method == "fail" and check is not None:
                executed = followed_by(executed, {"assertion-failure": Evidence()})
            elif self.file.syntax.language == "kotlin" and call.receiver is None and method == "error" and check is None:
                executed = followed_by(executed, {"throw": Evidence()})
        executed = followed_by(arguments, executed)
        if has_safe_navigation(call.callee):
            executed = alternatives(NORMAL, executed)
        return followed_by(receiver, executed)

    def flush_soft_checks(self, node, receiver):
        known = NORMAL
        flush_path = branch_path(node)
        for check in self.buffered_checks[self.file.recorder(receiver)]:
            entry, block = statement(check.node)
            if block is None:
                continue
            for parent in node.ancestors():
                if parent.kind() in LAMBDAS | FUNCTIONS:
                    break
                if node_key(parent) == node_key(block):
                    if all(node_key(ancestor) != node_key(entry) for ancestor in node.ancestors()):
                        evidence = self.record(check)
                        checked_path = branch_path(check.node)
                        if checked_path != flush_path[:len(checked_path)]:
                            evidence = alternatives(NORMAL, evidence)
                        known = followed_by(known, evidence)
                    break
        return known

    def aggregate_callbacks(self, callbacks, mode):
        executed = NORMAL
        for callback in callbacks:
            exits = self.invoke_callback(self.file, callback, self.invocation)
            if mode == "executables":
                paths = {}
                for previous, before in executed.items():
                    for reason, after in exits.items():
                        outcome = reason if reason not in {"next", "throw", "assertion-failure"} else (
                            "next" if previous == reason == "next" else "assertion-failure")
                        paths = alternatives(paths, {outcome: before.then(after)})
                executed = paths
            elif "next" in executed:
                executed = followed_by(executed, exits)
        return executed

    def bound_callbacks(self, call):
        items = []
        for arg in call.args:
            unwrapped = unwrap(arg)
            if unwrapped is not None and unwrapped.kind() in LAMBDAS:
                items.append((self.file, unwrapped))
            elif unwrapped is not None and (bound := self.argument_binding(self.file, unwrapped)) is not None:
                if not isinstance(bound[1], tuple) and bound[1].kind() in LAMBDAS:
                    items.append((bound[0], bound[1]))
        return items

    def bound_argument(self, call):
        target = call.callee if call.receiver is None else (
            call.receiver if call.method in {"invoke", "run", "call", "accept", "apply", "get", "test"} else None)
        if target is not None:
            return self.argument_binding(self.file, target)
        if self.file.syntax.language == "kotlin" and call.callee is not None and call.receiver is not None and call.method:
            _, binding = self.file.visible_binding(call.callee, call.method)
            if binding is not None:
                bound = self.bound.get((self.file.path, node_key(binding)))
                if bound is not None and not isinstance(bound[1], tuple) and (
                        bound[1].kind() in LAMBDAS | {"callable_reference", "method_reference"}):
                    return bound
        return None

    def invoke_reference(self, node, call, ref_file, ref_node, _invocation):
        from .ast_grep_jvm import Call

        parts = children(ref_node)
        if not parts:
            return NORMAL
        recv_node, ref_method = (parts[0], name(parts[1])) if len(parts) == 2 else (None, name(parts[0]))
        if not ref_method:
            return NORMAL
        synthetic = Call(ref_method, list(call.args), recv_node, dict(call.named))
        check = check_call(ref_file, ref_node, synthetic)
        if check is not None:
            previous, caller = self.file, self.invocation
            self.file = ref_file
            try:
                return self.record(replace(check, node=node))
            finally:
                self.file, self.invocation = previous, caller
        if (local := ref_file.helper(ref_node, synthetic)) is not None:
            return self.helper(node, synthetic, *local)
        return NORMAL

    def argument_binding(self, file, node):
        _, binding = file.visible_binding(node, name(node))
        if binding is not None:
            return self.bound.get((file.path, node_key(binding)))
        return None

    def helper(self, node, call, file, declaration):
        key = file.path, node_key(declaration)
        if key in self.active:
            return NORMAL
        previous, caller, prev_returned = self.invocation, self.file, self.last_returned_job
        if previous is None:
            self.invocation = node
        self.last_returned_job = None
        slots = {}
        for parameter, argument, supplied in bind_arguments(declaration, call):
            origin = caller if supplied else file
            forwarded = None if isinstance(argument, tuple) else self.argument_binding(origin, argument)
            slots[file.path, node_key(parameter)] = forwarded or (origin, argument, previous)
        self.bound.update(slots)
        self.file = file
        self.active.add(key)
        try:
            if expects_failure(declaration):
                return self.record(Check(node))
            if (file is not caller and "fake" in str(file.path).lower()
                    and call.method.startswith("check")
                    and any("IllegalStateException" in t.text() or throws_assertion_error(t)
                            for t in query(body_of(declaration),
                                           "jump_expression" if file.syntax.language == "kotlin" else "throw_statement"))):
                return self.record(Check(node))
            exits = finish(self.visit(body_of(declaration)), "return")
            if self.last_returned_job is not None:
                self.unjoined_jobs[node_key(node)] = self.last_returned_job
            return exits
        finally:
            self.active.remove(key)
            for slot in slots:
                del self.bound[slot]
            self.file = caller
            self.invocation = previous
            self.last_returned_job = prev_returned

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
        callbacks = self.bound_callbacks(call)
        if method.startswith(("assertThrows", "assertFails", "shouldThrow")) or method in {
                "assertThatThrownBy", "assertThatCode", "thenThrownBy", "thenCode", "isThrownBy"}:
            return NORMAL
        if method.startswith(("verify", "coVerify")):
            return NORMAL
        if method == "waitUntil" and call.receiver is not None and len(callbacks) == 1:
            body = body_of(callbacks[0][1])
            condition = children(body)[-1] if body is not None and children(body) else None
            return self.record(collect_observations(self.file, predicate(node, condition, False)))
        synchronous = (method in SYNCHRONOUS or check is not None and (
            check.no_throw or method == "satisfies" or check.owner == "assertk" and method in {"all", "given"})
                       or method in {"consumeNextWith", "assertNext"} and self.file.syntax.language == "java"
                       or method == "compile" and self.file.syntax.language == "java"
                       and ("TestCompiler" in self.file.imports or "TestCompiler" in self.file.syntax.source))
        if not synchronous and method not in ITERATING | {"launch", "async", "allSatisfy", "anySatisfy",
                                                           "noneSatisfy"}:
            self.opaque_check |= node.parent().kind() == "function_body" or any(
                self.has_callback_check(callback, node.kind()) for _, callback in callbacks)
            return NORMAL
        executed = NORMAL
        previous_checks = set(self.checks)
        structured = method in {"withContext", "coroutineScope", "supervisorScope"}
        if structured:
            self.structured_scope += 1
            self.structured_launches.append([])
        try:
            for cb_file, callback in callbacks:
                if "next" not in executed:
                    break
                callback_exits = self.invoke_callback(cb_file, callback, self.invocation)
                if method == "withTimeout" and timeout_poll(callback):
                    callback_exits = followed_by(self.record(Check(node)), callback_exits)
                executed = followed_by(executed, callback_exits)
        finally:
            launched_inside = self.structured_launches.pop() if structured else ()
            if structured:
                self.structured_scope -= 1
        if structured and launched_inside:
            joined_ev = reduce(Evidence.then, launched_inside)
            if "next" in executed:
                executed = {**executed, "next": executed["next"].then(joined_ev)}
        if method == "assumingThat":
            known = self.known_truth(call.args[0])
            return executed if known is True else alternatives(NORMAL, executed)
        if synchronous:
            return executed
        if method in ITERATING:
            if method == "map" and node.parent() is not None and node.parent().kind() == "return_statement" and any(
                    (inner := call_of(entry)) is not None and inner.method in {"dynamicTest", "dynamicContainer"}
                    for _, callback in callbacks for entry in query(callback, node.kind())):
                return executed
            if method == "map" and any(
                    (inner := call_of(entry)) is not None and inner.method == "dynamicTest"
                    for _, callback in callbacks for entry in query(callback, node.kind())) and any(
                    parent.kind() == "return_statement" for parent in node.ancestors()):
                return executed
            iterable = (self.minimum_positive_int(call.args[0]) if method == "repeat" and call.args
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
        if method in {"launch", "async"} and "next" in executed and executed["next"].checked:
            self.unjoined_jobs[node_key(node)] = executed["next"]
            if self.structured_scope > 0 and node.parent() is not None and node.parent().kind() in {
                    "statements", "block", "function_body"}:
                self.structured_launches[-1].append(executed["next"])
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

    def has_callback_check(self, callback, call_kind, seen=frozenset()):
        for entry in query(callback, call_kind):
            inner = call_of(entry)
            if inner is None:
                continue
            if check_call(self.file, entry, inner) is not None:
                return True
            if (bound := self.bound_argument(inner)) is not None and not isinstance(bound[1], tuple) and (
                    bound[1].kind() in LAMBDAS and node_key(bound[1]) not in seen):
                if self.has_callback_check(bound[1], call_kind, seen | {node_key(bound[1])}):
                    return True
        if self.file.syntax.language == "java":
            return bool(query(callback, "assert_statement"))
        return any(infix_check(self.file, entry) is not None for entry in query(callback, "infix_expression"))

    def receiver_throws_assertion_error(self, receiver, method):
        if not name(receiver):
            return False
        _, binding = self.file.visible_binding(receiver, name(receiver))
        if binding is None:
            return False
        declared_node = binding.field("type") or (binding.parent().field("type") if binding.parent() is not None else None)
        iface = declared_node.text().split("<", 1)[0] if declared_node is not None else ""
        if not iface or iface not in self.file.types:
            return False
        for cls_node in self.file.types.values():
            header = cls_node.text().split("{", 1)[0]
            if f"implements {iface}" in header or f", {iface}" in header or f"extends {iface}" in header:
                for scope, fn in self.file.functions.get(method, ()):
                    if node_key(scope) == node_key(cls_node) and any(
                            throws_assertion_error(t) for t in query(body_of(fn), "throw_statement")):
                        return True
        return False

    def remember_loop(self, previous_checks, source):
        if source is not None:
            for key in self.checks:
                if key in previous_checks:
                    continue
                self.loop_checks.setdefault(key, source)

    def minimum_positive_int(self, node, file=None, bindings=None, active=frozenset()):
        file = file or self.file
        node = unwrap(node)
        if node is not None and node.kind() == "control_structure_body" and len(children(node)) == 1:
            node = unwrap(children(node)[0])
        if node is None:
            return None
        key = ("int", file.path, node_key(node))
        if key in active:
            return None
        active = active | {key}
        known = self.known_literal(node, file, bindings, active)
        if type(known) is int:
            return known
        if (identifier := name(node)):
            scope, binding = file.visible_binding(node, identifier)
            target_file = file
            if binding is None and file.syntax.language == "kotlin":
                from .ast_grep_jvm_signatures import family

                if (qualified := file.imports.get(identifier)) is not None:
                    pkgs = [qualified.rpartition(".")[0]]
                else:
                    pkgs = [file.package, *(pkg for pkg in sorted(file.wildcard_imports) if family(pkg) == "foreign")]
                for pkg in pkgs:
                    for peer in file.package_files(pkg):
                        for sc, entry in peer.bindings.get(identifier, ()):
                            if node_key(sc) == node_key(peer.syntax.root) and peer.initializer(entry, immutable=True) is not None:
                                target_file, binding = peer, entry
                                break
                        if binding is not None:
                            break
                    if binding is not None:
                        break
                if binding is None:
                    repo_root = project_root(file.path, None, {".git"})
                    module_parent = file.module.parent if file.module is not None else None
                    bases = [b for b in (file.module, repo_root) if b is not None]
                    for pkg in pkgs:
                        pkg_parts = tuple(pkg.split(".")) if pkg else ()
                        for base in bases:
                            for directory in _repo_package_directories(base, module_parent, pkg_parts):
                                if not directory.is_dir():
                                    continue
                                for path in sorted(directory.iterdir()):
                                    if path.suffix != ".kt" or path.is_symlink():
                                        continue
                                    peer = read_file(path, file.module)
                                    if peer is None or peer.package != pkg:
                                        continue
                                    for sc, entry in peer.bindings.get(identifier, ()):
                                        if node_key(sc) == node_key(peer.syntax.root) and peer.initializer(entry, immutable=True) is not None:
                                            target_file, binding = peer, entry
                                            break
                                    if binding is not None:
                                        break
                                if binding is not None:
                                    break
                            if binding is not None:
                                break
                        if binding is not None:
                            break
            if binding is not None:
                init = target_file.initializer(binding, immutable=True)
                if init is not None:
                    return self.minimum_positive_int(init, target_file, bindings, active)
        if node.kind() == "if_expression":
            elements = children(node)
            if len(elements) == 3:
                pos = self.minimum_positive_int(elements[1], file, bindings, active)
                neg = self.minimum_positive_int(elements[2], file, bindings, active)
                if type(pos) is int and type(neg) is int:
                    return min(pos, neg)
        if node.kind() in {"multiplicative_expression", "additive_expression"}:
            elements = [c for c in node.children() if c.kind() not in {"comment", "line_comment", "multiline_comment"}]
            if len(elements) == 3:
                op = elements[1].text()
                left = self.minimum_positive_int(elements[0], file, bindings, active)
                right = self.minimum_positive_int(elements[2], file, bindings, active)
                if type(left) is int and type(right) is int:
                    if op == "*" and left >= 0 and right >= 0:
                        return left * right
                    if op == "+" and left >= 0 and right >= 0:
                        return left + right
        return None

    def known_literal(self, node, file=None, bindings=None, active=frozenset()):
        file = file or self.file
        node = unwrap(node)
        if node is not None and (identifier := name(node)):
            scope, binding = file.visible_binding(node, identifier)
            if binding is not None:
                if (bound := self.bound_value(node, file, bindings)) is not None:
                    origin, argument = bound
                    if isinstance(argument, tuple):
                        return lint.UNKNOWN
                    return literal(origin.syntax, origin.immutable_value(unwrap(argument)) or argument)
                node = file.stable_initializer(node, scope, binding) or node
        receiver, measurement = member(node)
        called = call_of(node)
        if called is not None:
            if not called.args:
                receiver, measurement = called.receiver, called.method
            elif len(called.args) == 2 and name(called.receiver) in {"Integer", "Long"} and called.method in {
                    "getInteger", "getLong"}:
                return self.known_literal(called.args[1], file, bindings, active)
        if measurement in {"size", "length", "count"} and receiver is not None:
            if (size := self.iterable_size(receiver, file, bindings, active)) is not None:
                return size
        return literal(file.syntax, node)

    def iterable_size(self, node, file=None, bindings=None, active=frozenset()):
        file = file or self.file
        node = unwrap(node)
        if node is not None and node.kind() == "control_structure_body" and len(children(node)) == 1:
            node = unwrap(children(node)[0])
        if node is None:
            return None
        key = file.path, node_key(node)
        if key in active:
            return None
        active = active | {key}
        if (bound := self.bound_value(node, file, bindings)) is not None:
            defining_file, value = bound
            if isinstance(value, tuple):
                return len(value)
            return self.iterable_size(value, defining_file, bindings, active)
        if self.sizes and (source := sized_source(file, node)) in self.sizes:
            return self.sizes[source]
        value = file.immutable_value(node) or file.local_array(node) or file.setup_initializer(node, name(node))
        if value is None and file.syntax.language == "java" and (ident := name(node)):
            value = file.stable_initializer(node, *file.visible_binding(node, ident))
            if value is not None and value.kind() == "object_creation_expression":
                value = None
        if value is not None:
            origin = call_of(value)
            if origin is not None and origin.method.startswith("mutable"):
                return None
            return self.iterable_size(value, file, bindings, active)
        call = call_of(node)
        receiver, component = (call.receiver, call.method) if call is not None else member(node)
        if component in {"values", "entries"} and (identifier := name(receiver))[:1].isupper():
            target = file.type_named(identifier)
            if target is not None:
                if (body := child(target[1], "enum_class_body", "enum_body")) is not None:
                    return sum(entry.kind() in {"enum_entry", "enum_constant"} for entry in children(body))
                if (body := child(target[1], "class_body")) is not None and any(
                        m.kind() == "method_declaration" and name(m.field("name")) == "values"
                        for m in children(body)):
                    short = identifier.rsplit(".", 1)[-1]
                    count = sum(
                        1 for f in children(body)
                        if f.kind() == "field_declaration" and name(f.field("type")) == short
                    )
                    if count > 0:
                        return count
        if call is None and receiver is not None:
            if component in {"values", "keys", "entries"}:
                size = self.iterable_size(receiver, file, bindings, active)
                if size is not None:
                    return size
            if (property_value := file.object_property(receiver, component)) is not None:
                defining_file, value = property_value
                return self.iterable_size(value, defining_file, bindings, active)
        if call is not None and file.syntax.language == "java":
            if call.method in {"parallel", "sequential", "unordered", "boxed", "map",
                               "mapToInt", "mapToLong", "mapToDouble", "mapToObj", "sorted",
                               "keySet", "values", "entrySet", "stream"} and not call.args and call.receiver is not None:
                return self.iterable_size(call.receiver, file, bindings, active)
            if call.method in {"map", "mapToInt", "mapToLong", "mapToDouble", "mapToObj"} and call.receiver is not None:
                return self.iterable_size(call.receiver, file, bindings, active)
            if call.method == "filter" and call.receiver is not None and len(call.callbacks()) == 1:
                if (count := self._filtered_fields_size(file, call, bindings, active)) is not None:
                    return count
            if call.method in {"getAvailableIDs", "getAvailableLocales", "availableCharsets",
                               "getAvailableCurrencies"} and not call.args and name(call.receiver) in {
                    "TimeZone", "Locale", "Charset", "Currency"}:
                return 1
            if call.method in {"range", "rangeClosed"} and len(call.args) == 2 and name(call.receiver) in {
                    "", "IntStream", "LongStream"}:
                start = self.known_literal(call.args[0], file, bindings, active)
                end = self.known_literal(call.args[1], file, bindings, active)
                if type(start) is type(end) is int:
                    return max(0, end - start + int(call.method == "rangeClosed"))
            if call.method == "stream" and len(call.args) == 1 and name(call.receiver) == "Arrays":
                return self.iterable_size(call.args[0], file, bindings, active)
            if call.method == "of" and name(call.receiver) in {"Stream", "IntStream", "LongStream", "DoubleStream"} and call.args:
                if len(call.args) == 1:
                    inner = self.iterable_size(call.args[0], file, bindings, active)
                    return inner if inner is not None else 1
                return len(call.args)
            if call.method == "asList" and name(call.receiver) == "Arrays" or (
                    call.method == "of" and name(call.receiver) in {"List", "Set", "ImmutableList", "ImmutableSet"}):
                if len(call.args) == 1:
                    inner = self.iterable_size(call.args[0], file, bindings, active)
                    if inner is not None:
                        return inner
                return len(call.args)
            if call.method == "of" and name(call.receiver) in {"Map", "ImmutableMap"}:
                return len(call.args) // 2
            if call.method == "ofEntries" and name(call.receiver) == "Map":
                return len(call.args)
            if call.method == "singletonMap" and name(call.receiver) == "Collections" and len(call.args) == 2:
                return 1
            if call.method == "getParameters" and not call.args and call.receiver is not None:
                recv = unwrap(call.receiver)
                recv_init = (file.immutable_value(recv)
                             or (file.stable_initializer(recv, *file.visible_binding(recv, name(recv)))
                                 if name(recv) else None)
                             or recv)
                if (ctor := call_of(recv_init)) is not None and ctor.method in {
                        "getConstructor", "getDeclaredConstructor"}:
                    return len(ctor.args)
            if call.method in {"getDeclaredMethods", "getDeclaredFields"} and not call.args and call.receiver is not None:
                target_file, recv = file, unwrap(call.receiver)
                if recv.kind() != "class_literal" and (bound := self.bound_value(recv, file, bindings)) is not None:
                    target_file, recv = bound[0], unwrap(bound[1])
                if recv is not None and recv.kind() == "class_literal":
                    cls_node = recv.field("type") or next(iter(children(recv)), None)
                    if (target := target_file.type_named(name(cls_node))) is not None:
                        decl_kind = "method_declaration" if call.method == "getDeclaredMethods" else "field_declaration"
                        body_node = child(target[1], "class_body", "interface_body")
                        count = sum(1 for entry in children(body_node) if entry.kind() == decl_kind) if body_node is not None else 0
                        if count > 0:
                            return count
            if (local := file.helper(node, call)) is not None:
                defining_file, declaration = local
                hkey = defining_file.path, node_key(declaration)
                if hkey not in active:
                    steps = children(body_of(declaration))
                    if steps and steps[-1].kind() == "return_statement":
                        returned = next(iter(children(steps[-1])), None)
                        slots = dict(bindings or {})
                        slots.update({(defining_file.path, node_key(parameter)): (file if supplied else defining_file, val)
                                      for parameter, val, supplied in bind_arguments(declaration, call)})
                        if len(steps) == 1:
                            return self.iterable_size(returned, defining_file, slots, active | {hkey})
                        if name(returned) and _local_added_count(defining_file, returned, name(returned), self) > 0:
                            return 1
        if file.syntax.language == "java" and call is None and receiver is None and (ident := name(node)):
            if (added := _local_added_count(file, node, ident, self)) > 0:
                return added
            if fixtures := java_list_fixture(file, node):
                return len(fixtures)
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

    def _filtered_fields_size(self, file, call, bindings, active):
        cb = call.callbacks()[0]
        prefix = None
        for entry in query(cb, "method_invocation"):
            c = call_of(entry)
            if c is not None and c.method == "startsWith" and len(c.args) == 1:
                val = literal(file.syntax, c.args[0])
                if isinstance(val, str) and val:
                    prefix = val
                    break
        if prefix is None:
            return None
        cur = call.receiver
        while (inner := call_of(cur)) is not None and inner.method == "filter":
            cur = inner.receiver
        stream_call = call_of(cur)
        if stream_call is None or stream_call.method != "stream" or name(stream_call.receiver) != "Arrays" or len(stream_call.args) != 1:
            return None
        fields_call = call_of(stream_call.args[0])
        if fields_call is None or fields_call.method not in {"getFields", "getDeclaredFields"} or fields_call.args or fields_call.receiver is None:
            return None
        recv = unwrap(fields_call.receiver)
        if recv.kind() != "class_literal":
            return None
        cls_node = recv.field("type") or next(iter(children(recv)), None)
        target = file.type_named(name(cls_node))
        if target is None:
            return None
        body_node = child(target[1], "class_body", "interface_body")
        if body_node is None:
            return None
        count = 0
        for entry in children(body_node):
            if entry.kind() == "field_declaration":
                decl = child(entry, "variable_declarator")
                if decl is not None and name(decl.field("name")).startswith(prefix):
                    count += 1
        return count if count > 0 else None

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

    def known_truth(self, condition):
        if self.fixture_item is not None:
            return fixture_truth(self.file, condition, *self.fixture_item)
        known = truth(self.file.syntax, condition)
        if known is not lint.UNKNOWN:
            return known
        node = unwrap(condition)
        operator, left, right = split(node)
        if operator:
            lval, rval = self.known_literal(left), self.known_literal(right)
            if lval is not lint.UNKNOWN and rval is not lint.UNKNOWN:
                return compare(operator, lval, rval)
            return lint.UNKNOWN
        val = self.known_literal(node)
        return val if type(val) is bool else lint.UNKNOWN

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
            self.last_branch_positive = None
            return evaluated
        known = self.known_truth(condition)
        branches = []
        pos_result = None
        if known is not False:
            pos_result = self.visit(positive)
            branches.append(pos_result)
        self.last_branch_positive = pos_result if node.kind() == "if_statement" and negative is None else None
        if known is not True:
            branches.append(self.visit(negative))
        combined = followed_by(evaluated, alternatives(*branches))
        allow_err = self.file.syntax.language == "kotlin"
        fail_branch = (
            negative is None and known is lint.UNKNOWN and pos_result is not None
            and (_guarded_equals_check(condition, positive, pos_result)
                 or "next" not in pos_result and ("assertion-failure" in pos_result or allow_err and "throw" in pos_result) and (
                     failure_statement(positive, allow_error=allow_err)
                     or len(children(body_of(positive) or positive)) == 1
                     and failure_statement(children(body_of(positive) or positive)[0], allow_error=allow_err))))
        if fail_branch:
            if not any(node_key(c.node) == node_key(node) for c in self.checks.values()):
                self.record(Check(node))
            if "next" in combined:
                combined["next"] = replace(combined["next"], checked=True)
            else:
                combined["next"] = Evidence(True)
        return combined

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
            filtered = self.enum_filter_steps(node, iterable, body)
            if filtered is not None and size is not None and size > 1:
                before = self.visit(iterable)
                if "next" not in before:
                    return before
                executed = finish(self.sequence(filtered), *loop_jumps(node))
                return followed_by(before, executed)
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
        if reason == "return" and (returned := next(iter(children(node)), None)) is not None:
            if (job_ev := self.unjoined_jobs.get(node_key(unwrap(returned)))) is not None:
                self.last_returned_job = job_ev
        if "next" in evaluated:
            if reason == "throw" and throws_assertion_error(node):
                evaluated = followed_by(evaluated, self.record(Check(node)))
                reason = "assertion-failure"
            evaluated = followed_by(evaluated, {reason: Evidence()})
        return evaluated

    def try_block(self, node):
        entries = children(node)
        resources = node.field("resources") or child(node, "resource_specification")
        prefix = self.visit(resources) if resources is not None else NORMAL
        if "next" not in prefix:
            return prefix
        body = node.field("body") or child(node, "statements", "block")
        tried = self.visit(body)
        if node.kind() == "try_with_resources_statement" and resources is not None:
            flushed = self.flush_try_resources(node, body, resources)
            if "next" in flushed:
                tried = {reason: evidence.then(flushed["next"]) for reason, evidence in tried.items()}
        tried = followed_by(prefix, tried)
        catches = [entry for entry in entries if entry.kind() in {"catch_block", "catch_clause"}]
        if catches:
            expected = self.expected_failure(body, catches)
            catch_branches = []
            for catch in catches:
                cbody = body_of(catch)
                cexits = self.visit(cbody)
                if "next" in cexits and self.file.syntax.language == "java" and self._catch_rethrows(cbody):
                    cexits = {**{k: v for k, v in cexits.items() if k != "next"}, "throw": Evidence()}
                catch_branches.append(followed_by(expected, cexits))
            tried = alternatives(tried, *catch_branches)
        cleanup = child(node, "finally_block", "finally_clause")
        if cleanup is None:
            return tried
        completed = self.sequence(children(cleanup))
        return alternatives(*(
            {reason if exit_kind == "next" else exit_kind: evidence.then(final)} for reason, evidence in tried.items() for exit_kind, final in completed.items()
        ))

    def flush_try_resources(self, try_node, body, resources):
        known = NORMAL
        if resources is None or body is None:
            return known
        start, end = body.range().start.index, body.range().end.index
        flush_path = branch_path(try_node)
        for resource in children(resources):
            target = resource.field("name") or next(
                (entry for entry in children(resource) if entry.kind() in {"identifier", "field_access"}), None)
            rec = self.file.recorder(target)
            if rec is None:
                continue
            remaining = []
            for check in self.buffered_checks.get(rec, ()):
                pos = offset(check.node)
                if start <= pos <= end:
                    evidence = self.record(check)
                    checked_path = branch_path(check.node)
                    if checked_path != flush_path[:len(checked_path)]:
                        evidence = alternatives(NORMAL, evidence)
                    known = followed_by(known, evidence)
                else:
                    remaining.append(check)
            self.buffered_checks[rec] = remaining
        return known

    def enum_filter_steps(self, loop, iterable, body):
        steps = loop_steps(loop)
        if not steps or steps[0].kind() != "if_expression":
            return None
        parts = children(steps[0])
        if len(parts) != 2 or len(children(body_of(parts[1]) or parts[1])) != 1:
            stmt_list = children(parts[1]) if len(parts) == 2 else ()
            if not stmt_list or stmt_list[0].kind() != "jump_expression" or stmt_list[0].children()[0].kind() != "continue":
                return None
        jump_body = body_of(parts[1]) if len(parts) == 2 else None
        jump_nodes = children(jump_body) if jump_body is not None else children(parts[1]) if len(parts) == 2 else ()
        if not (len(jump_nodes) == 1 and jump_nodes[0].kind() == "jump_expression" and jump_nodes[0].children()[0].kind() == "continue"):
            return None
        var_node = child(loop, "variable_declaration")
        var_name = name(child(var_node, "simple_identifier")) if var_node is not None else ""
        cond = unwrap(parts[0])
        negated = negate(cond)
        inner = negated or cond
        recv, prop = member(inner)
        if not var_name or not prop or name(recv) != var_name:
            return None
        it_call = call_of(unwrap(iterable))
        enum_recv, comp = (it_call.receiver, it_call.method) if it_call is not None else member(unwrap(iterable))
        if comp not in {"values", "entries"} or not (enum_name := name(enum_recv))[:1].isupper():
            return None
        target = self.file.type_named(enum_name)
        if target is None or (enum_body := child(target[1], "enum_class_body")) is None:
            return None
        entries = [e for e in children(enum_body) if e.kind() == "enum_entry"]
        if len(entries) < 2:
            return None
        # Check whether at least one entry lacks the skipped boolean argument or passes the opposite.
        skip_true = negated is None
        for entry in entries:
            txt = entry.text()
            if skip_true and f"{prop} = true" not in txt:
                return steps[1:]
            if not skip_true and f"{prop} = true" in txt:
                return steps[1:]
        return None

    def expected_failure(self, body, catches):
        steps = children(body) if body is not None else []
        if len(steps) < 2 or not failure_statement(steps[-1]):
            return NORMAL
        fail_msg = ""
        fail_call = call_of(steps[-1])
        if fail_call is not None and fail_call.args:
            val = literal(self.file.syntax, fail_call.args[0])
            if isinstance(val, str):
                fail_msg = val
        for catch in catches:
            caught = child(catch, "user_type") or child(child(catch, "catch_formal_parameter"), "catch_type")
            if caught is None:
                return NORMAL
            names_caught = [entry.text().rsplit(".", 1)[-1] for entry in children(caught)]
            if any(n in {"Error", "Throwable"} for n in names_caught):
                return NORMAL
            if "AssertionError" in names_caught:
                catch_body = body_of(catch)
                call_kind = "call_expression" if self.file.syntax.language == "kotlin" else "method_invocation"
                if catch_body is None or not any(
                        (c := call_of(cn)) is not None and c.method in {"contains", "assertContains", "assertEquals"}
                        for cn in query(catch_body, call_kind)):
                    return NORMAL
                if fail_msg and any(
                        (needle := literal(self.file.syntax, n)) and isinstance(needle, str) and needle in fail_msg
                        for n in query(catch_body, "string_literal")):
                    return NORMAL
        return {"next": Evidence(checked=True)}

    def _catch_rethrows(self, catch_body):
        steps = children(catch_body) if catch_body is not None else []
        if not steps:
            return False
        last = unwrap(steps[-1])
        if last is not None and last.kind() == "expression_statement":
            last = unwrap(next(iter(children(last)), None))
        call = call_of(last)
        return call is not None and call.method.startswith(("throw", "rethrow", "sneakyThrow"))
