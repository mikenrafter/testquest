"""Track exits from a Dart test that can occur before a completed check."""

from . import lint
from .ast_grep import line, offset, query
from .ast_grep_dart import CHANGING_COLLECTION, Check, collection_size, constant, expression_operator
from .ast_grep_dart_syntax import (
    CALLBACKS, COLLECTIONS, between, call_arguments, collection_entries, contents, final_call, identifier, kind,
    member, named, names, signature, split, tokens, unwrap,
)


NEXT, RETURN, BREAK, CONTINUE, THROW = 1, 2, 4, 8, 16
EXITS = {"return_statement": RETURN, "break_statement": BREAK, "continue_statement": CONTINUE,
         "throw_statement": THROW, "rethrow_statement": THROW}
ITERATORS = {"forEach", "map", "where", "any", "every", "expand"}
FUTURE_CONSTRUCTORS = {("Future",), ("Future", "sync"), ("Future", "microtask"),
                       ("Future", "delayed"), ("Future", "forEach"), ("Future", "doWhile")}
FUTURE_CHAINS = {"then", "catchError", "whenComplete", "timeout"}
TEST_BOUNDARIES = {"test", "testWidgets", "group", "setUp", "setUpAll", "tearDown", "tearDownAll"}
EXPRESSIONS = {"expression_statement", "argument", "named_argument", "parenthesized_expression",
               "initialized_identifier", "initialized_variable_definition", "assertion_arguments"}


def followed_by(first, second):
    return (first & ~NEXT) | (second if first & NEXT else 0)


def as_tuple(node):
    return (node,) if node is not None else ()


def is_async(body):
    return any(child.kind() == "async" for child in tokens(body))


def variable_definition(node):
    return next((child for child in contents(node) if child.kind() == "initialized_variable_definition"), None)


def await_target(node):
    if node.kind() == "expression_statement":
        nodes = contents(node)
        if kind(nodes) == "unary_expression":
            nodes = contents(nodes[0])
        if kind(nodes) == "await_expression":
            return names(contents(nodes[0]))
    return ()


def declared_name(node):
    head = []
    for child in tokens(node):
        if child.kind() == "=":
            break
        head.append(child)
    return next((identifier(child) for child in reversed(head) if child.kind() == "identifier"), "")


def generic_future_callback(node):
    """Recover a standalone Future<T>(closure) parsed as two comparisons."""
    if node.kind() != "relational_expression":
        return None
    operator, left, right = split(tokens(node), {">"})
    if operator != ">" or kind(left) != "relational_expression" or kind(right) != "parenthesized_expression":
        return None
    angle, target, type_argument = split(tokens(left[0]), {"<"})
    if angle != "<" or names(target) != ("Future",) or len(type_argument) != 1:
        return None
    callback = unwrap(right)
    if kind(callback) == "function_expression":
        return next((child for child in contents(callback[0]) if child.kind() == "function_expression_body"), None)
    return None


def fixed_duration(nodes):
    nodes = unwrap(nodes)
    if kind(nodes) == "const_object_expression":
        parts = contents(nodes[0])
        if len(parts) != 2 or identifier(parts[0]) != "Duration":
            return False
        _, units = call_arguments(parts[1])
    elif (call := final_call(nodes)) is not None and call.name == ("Duration",):
        units = call.keywords
    else:
        return False
    values = [constant(amount) for amount in units.values()]
    return bool(values) and all(type(value) is int and value >= 0 for value in values) and any(values)


