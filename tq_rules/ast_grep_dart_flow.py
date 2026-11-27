"""Track exits from a Dart test that can occur before a completed check."""

from . import lint
from .ast_grep import line, offset, query
from .ast_grep_dart import CHANGING_COLLECTION, Check, collection_size, constant, expression_operator
from .ast_grep_dart_syntax import (
    CALLBACKS, COLLECTIONS, between, collection_entries, contents, final_call, identifier, kind,
    named, names, signature, split, tokens, unwrap,
)
from .ast_grep_lint import range_size


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


def future_returns(body):
    if body is None:
        return False
    if any(child.kind() == "async" for child in tokens(body)):
        return True
    definition = body.prev()
    if definition is not None and definition.kind() == "function_signature":
        if any(child.kind() == "type_identifier" and child.text() in {"Future", "FutureOr"}
               for child in contents(definition)):
            return True
    call = final_call(contents(body))
    return call is not None and (call.method == "expectLater" or call.name[:1] == ("Future",))


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


class Flow:
    def __init__(self, file, callback):
        self.file = file
        self.callback = callback
        self.checks = []
        self.recorders = set()
        self.active = set()

    def run(self):
        return self.body(self.callback, False, True)

    def body(self, callback, conditional, completed):
        if callback is None or offset(callback) in self.active:
            return NEXT
        if any(child.kind() in {"async*", "sync*"} for child in tokens(callback)):
            return NEXT
        self.active.add(offset(callback))
        block = next((child for child in contents(callback) if child.kind() == "block"), None)
        result = (self.visit(block, conditional, completed=completed) if block is not None else
                  self.expression(contents(callback), conditional, completed))
        self.active.remove(offset(callback))
        return result

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
        self.checks.append(check)
        return NEXT if detached else 0

    def visit(self, node, conditional, *, completed=False):
        if node is None or node.kind() in CALLBACKS:
            return NEXT
        node_kind = node.kind()
        if node_kind == "block":
            return self.sequence(contents(node), conditional)
        if node_kind in {"if_statement", "if_element"}:
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
            result = self.expression(contents(node), conditional, completed=node_kind == "return_statement")
            return EXITS[node_kind] if result else 0
        if node_kind == "assertion":
            args = next((child for child in contents(node) if child.kind() == "assertion_arguments"), None)
            condition = named(between(tokens(args), "(", ",")) if args is not None else ()
            operator, left, right = expression_operator(condition)
            return self.record(Check(node, left, right, operator, condition, True), conditional)
        if node_kind == "await_expression":
            return self.expression(contents(node), conditional, True)
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
            result = self.body(callback, conditional or not completed, completed)
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
        call = final_call(nodes)
        if call is not None:
            return self.call(call, conditional, completed)
        outcome = NEXT
        for node in nodes:
            visited = self.visit(node, conditional or bool(outcome & ~NEXT), completed=completed)
            outcome = followed_by(outcome, visited)
        return outcome

    def call(self, call, conditional, completed):
        method = self.file.framework_name(call.name)
        if method in TEST_BOUNDARIES:
            return NEXT
        optional = call.optional
        guarded = conditional or optional
        chained = call.method in FUTURE_CHAINS and bool(call.receiver)
        out = self.expression(call.receiver, conditional, completed and chained)
        for argument in (*call.args, *call.keywords.values()):
            arg = unwrap(argument)
            if call.name == ("Future", "wait") and kind(arg) == "list_literal":
                for element in collection_entries(arg[0]):
                    out = followed_by(out, self.expression(element, guarded, completed))
            else:
                out = followed_by(out, self.expression(arg, guarded, completed and call.name == ("Future", "value")))
            self.record_input(arg)
        if call.method in CHANGING_COLLECTION and (key := self.file.recorder(call.receiver)) is not None:
            self.recorders.add(key)
        check = self.file.check(call)
        if check is not None:
            return followed_by(out, self.record(check, guarded, optional or method == "expectLater" and not completed))

        if call.name in FUTURE_CONSTRUCTORS or chained or call.method in ITERATORS:
            callback = next((candidate for arg in reversed((*call.args, *call.keywords.values()))
                             if (candidate := self.file.callback(arg)) is not None), None)
            lazy = call.method in {"map", "where", "expand"}
            async_iterator = call.name == ("Future", "forEach")
            iterator = call.method in ITERATORS and not async_iterator
            bypass = (call.method in {"catchError", "timeout"} or optional or lazy
                      or iterator and not self.nonempty(call.receiver)
                      or async_iterator and not self.nonempty(call.args[0] if call.args else ()))
            awaited = (completed if call.name in FUTURE_CONSTRUCTORS or chained else not future_returns(callback))
            result = self.body(callback, guarded or bypass or not awaited, awaited)
            return followed_by(out, NEXT if bypass or not awaited or result else 0)
        if call.name and len(call.name) == 1:
            callback = self.file.callback((call.node,))
            if callback is not None:
                joined = completed or not future_returns(callback)
                result = self.body(callback, guarded or not joined, joined)
                return followed_by(out, NEXT if optional or not joined or result else 0)
        for arg in (*call.args, *call.keywords.values()):
            if kind(arg) == "function_expression":
                callback = self.file.callback(arg)
                self.body(callback, True, False)
        if method and lint.helper(method) and (len(call.name) != 1 or not self.file.binding((call.node,))):
            return followed_by(out, self.record(Check(call.node), guarded, optional))
        return out

    def record_input(self, nodes):
        path = names(nodes)
        if path and path[-1] in CHANGING_COLLECTION:
            key = self.file.recorder(unwrap(nodes)[:-1])
            if key is not None:
                self.recorders.add(key)

    def nonempty(self, nodes):
        nodes = unwrap(nodes)
        if size := collection_size(nodes):
            return size > 0
        binding = self.file.binding(nodes)
        if binding and binding.final and len(names(nodes)) == 1 and collection_size(binding.initializer):
            declaration = binding.node.parent()
            statement = declaration.parent()
            following = statement.next()
            return following is not None and any(offset(parent) == offset(following)
                                                  for parent in nodes[0].ancestors())
        call = final_call(nodes)
        if call and call.name in {("Iterable", "generate"), ("List", "filled"), ("List", "generate")} and call.args:
            count = constant(call.args[0])
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
        first = constant(named(between(tokens(definition), "=", ",")))
        operator, left, right = expression_operator(condition)
        bound = constant(right)
        name = declared_name(definition)
        if names(left) != (name,) or type(first) is not int or type(bound) is not int:
            return False
        if operator in {"<", "<="}:
            return bool(range_size(first, bound + (operator == "<=")))
        if operator in {">", ">="}:
            return bool(range_size(first, bound - (operator == ">="), -1))
        return False

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
        if not has_call or check.operator == "!=":
            return True
        site = line(check.node)
        near = " ".join(notes.get(position, "") for position in (site - 1, site))
        if any(lint.describes_distinct_inputs(text) for text in (test_name, near)):
            return True
        if any(lint.suggests_repeatability(text) for text in (test_name, near)):
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
