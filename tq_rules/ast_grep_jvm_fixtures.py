"""Facts that follow from the source of a JVM test fixture."""

from .ast_grep import children, offset, query, unwrap
from .ast_grep_jvm import body_of, call_of, child, declaration_name, member, name, node_key
from .ast_grep_jvm_checks import compare, literal, negate, split, truth
from .lint import UNKNOWN


def java_list_fixture(file, source):
    """Read a local helper that fills a new list with explicit add calls."""
    called = call_of(source)
    helper = file.helper(source, called) if called is not None and called.receiver is None else None
    if helper is None:
        if not name(source):
            return ()
        scope, binding = file.visible_binding(source, name(source))
        if scope is None or scope.kind() != "block":
            return ()
        steps = [step for step in children(scope)
                 if offset(binding) <= offset(step) and step.range().end.index <= offset(source)]
        return java_additions(file, binding, steps)
    defining_file, declaration = helper
    steps = children(body_of(declaration))
    if not steps or steps[-1].kind() != "return_statement":
        return ()
    returned = next(iter(children(steps[-1])), None)
    _, binding = defining_file.visible_binding(returned, name(returned)) if name(returned) else (None, None)
    return java_additions(defining_file, binding, steps[:-1])


def java_additions(file, binding, steps):
    initial = file.initializer(binding)
    if initial is None or initial.kind() != "object_creation_expression":
        return ()
    collection = initial.field("type")
    if collection is None or collection.text().split("<")[0] not in {"ArrayList", "LinkedList"}:
        return ()
    values = []
    identifier = declaration_name(binding)
    for step in steps:
        if node_key(step) == node_key(binding.parent()):
            continue
        references = [entry for entry in query(step, "identifier") if name(entry) == identifier]
        if not references:
            if step.kind() in {"local_variable_declaration", "expression_statement"}:
                continue
            return ()
        entry = next(iter(children(step)), None) if step.kind() == "expression_statement" else None
        added = call_of(entry)
        if added is None or added.method != "add" or len(added.args) != 1 or len(references) != 1 or (
                name(added.receiver) != identifier):
            return ()
        values.append(added.args[0])
    return tuple(values)


def fixture_truth(file, node, loop, item, active=frozenset()):
    node = unwrap(node)
    if node is None:
        return UNKNOWN
    known = truth(file.syntax, node)
    if known is not UNKNOWN:
        return known
    if (opposite := negate(node)) is not None:
        value = fixture_truth(file, opposite, loop, item, active)
        return not value if type(value) is bool else UNKNOWN
    operator, left, right = split(node)
    if operator in {"&&", "||"}:
        values = [fixture_truth(file, operand, loop, item, active) for operand in (left, right)]
        decisive = operator == "||"
        if decisive in values:
            return decisive
        return not decisive if all(type(value) is bool for value in values) else UNKNOWN
    if operator in {"==", "!="}:
        left_value, right_value = (fixture_value(file, entry, loop, item) for entry in (left, right))
        if isinstance(left_value, tuple) and isinstance(right_value, tuple) and left_value[0] == right_value[0]:
            return compare(operator, left_value[1], right_value[1])
        return compare(operator, left_value, right_value) if not isinstance(left_value, tuple) and (
            not isinstance(right_value, tuple)) else UNKNOWN
    if name(node):
        scope, binding = file.visible_binding(node, name(node))
        initial = file.stable_initializer(node, scope, binding)
        if initial is not None and (key := node_key(binding)) not in active:
            return fixture_truth(file, initial, loop, item, active | {key})
    return UNKNOWN


def fixture_value(file, node, loop, item):
    called = call_of(node)
    if called is not None and not called.args and name(called.receiver) == name(loop.field("name")):
        _, binding = file.visible_binding(called.receiver, name(called.receiver))
        if binding is None or node_key(binding) != node_key(loop) or item.kind() != "object_creation_expression":
            return UNKNOWN
        target = file.type_named(name(item.field("type")))
        if target is None or target[1].kind() != "record_declaration":
            return UNKNOWN
        defining_file, record = target
        if child(record, "class_body") is not None and children(child(record, "class_body")):
            return UNKNOWN
        parameters = children(record.field("parameters"))
        arguments = children(item.field("arguments"))
        if len(parameters) != len(arguments):
            return UNKNOWN
        for parameter, argument in zip(parameters, arguments):
            if declaration_name(parameter) == called.method:
                return fixture_value(defining_file, argument, loop, item)
        return UNKNOWN
    if node is not None and node.kind() == "field_access":
        owner, constant = member(node)
        if owner is not None and (target := file.type_named(owner.text())) is not None:
            defining_file, enum = target
            if enum.kind() == "enum_declaration" and any(declaration_name(entry) == constant
                    for entry in children(child(enum, "enum_body")) if entry.kind() == "enum_constant"):
                return (defining_file.path, node_key(enum)), constant
        # Imported enum constants have the same owner type on both sides of a comparison.
        if owner is not None and owner.text() in file.imports and constant.isupper():
            return file.imports[owner.text()], constant
    return literal(file.syntax, node)


def counted_list_builder(file, call):
    """Read a method that returns a fresh list with one addition per fixed iteration."""
    from .ast_grep_jvm import bind_arguments
    from .ast_grep_jvm_controls import declared_value, receiver_type

    _, binding, initial = declared_value(file, call.receiver)
    target = receiver_type(file, binding, initial)
    if target is None:
        return None
    defining_file, owner = target
    methods = [(fn, arguments) for scope, fn in defining_file.functions.get(call.method, ())
               if node_key(scope) == node_key(owner) and (arguments := bind_arguments(fn, call)) is not None]
    if len(methods) != 1:
        return None
    method, arguments = methods[0]
    block = child(body_of(method), "statements")
    if block is None:
        return None
    steps = children(block)
    if len(steps) < 3 or steps[-2].kind() != "for_statement" or steps[-1].kind() != "jump_expression":
        return None
    returned = children(steps[-1])
    if steps[-1].children()[0].kind() != "return" or len(returned) != 1 or not name(returned[0]):
        return None
    scope, storage, initializer = declared_value(defining_file, returned[0])
    factory = call_of(initializer)
    if factory is None or factory.receiver is not None or factory.args or factory.method not in {
            "mutableListOf", "arrayListOf"}:
        return None
    loop = steps[-2]
    index, interval, body = (children(loop) + [None, None, None])[:3]
    if index is None or interval is None or interval.kind() != "infix_expression":
        return None
    bounds = children(interval)
    if len(bounds) != 3 or name(bounds[1]) != "until" or literal(defining_file.syntax, bounds[0]) != 0:
        return None
    counter = name(bounds[2])
    if not counter:
        return None
    body = child(body, "statements")
    if body is None or not children(body):
        return None
    append = call_of(children(body)[-1])
    if append is None or append.method != "add" or len(append.args) != 1 or name(append.receiver) != name(returned[0]):
        return None
    loop_refs = query(block, "simple_identifier")
    uses = {node_key(ref) for ref in loop_refs if name(ref) == name(returned[0])}
    if uses != {node_key(child(storage, "simple_identifier")), node_key(append.receiver), node_key(returned[0])}:
        return None
    for jump in query(block, "jump_expression"):
        if node_key(jump) != node_key(steps[-1]) and jump.children()[0].kind() != "throw":
            return None
    for parameter, argument, supplied in arguments:
        if declaration_name(parameter) == counter:
            size = literal((file if supplied else defining_file).syntax, argument)
            return max(0, size) if type(size) is int else None
    return None
