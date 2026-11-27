"""Identify positive controls on the same test observation."""

from . import lint
from .ast_grep import children, offset, query, shape, split_operands
from .ast_grep_ts import (
    FUNCTIONS, arguments, chain, const_declaration, expression, formal_parameters, literal, name, span,
)
from .ast_grep_ts_effects import returned_value
from .ast_grep_ts_recorders import fixture_origin, object_member


def projected_control(file, subject, counter):
    if counter == "length":
        selector = projected_filter(file, subject)
        return ("filtered", selector) if selector is not None else None
    root, members = chain(subject)
    if root is not None and root.kind() == "subscript_expression" and members:
        index = literal(root.field("index"))
        if type(index) is int and index >= 0:
            origin = fixture_origin(file, root.field("object"))
            if origin is not None:
                return "indexed recorder", origin, index, members
    return None


def projected_filter(file, node, supplied=None, path=(), seen=frozenset()):
    node, supplied = file.fixed(node, supplied)
    if node is None or node.kind() != "call_expression" or span(node) in seen:
        return None
    seen = seen | {span(node)}
    fn = expression(node.field("function"))
    args = arguments(node)
    if fn is None or any(child.kind() == "optional_chain" for child in (*node.children(), *fn.children())):
        return None
    target = file.helper(fn)
    if target is not None:
        return projected_filter(file, returned_value(target),
                                {**supplied, **file.fixture_arguments(target, args, supplied)}, path, seen)
    _, members = chain(fn)
    if not members:
        return None
    receiver = fn.field("object")
    method = members[-1]
    if method == "sort" and not args:
        return projected_filter(file, receiver, supplied, path, seen)
    if method == "map" and len(args) == 1 and args[0].kind() in FUNCTIONS and not path:
        return projected_filter(file, receiver, supplied, seen=seen)
    if method != "filter" or len(args) != 1 or args[0].kind() not in FUNCTIONS:
        return None
    callback = args[0]
    test = returned_value(callback)
    operator, guard, tail = split_operands(test)
    predicate = expression(tail if operator == "&&" else test)
    if predicate is None or predicate.kind() != "call_expression":
        return None
    fn = predicate.field("function")
    if name(fn.field("property")) != "startsWith" or len(args := arguments(predicate)) != 1:
        return None
    base, fields = chain(fn.field("object"))
    if name(base) not in formal_parameters(callback):
        return None
    if operator == "&&":
        guard = expression(guard)
        for _ in range(2):
            if guard is None or guard.kind() != "unary_expression" or guard.children()[0].kind() != "!":
                return None
            guard = expression(guard.field("argument"))
        if shape(guard) != shape(expression(fn.field("object"))):
            return None
    prefix = prefix_key(file, args[0], supplied)
    if operator == "&&" and prefix and not any(isinstance(part, str) and part for part in prefix):
        return None
    origin = projected_source(file, receiver, supplied, (*fields, *path), seen)
    if prefix is not None and origin is not None:
        return origin, prefix
    return None


def prefix_key(file, node, supplied):
    resolved, bindings = file.fixed(node, supplied)
    value = literal(resolved)
    if isinstance(value, str):
        return (value,)
    if resolved is None:
        return None
    binding = file.binding(resolved)
    if binding is not None and binding.kind() == "variable_declarator" and const_declaration(binding):
        value = literal(binding.field("value"))
        if isinstance(value, str):
            return (value,)
    if resolved.kind() == "template_string":
        parts = []
        for child in children(resolved):
            if child.kind() == "string_fragment":
                parts.append(child.text())
            elif child.kind() == "template_substitution" and children(child):
                key = prefix_key(file, children(child)[0], bindings)
                if key is None:
                    return None
                parts.extend(key)
            else:
                return None
        merged = []
        for part in parts:
            if merged and isinstance(part, str) and isinstance(merged[-1], str):
                merged[-1] += part
            else:
                merged.append(part)
        return tuple(merged)
    if name(resolved) in file.source_imports and file.binding(resolved) is None and file.parameter_scope(resolved) is None:
        return (file.source_imports[name(resolved)],)
    return None


