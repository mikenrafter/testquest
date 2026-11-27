"""Resolve observations on test fixture members to their declarations."""

from collections import defaultdict

from .ast_grep import children, offset, query
from .ast_grep_ts import FUNCTIONS, arguments, const_declaration, expression, name, text_literal


def fixture_origin(file, node, path=(), seen=frozenset(), *, in_factory=False):
    node = expression(node)
    if node is None or any(child.kind() in {"?.", "optional_chain"} for child in node.children()):
        return None
    span = offset(node), node.range().end.index
    if span in seen:
        return None
    seen = seen | {span}
    if node.kind() == "await_expression":
        return fixture_origin(file, next(iter(children(node)), None), path, seen, in_factory=in_factory)
    if node.kind() == "member_expression":
        return fixture_origin(file, node.field("object"), (name(node.field("property")), *path),
                              seen, in_factory=in_factory)
    if node.kind() == "object" and path and in_factory:
        value = object_member(node, path[0])
        return fixture_origin(file, value, path[1:], seen, in_factory=True)
    if node.kind() == "call_expression":
        callee = node.field("function")
        names = file.callee(callee)[1]
        if names in {("vi", "mocked"), ("jest", "mocked")} and arguments(node):
            return fixture_origin(file, arguments(node)[0], path, seen, in_factory=in_factory)
        if names and names[-1] in {"mockImplementation", "mockReturnValue", "mockResolvedValue",
                                   "mockRejectedValue", "mockImplementationOnce", "mockReturnValueOnce",
                                   "mockResolvedValueOnce", "mockRejectedValueOnce"}:
            return fixture_origin(file, callee.field("object"), path, seen, in_factory=in_factory)
        if in_factory and not path and names in {("vi", "fn"), ("jest", "fn")}:
            return "fixture", offset(node)
        if (target := file.helper(callee)) is not None and file.parameter_scope(callee) is None:
            body = expression(target.field("body"))
            if body is not None and body.kind() == "statement_block":
                returns = [entry for entry in query(body, "return_statement") if
                           not any(parent.kind() in FUNCTIONS and offset(body) <= offset(parent)
                                   for parent in entry.ancestors())]
                if len(returns) != 1 or offset(returns[0].parent()) != offset(body):
                    return None
                body = next(iter(children(returns[0])), None)
            return fixture_origin(file, body, path, seen, in_factory=True)
    if name(node) and file.parameter_scope(node) is None:
        binding = file.binding(node)
        destructured, key = file.destructuring(node)
        if destructured is not None and (binding is None or offset(destructured) > offset(binding)):
            return fixture_origin(file, destructured.field("value"), (key, *path), seen, in_factory=in_factory)
        if binding is None or binding.kind() != "variable_declarator":
            return None
        value = binding.field("value")
        if value is not None and not const_declaration(binding):
            return None
        if value is None:
            if file.hook_values is None:
                file.hook_values = hook_values(file)
            value = file.hook_values.get(offset(binding))
        return fixture_origin(file, value, path, seen, in_factory=in_factory)
    if in_factory and not path and node.kind() in {"array", "object"}:
        return "fixture", offset(node)
    return None


def object_member(node, key):
    for entry in reversed(children(node)):
        if entry.kind() == "spread_element":
            return None
        if entry.kind() == "pair":
            property_key = entry.field("key")
            if property_key is None or property_key.kind() == "computed_property_name":
                return None
            if (name(property_key) or text_literal(property_key)) == key:
                return entry.field("value")
        if entry.kind() == "shorthand_property_identifier" and entry.text() == key:
            return entry
    return None


def hook_values(file):
    writes = defaultdict(list)
    for assignment in query(file.syntax.root, "assignment_expression", "augmented_assignment_expression",
                            "update_expression"):
        lhs = expression(assignment.field("left") or assignment.field("argument"))
        if lhs is not None and (binding := file.binding(lhs)) is not None and file.parameter_scope(lhs) is None:
            writes[offset(binding)].append(assignment)
    values = {}
    for site, assignments in writes.items():
        if len(assignments) != 1:
            continue
        assignment = assignments[0]
        if assignment.kind() != "assignment_expression":
            continue
        body = assignment.parent().parent()
        callback = body.parent()
        if body.kind() != "statement_block" or callback.kind() not in FUNCTIONS:
            continue
        args = callback.parent()
        call = args.parent()
        if args.kind() != "arguments" or call.kind() != "call_expression":
            continue
        fn = call.field("function")
        if file.callee(fn)[1] == ("beforeEach",) and file.binding(fn) is None:
            values[site] = assignment.field("right")
    return values
