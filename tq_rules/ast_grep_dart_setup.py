"""Track callback count expectations installed by enclosing Dart setUp hooks."""

from collections import defaultdict

from .ast_grep import offset, query
from .ast_grep_dart import ASYNC_EXPECTATIONS, FUNCTION_BODIES
from .ast_grep_dart_syntax import between, contents, final_call, kind, named, names, tokens, unwrap


def direct_binding(file, nodes):
    nodes = unwrap(nodes)
    if nodes and nodes[-1].text() == "!":
        nodes = nodes[:-1]
    if len(names(nodes)) != 1:
        return None
    binding = file.binding(nodes)
    return (file, offset(binding.node)) if binding is not None else None


class Setups:
    def __init__(self, file, calls):
        self.assignments = defaultdict(list)
        self.collections = defaultdict(list)
        self.assignment_offsets = set()
        for call in calls:
            if (file.framework_name(call.name) != "setUp" or not call.args
                    or file.resolve_name((call.node,), {}, path=call.name) is not None):
                continue
            site = call.node.parent()
            scope = site.parent()
            if site.kind() != "expression_statement" or scope.kind() != "block":
                continue
            owner = scope.parent()
            if owner.kind() not in FUNCTION_BODIES:
                continue
            callback = file.callback(call.args[0])
            if callback is None or callback.file is not file:
                continue
            statements = contents(callback.body)
            if kind(statements) == "block":
                statements = contents(statements[0])
            direct = {offset(node) for statement in statements
                      for node in ((contents(statement)[0],) if statement.kind() == "expression_statement"
                                   and len(contents(statement)) == 1 else (statement,))
                      if node.kind() == "assignment_expression"}
            for assignment in sorted(query(callback.body, "assignment_expression"), key=offset):
                target = next(iter(contents(assignment)), None)
                binding = direct_binding(file, contents(target))
                if binding is None:
                    continue
                rhs = named(between(tokens(assignment), "="))
                expectation = final_call(rhs)
                count = file.fixed_number(expectation.keywords.get("count", ())) if expectation else None
                counted = (offset(assignment) in direct and expectation is not None
                           and file.framework_name(expectation.name) in ASYNC_EXPECTATIONS
                           and type(count) is int and count == 0)
                self.assignments[offset(owner)].append((binding, counted))
                self.collections[offset(owner)].append((binding, rhs if offset(assignment) in direct else ()))
                if offset(assignment) in direct:
                    self.assignment_offsets.add(offset(assignment))

    def callbacks_for(self, *sites):
        bindings = set()
        scopes = dict.fromkeys(offset(owner) for site in sites for owner in reversed(site.ancestors())
                               if owner.kind() in FUNCTION_BODIES)
        for scope in scopes:
            for binding, counted in self.assignments.get(scope, ()):
                if counted:
                    bindings.add(binding)
                else:
                    bindings.discard(binding)
        return bindings

    def collections_for(self, *sites):
        values = {}
        scopes = dict.fromkeys(offset(owner) for site in sites for owner in reversed(site.ancestors())
                               if owner.kind() in FUNCTION_BODIES)
        for scope in scopes:
            for binding, rhs in self.collections.get(scope, ()):
                if rhs:
                    values[binding] = rhs
                else:
                    values.pop(binding, None)
        return values, self.assignment_offsets