def projected_source(file, node, supplied, fields, seen):
    resolved, bindings = file.fixed(node, supplied)
    if resolved is None or span(resolved) in seen:
        return None
    if resolved.kind() == "call_expression":
        fn = expression(resolved.field("function"))
        args = arguments(resolved)
        target = file.helper(fn)
        if target is not None:
            return projected_source(file, returned_value(target),
                                    {**bindings, **file.fixture_arguments(target, args, bindings)}, fields,
                                    seen | {span(resolved)})
        if name(fn.field("property")) == "map" and len(args) == 1 and args[0].kind() in FUNCTIONS:
            projection = returned_value(args[0])
            base, path = chain(projection)
            if name(base) in formal_parameters(args[0]) and path:
                return projected_source(file, fn.field("object"), bindings, (*path, *fields),
                                        seen | {span(resolved)})
            receiver = fn.field("object")
            origin = projected_source(file, receiver, bindings, (), seen | {span(resolved)})
            if origin is not None:
                return ("map", origin, shape(args[0])), fields
            return None
    base, members = chain(resolved)
    if base is not None and base.kind() == "call_expression" and file.callee(base.field("function"))[1] in {
            ("vi", "fn"), ("jest", "fn")}:
        return (("recorder", offset(base)), members, fields)
    if not name(base):
        return None
    base, _ = file.fixed(base, bindings)
    origin = fixture_origin(file, base) or file.sibling_control(base)
    return (origin, members, fields) if origin is not None else None


def returned_mock_control(file, subject, absence, checks):
    """A later positive call can observe a unique token supplied by this recorder."""
    recorder = file.recorder(subject)
    for check in checks:
        if (check.guarded or check.negated or check.method not in {
                "toHaveBeenCalledWith", "toHaveBeenCalledExactlyOnceWith"}
                or offset(check.node) <= offset(absence.node) or file.recorder(check.actual) == recorder):
            continue
        value, fields = chain(check.expected)
        binding = file.binding(value)
        if (not fields or binding is None or binding.kind() != "variable_declarator"
                or not const_declaration(binding) or binding.field("value") is None
                or binding.field("value").kind() != "object"):
            continue
        token = binding.field("value")
        for field in fields:
            token = object_member(token, field) if token is not None and token.kind() == "object" else None
        text = literal(token)
        if not isinstance(text, str) or not text:
            continue
        candidates, literal_sites = mock_returns(file)
        if literal_sites.get(text) == {offset(token)} and candidates.get(offset(binding)) == {recorder}:
            return True
    return False


def mock_returns(file):
    if file.mock_returns is not None:
        return file.mock_returns
    candidates = {}
    for node in query(file.syntax.root, "call_expression"):
        fn = node.field("function")
        if name(fn.field("property")) not in {"mockReturnValue", "mockResolvedValue"}:
            continue
        args = arguments(node)
        if len(args) != 1 or (binding := file.binding(args[0])) is None:
            continue
        receiver = expression(fn.field("object"))
        while receiver is not None and receiver.kind() == "call_expression":
            callee = receiver.field("function")
            if name(callee.field("property")) not in {"mockReset", "mockClear"} or arguments(receiver):
                break
            receiver = expression(callee.field("object"))
        if receiver is not None and receiver.kind() == "call_expression":
            if file.callee(receiver.field("function"))[1] not in {("vi", "fn"), ("jest", "fn")}:
                continue
            parent = node.parent()
            if parent.kind() == "pair" and parent.parent().parent().kind() == "variable_declarator":
                declaration = parent.parent().parent()
                key = (name(declaration.field("name")), offset(declaration)), name(parent.field("key"))
            else:
                continue
        elif receiver is not None:
            key = file.recorder(receiver)
        else:
            continue
        candidates.setdefault(offset(binding), set()).add(key)
    literals = {}
    if candidates:
        for node in query(file.syntax.root, "string", "template_string"):
            value = literal(node)
            if value is not lint.UNKNOWN:
                literals.setdefault(value, set()).add(offset(node))
    file.mock_returns = candidates, literals
    return file.mock_returns