class Flow:
    def __init__(self, file, callback):
        self.file = file
        self.callback = callback
        self.checks = []
        self.writes = []
        self.completions = {}
        self.escaped_completers = set()
        self.active = set()
        self.supplied = {}
        self.reply_joined = True
        self.sink = None
        self.event = None

    def run(self):
        return self.body(self.callback, False, True)

    def body(self, callback, conditional, completed):
        if callback is None or callback.key in self.active:
            return NEXT
        body = callback.body
        if any(child.kind() in {"async*", "sync*"} for child in tokens(body)):
            return NEXT
        self.active.add(callback.key)
        outer = self.file, self.supplied, self.reply_joined
        self.file, self.supplied, self.reply_joined = callback.file, callback.supplied, completed
        try:
            block = next((child for child in contents(body) if child.kind() == "block"), None)
            return (self.visit(block, conditional) if block is not None else
                    self.expression(contents(body), conditional, completed))
        finally:
            self.file, self.supplied, self.reply_joined = outer
            self.active.remove(callback.key)

    def sequence(self, nodes, conditional, completed=False):
        remaining = NEXT
        for index, node in enumerate(nodes):
            joined = completed
            if node.kind() == "local_variable_declaration":
                declaration = variable_definition(node)
                if declaration is not None:
                    target = declared_name(declaration)
                    joined = bool(target and index + 1 < len(nodes)
                                  and await_target(nodes[index + 1]) == (target,))
            outcome = self.visit(node, conditional or remaining not in {0, NEXT}, completed=joined)
            remaining = followed_by(remaining, outcome)
        return remaining

    def record(self, check, conditional, detached=False):
        check.guarded = conditional or detached
        check.file = self.file
        self.checks.append(check)
        return NEXT if detached else 0

    def visit(self, node, conditional, *, completed=False):
        if node is None or node.kind() in CALLBACKS:
            return NEXT
        node_kind = node.kind()
        if node_kind == "block":
            return self.sequence(contents(node), conditional)
        if node_kind in {"if_statement", "if_element"}:
            condition = as_tuple(node.field("condition")) or named(between(tokens(node), "(", "case"))
            if not any(child.kind() == "case" for child in tokens(node)):
                condition = as_tuple(node.field("condition")) or named(between(tokens(node), "(", ")"))
            value = constant(condition)
            leading = self.expression(condition, conditional)
            first = self.visit(node.field("consequence"), conditional or value is not True,
                               completed=completed)
            second = self.visit(node.field("alternative"), conditional or value is not False,
                                completed=completed)
            return followed_by(leading, first if value is True else second if value is False else first | second)
        if node_kind in {"for_statement", "while_statement", "do_statement"}:
            return self.loop(node, conditional)
        if node_kind == "try_statement":
            body = node.field("body")
            handlers = [child for child in contents(node) if child.kind() == "block"
                        and (body is None or offset(child) != offset(body))]
            flow = self.visit(body, conditional or bool(handlers))
            for handler in handlers:
                flow |= self.visit(handler, True)
            final = next((child for child in contents(node) if child.kind() == "finally_clause"), None)
            after = self.visit(final, conditional)
            return (flow if after & NEXT else 0) | (after & ~NEXT if flow else 0)
        if node_kind == "switch_statement":
            return self.switch(node, conditional)
        if node_kind in EXITS:
            result = self.expression(contents(node), conditional, completed=node_kind == "return_statement" and self.reply_joined)
            return EXITS[node_kind] if result else 0
        if node_kind == "assertion":
            args = next((child for child in contents(node) if child.kind() == "assertion_arguments"), None)
            condition = named(between(tokens(args), "(", ",")) if args is not None else ()
            operator, left, right = expression_operator(condition)
            return self.record(Check(node, left, right, operator, condition, True), conditional)
        if node_kind == "await_expression":
            target = contents(node)
            outcome = self.expression(target, conditional, True)
            path = names(target)
            if path[-1:] == ("future",) and (key := self.completer(target[:-1])) is not None:
                writers = self.completions.get(key, {})
                if len(writers) == 1 and all(writers.values()) and key not in self.escaped_completers:
                    return followed_by(outcome, self.record(Check(node), conditional))
            return outcome
        if node_kind == "assignment_expression":
            assignment = next((child.kind() for child in tokens(node) if child.kind() in {"=", "??="}), None)
            if assignment:
                target = next(iter(contents(node)), None)
                if (key := self.completer(contents(target))) is not None:
                    self.escaped_completers.add(key)
                return self.expression(named(between(tokens(node), assignment)), conditional or assignment == "??=")
        if node_kind in EXPRESSIONS:
            nodes = contents(node)
            if node_kind.startswith("initialized_"):
                nodes = named(between(tokens(node), "=", ","))
            elif node_kind == "named_argument":
                nodes = nodes[1:]
            return self.expression(nodes, conditional, completed)
        if node_kind == "conditional_expression":
            items = tokens(node)
            condition = named(items[:next(i for i, child in enumerate(items) if child.kind() == "?")])
            first, second = named(between(items, "?", ":")), named(between(items, ":"))
            truth = constant(condition)
            leading = self.expression(condition, conditional)
            left = self.expression(first, conditional or truth is not True, completed)
            right = self.expression(second, conditional or truth is not False, completed)
            return followed_by(leading, left if truth is True else right if truth is False else left | right)
        if callback := generic_future_callback(node):
            target = self.file.callback((callback.parent(),), self.supplied)
            result = self.body(target, conditional or not completed, completed)
            return NEXT if not completed or result else 0
        operator, left, right = expression_operator((node,))
        if operator in {"&&", "||", "??"}:
            value = constant(left)
            runs = (value is True if operator == "&&" else value is False if operator == "||" else value is None)
            first = self.expression(left, conditional)
            second = self.expression(right, conditional or not runs, completed)
            return followed_by(first, second if runs else second | NEXT)
        if node_kind in COLLECTIONS:
            outcome = NEXT
            for item in collection_entries(node):
                outcome = followed_by(outcome, self.expression(item, conditional))
            return outcome
        if node_kind in {"local_variable_declaration", "unary_expression", "finally_clause", "assert_statement"}:
            return self.sequence(contents(node), conditional, completed)
        return self.expression(contents(node), conditional)

    def expression(self, nodes, conditional, completed=False):
        nodes = unwrap(nodes)
        if not nodes or kind(nodes) in CALLBACKS:
            return NEXT
        if completed and len(nodes) == 2 and kind(nodes[:1]) == "list_literal" and member(nodes[1]) == "wait":
            return self.join_list(nodes[0], conditional)
        call = final_call(nodes)
        if call is not None:
            return self.call(call, conditional, completed)
        outcome = NEXT
        for node in nodes:
            visited = self.visit(node, conditional or bool(outcome & ~NEXT), completed=completed)
            outcome = followed_by(outcome, visited)
        return outcome

    def join_list(self, node, conditional, completed=True):
        outcome = NEXT
        for item in collection_entries(node):
            outcome = followed_by(outcome, self.expression(item, conditional, completed))
        return outcome

    def call(self, call, conditional, completed):
        method = self.file.framework_name(call.name)
        if method in TEST_BOUNDARIES:
            return NEXT
        optional = call.optional
        guarded = conditional or optional
        chained = call.method in FUTURE_CHAINS and bool(call.receiver)
        out = self.expression(call.receiver, conditional, completed and chained)
        arguments = (*enumerate(call.args), *call.keywords.items())
        for _, argument in arguments:
            arg = unwrap(argument)
            if call.name == ("Future", "wait") and kind(arg) == "list_literal":
                visited = self.join_list(arg[0], guarded, completed)
            else:
                visited = self.expression(arg, guarded, completed and call.name == ("Future", "value"))
            out = followed_by(out, visited)
            if (key := self.completer(arg)) is not None:
                self.escaped_completers.add(key)
        if call.method in CHANGING_COLLECTION:
            self.write(call.receiver, call.node)
        if call.method in {"complete", "completeError"} and (key := self.completer(call.receiver)) is not None:
            self.complete(key, call.node, on_event=self.event is not None and call.method == "complete")
        if (check := self.file.check(call)) is not None:
            return followed_by(out, self.record(check, guarded, optional))
        if any(self.is_unreachable(arg) for _, arg in arguments):
            out = followed_by(out, self.record(Check(call.node), guarded, optional))

        receiver = final_call(call.receiver)
        fake_time = (self.file.library_name(call.name, "package:fake_async/") == "fakeAsync"
                     or call.name == ("fakeAsync",) and not self.file.binding((call.node,))
                     and not self.file.functions.get("fakeAsync")
                     or call.method == "run" and receiver is not None and (
                         self.file.library_name(receiver.name, "package:fake_async/") == "FakeAsync"
                         or receiver.name == ("FakeAsync",) and not self.file.binding((receiver.node,))
                         and not self.file.functions.get("FakeAsync")))
        zone_method = (call.method if len(call.name) == 1 else
                       self.file.library_name(call.name, "dart:async"))
        zone = (zone_method in {"runZoned", "runZonedGuarded"}
                or call.method == "runZoned"
                and self.file.library_name(call.name[:-1], "dart:io") == "IOOverrides")
        widget_async = call.method == "runAsync" and completed
        if fake_time or zone or widget_async:
            callback = self.file.callback(call.args[0], self.supplied) if call.args else None
            joined = completed or callback is not None and not is_async(callback.body)
            result = self.body(callback, guarded or not joined, completed)
            if not result and joined and not optional:
                out = followed_by(out, 0)
            if zone_method == "runZonedGuarded" and len(call.args) > 1:
                self.external_callback(call, 1, call.args[1])
            return out

        if call.name in FUTURE_CONSTRUCTORS or chained or call.method in ITERATORS:
            callback = next((candidate for _, arg in reversed(arguments)
                             if (candidate := self.file.callback(arg, self.supplied)) is not None), None)
            lazy = call.method in {"map", "where", "expand"}
            async_iterator = call.name == ("Future", "forEach")
            iterator = call.method in ITERATORS and not async_iterator
            bypass = (call.method in {"catchError", "timeout"} or optional or lazy
                      or iterator and not self.nonempty(call.receiver)
                      or async_iterator and not self.nonempty(call.args[0] if call.args else ()))
            awaited = completed if call.name in FUTURE_CONSTRUCTORS or chained else (
                callback is not None and not is_async(callback.body))
            self.record_callback_inputs(call, arguments)
            result = self.body(callback, guarded or bypass or not awaited, awaited)
            if (call.method == "timeout" and completed and not optional and "onTimeout" not in call.keywords
                    and call.args and fixed_duration(call.args[0])):
                return followed_by(out, self.record(Check(call.node), conditional))
            return followed_by(out, NEXT if bypass or not awaited or result else 0)
        if (callback := self.file.resolve_call(call, self.supplied)) is not None:
            joined = completed or not is_async(callback.body)
            result = self.body(callback, guarded or not joined, completed)
            return followed_by(out, NEXT if optional or not joined or result else 0)
        self.record_callback_inputs(call, arguments)
        for slot, arg in arguments:
            self.external_callback(call, slot, arg)
        if method and lint.helper(method) and (len(call.name) != 1 or not self.file.binding((call.node,))):
            return followed_by(out, self.record(Check(call.node), guarded, optional))
        return out

    def is_unreachable(self, nodes):
        call = final_call(nodes)
        path = call.name if call is not None else names(nodes)
        if self.file.framework_name(path) != "unreachable":
            return False
        if (callback := self.file.callback(nodes, self.supplied)) is None:
            return (not self.file.binding(unwrap(nodes))
                    and self.file.resolve_name(unwrap(nodes), self.supplied, path=path) is None)
        body = contents(callback.body)
        if kind(body) == "block":
            body = contents(body[0])
            if kind(body) == "expression_statement":
                body = contents(body[0])
        failure = final_call(body)
        return kind(body) in {"throw_statement", "throw_expression"} or (
            failure is not None and callback.file.framework_name(failure.name) == "fail")

    def completer(self, nodes):
        if len(names(nodes)) != 1 or (binding := self.file.binding(nodes)) is None:
            return None
        if len(names(binding.initializer)) == 1:
            return self.completer(binding.initializer) if binding.final else None
        initializer = final_call(binding.initializer)
        if initializer is not None and initializer.name in {("Completer",), ("Completer", "sync")}:
            return self.file, offset(binding.node)
        return None

    def complete(self, key, node, *, on_event):
        writers = self.completions.setdefault(key, {})
        site = self.file, offset(node)
        writers[site] = writers.get(site, True) and on_event

    def is_event_callback(self, call, slot):
        return call.method == "listen" or isinstance(slot, str) and slot.startswith("on") and slot[2:3].isupper()

    def external_callback(self, call, slot, nodes):
        if kind(unwrap(nodes)) != "function_expression" and call.method != "listen":
            return
        callback = self.file.callback(nodes, self.supplied)
        if callback is None:
            return
        outer = self.sink, self.event
        self.sink = self.sink_for(call, slot)
        self.event = (self.file, offset(call.node), slot) if self.is_event_callback(call, slot) else None
        if call.method == "listen" and slot != "onError" and (handler := call.keywords.get("onError")):
            error_callback = self.file.callback(handler, self.supplied)
            if error_callback is not None and kind(body := contents(error_callback.body)) == "block" and not contents(body[0]):
                self.sink = ()
        self.body(callback, True, False)
        self.sink, self.event = outer

    def sink_for(self, call, slot):
        if not call.receiver:
            return (call.name, slot) if isinstance(slot, str) else ()
        receiver = names(call.receiver)
        binding = self.file.binding(call.receiver)
        initializer = final_call(binding.initializer) if binding else None
        if receiver and initializer is not None and initializer.name:
            receiver = (*initializer.name, *receiver[1:])
        return (receiver, call.method, slot) if receiver else ()

    def record_callback_inputs(self, call, arguments):
        for slot, nodes in arguments:
            path = names(nodes)
            if path and path[-1] in CHANGING_COLLECTION:
                self.write(unwrap(nodes)[:-1], call.node, self.sink_for(call, slot))
            if path[-1:] == ("complete",) and (key := self.completer(unwrap(nodes)[:-1])) is not None:
                self.complete(key, nodes[0], on_event=self.is_event_callback(call, slot))

    def write(self, nodes, site, sink=None):
        if (key := self.file.recorder(nodes)) is not None:
            self.writes.append((self.file, key, site, self.sink if sink is None else sink))

    def nonempty(self, nodes):
        nodes = unwrap(nodes)
        if collection_size(nodes):
            return True
        path = names(nodes)
        if path[-1:] == ("values",) and self.file.is_enum(path[:-1], nodes[0]):
            return True
        if nodes and member(nodes[-1]) in {"entries", "values", "keys"}:
            nodes = unwrap(nodes[:-1])
            path = names(nodes)
            if kind(nodes) == "set_or_map_literal":
                return bool(collection_size(nodes))
        binding = self.file.binding(nodes)
        if binding and binding.final and len(path) == 1 and collection_size(binding.initializer):
            return binding.const or self.file.unchanged(binding)
        call = final_call(nodes)
        if call and call.name in {("Iterable", "generate"), ("List", "filled"), ("List", "generate")} and call.args:
            count = self.file.fixed_number(call.args[0])
            return type(count) is int and count > 0
        return False

    def loop(self, node, conditional):
        body = node.field("body")
        condition = node.field("condition")
        once = node.kind() == "do_statement"
        leading = NEXT
        if node.kind() == "for_statement":
            parts = next((child for child in contents(node) if child.kind() == "for_loop_parts"), node)
            items = between(tokens(parts), "(", ")")
            iterable = named(between(items, "in"))
            if iterable:
                once = self.nonempty(iterable)
                leading = self.expression(iterable, conditional)
            else:
                initial = next((child for child in items if child.kind() == "local_variable_declaration"), None)
                if initial is not None:
                    after = items[items.index(initial) + 1:]
                    condition = named(after[:next((i for i, child in enumerate(after) if child.kind() == ";"),
                                                 len(after))])
                    once = self.counting_loop(initial, condition)
                    leading = followed_by(self.visit(initial, conditional), self.expression(condition, conditional))
        elif not once:
            once = constant(as_tuple(condition)) is True
            leading = self.visit(condition, conditional)
        result = self.visit(body, conditional or not once)
        if node.kind() == "do_statement":
            result = followed_by(result, self.visit(condition, conditional or bool(result & ~NEXT)))
        exits = result & (RETURN | THROW)
        if not once or result & (NEXT | BREAK | CONTINUE):
            exits |= NEXT
        return followed_by(leading, exits)

    def counting_loop(self, initial, condition):
        definition = variable_definition(initial)
        if definition is None:
            return False
        first = self.file.fixed_number(named(between(tokens(definition), "=", ",")))
        operator, left, right = expression_operator(condition)
        bound = self.file.fixed_number(right)
        if names(left) != (declared_name(definition),) or type(first) not in {int, float} or type(bound) not in {int, float}:
            return False
        return {"<": first < bound, "<=": first <= bound, ">": first > bound, ">=": first >= bound}.get(operator, False)

    def switch(self, node, conditional):
        leading = self.visit(node.field("condition"), conditional)
        body = node.field("body")
        cases = []
        exhaustive = False
        for item in contents(body):
            if item.kind() == "switch_label":
                cases.append([])
                exhaustive |= any(child.kind() == "default" for child in tokens(item))
            elif cases:
                cases[-1].append(item)
        outcome = 0 if exhaustive else NEXT
        for case in cases:
            result = self.sequence(case, True)
            outcome |= (result & (RETURN | THROW)) | (NEXT if result & (NEXT | BREAK | CONTINUE) else 0)
        return followed_by(leading, outcome)

    def compares_itself(self, check, test_name, notes):
        if check.operator not in {"==", "!=", "is"} or not check.actual or not check.expected:
            return False
        same = signature(check.actual) == signature(check.expected)
        if not same:
            first, second = self.file.identity(check.actual), self.file.identity(check.expected)
            return first is not None and first == second
        has_call = any(final_call(nodes) is not None or any(query(node, "argument_part") for node in nodes)
                       for nodes in (check.actual, check.expected))
        if not has_call:
            return not (check.predicate and check.operator == "==" and self.file.overloads_equality(check.actual, test_name))
        if check.operator == "!=":
            return True
        site = line(check.node)
        near = " ".join(notes.get(position, "") for position in (site - 1, site))
        if any(lint.describes_distinct_inputs(text) for text in (test_name, near)):
            return True
        if (any(lint.suggests_repeatability(text) for text in (test_name, near))
                or check.operator == "==" and self.file.builds_value(check.actual)):
            return False
        subject = signature(check.actual)
        for other in self.checks:
            if other is check or not other.operator:
                continue
            for candidate, expected in ((other.actual, other.expected), (other.expected, other.actual)):
                if signature(candidate) == subject and (
                    other.operator == "!=" and signature(expected) != subject
                    or other.operator == "==" and (constant(expected) is not lint.UNKNOWN
                                                   or collection_size(expected, exact=True) is not None)
                ):
                    return False
        return True
