"""Identify positive controls on the same test observation."""

from . import lint
from .ast_grep import children, offset, query, shape, split_operands
from .ast_grep_ts import (
    FUNCTIONS, arguments, chain, const_declaration, expression, formal_parameters, literal, name, span, text,
)
from .ast_grep_ts_effects import language_model_reference, returned_value
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


def control_keys(file, counter, node, *, positive, supplied=None, seen=frozenset()):
    node = expression(node)
    if node is None or span(node) in seen:
        return set()
    seen = seen | {span(node)}
    origin = projected_control(file, node, counter) or fixture_origin(file, node) or file.sibling_control(node)
    keys = {(counter, origin)} if origin is not None else set()
    if counter == "calls":
        if (identity := zero_arg_fixture(file, node)) is not None:
            keys.add(("calls", identity))
        keys.update(("calls", identity) for identity in injected_slots(file, node))
        return keys
    resolved, bindings = file.fixed(node, supplied)
    if resolved is None:
        return keys
    if span(resolved) != span(node):
        return keys | control_keys(file, counter, resolved, positive=positive, supplied=bindings, seen=seen)
    declaration, field = file.destructuring(node)
    if field:
        source = file.sibling_control(declaration.field("value"))
        if source is not None:
            keys.add((counter, ("member", source, field)))
    if node.kind() == "yield_expression":
        return keys | control_keys(file, counter, next(iter(children(node)), None),
                                   positive=positive, supplied=bindings, seen=seen)
    if (recorder := mock_calls(file, node, bindings)) is not None:
        identity = recorder_identity(file, *recorder)
        if positive and identity is not None:
            keys.add(("calls", identity))
    if node.kind() != "call_expression":
        return keys
    callee = expression(node.field("function"))
    _, methods = chain(callee)
    args = arguments(node)
    if not methods or not args or args[0].kind() not in FUNCTIONS:
        return keys
    method = methods[-1]
    receiver = callee.field("object")
    if method == "filter":
        source = fixture_origin(file, receiver) or file.sibling_control(receiver)
        if source is None and (recorder := mock_calls(file, receiver, bindings)) is not None:
            source = recorder_identity(file, *recorder)
        if source is not None:
            keys.add((counter, ("filter", source, filter_key(file, args[0]))))
    if positive and method in {"map", "flatMap", "filter"}:
        keys.update(control_keys(file, counter, receiver, positive=True, supplied=bindings, seen=seen))
    return keys


def filter_key(file, callback):
    captures = []
    for use in query(callback, "identifier"):
        owner = file.parameter_scope(use) or file.binding(use)
        if owner is not None and offset(owner) < offset(callback):
            captures.append((name(use), offset(owner)))
    return shape(callback), tuple(captures)


def recorder_identity(file, base, members):
    origin = fixture_origin(file, base, members)
    if origin is not None:
        return origin
    detector = file.sibling_control(base)
    for field in members:
        if detector is None:
            break
        detector = "member", detector, field
    return detector


def zero_arg_fixture(file, node):
    root, fields = chain(node)
    declaration, field = file.destructuring(root)
    if declaration is None or not field:
        return None
    source = expression(declaration.field("value"))
    if source is None or source.kind() != "call_expression" or arguments(source):
        return None
    helper = file.helper(source.field("function"))
    return ("fixture member", offset(helper), field, fields) if helper is not None else None


def mock_calls(file, node, supplied):
    base, fields = chain(node)
    if fields[-2:] != ("mock", "calls"):
        return None
    base, _ = file.fixed(base, supplied)
    if base is None:
        return None
    if base.kind() == "call_expression" and file.callee(base.field("function"))[1] in {
            ("vi", "mocked"), ("jest", "mocked")}:
        args = arguments(base)
        base = args[0] if len(args) == 1 else None
    return (base, fields[:-2]) if base is not None else None


def injected_slots(file, node):
    """Match a fresh mock by the callable and the precise argument slot receiving it."""
    base, fields = chain(node)
    binding = file.binding(base)
    origin = fixture_origin(file, node)
    if binding is None and origin is None:
        return set()
    if file.injections is None:
        file.injections = injection_sites(file)
    slots, recorders = file.injections
    matches = set(recorders.get(origin, ()))
    if binding is not None:
        matches.update((identity, (*path, *fields)) for identity, path in slots.get(offset(binding), ()))
    return matches


def injection_sites(file):
    slots = {}
    fakes = {}
    for call in query(file.syntax.root, "call_expression", "new_expression"):
        fn = call.field("function") or call.field("constructor")
        base, members = chain(fn)
        if file.callee(fn)[1] in {("vi", "fn"), ("jest", "fn")}:
            if (declaration := mock_declaration(call)) is not None:
                fakes["fixture", offset(call)] = declaration
            continue
        source = file.source_imports.get(name(base))
        target = file.binding(base)
        if source is not None and target is None:
            identity = source, members
        elif target is not None and target.kind() in {"function_declaration", "class_declaration"}:
            identity = offset(target), members
        elif members and (receiver := file.sibling_control(base)) is not None:
            identity = receiver, members
        else:
            continue
        for position, arg in enumerate(arguments(call)):
            arg = expression(arg)
            if arg.kind() == "object":
                candidates = [(entry.field("value") or entry, (name(entry.field("key") or entry),))
                              for entry in children(arg) if entry.kind() in {"pair", "shorthand_property_identifier"}]
            else:
                candidates = [(arg, ())]
            for candidate, fields in candidates:
                if (owner := file.binding(candidate)) is not None and owner.kind() == "variable_declarator" and const_declaration(owner):
                    slots.setdefault(offset(owner), set()).add((("injection", identity, position), fields))
    recorders = {
        recorder: {(identity, (*prefix, *fields)) for identity, prefix in slots.get(owner, ())}
        for recorder, (owner, fields) in fakes.items()
    }
    return slots, recorders


def mock_declaration(call):
    fields = []
    for parent in call.ancestors():
        if parent.kind() == "pair":
            fields.append(name(parent.field("key")))
        elif parent.kind() == "variable_declarator":
            return offset(parent), tuple(reversed(fields))
        elif parent.kind() not in {"object", "member_expression", "call_expression"}:
            return
    return None


def positive_reads(file, check):
    """A checked index, find result, or derived request witnesses a nonempty input."""
    expected = literal(check.expected)
    exp_node = expression(check.expected)
    if check.negated or not (
        check.method in {"toBeDefined", "toBeTruthy", "toBeGreaterThan"} or
        check.operator in {"===", "=="} and (
            expected is not lint.UNKNOWN and expected is not None
            or exp_node is not None and exp_node.kind() in {"object", "array"} | FUNCTIONS
            or exp_node is not None and exp_node.kind() == "identifier" and text(exp_node) not in {"undefined", "null"}
        )
    ):
        return
    allow_member = check.method not in {"toBeNull", "toBeUndefined", "toBeFalsy"} and expected is not None
    yield from required_reads(file, check.actual, allow_member=allow_member)


def required_reads(file, node, supplied=None, seen=frozenset(), *, allow_member=False):
    node, bindings = file.fixed(node, supplied)
    if node is None or span(node) in seen:
        return
    seen = seen | {span(node)}
    kind = node.kind()
    if kind in {"member_expression", "subscript_expression"}:
        receiver = expression(node.field("object"))
        if kind == "subscript_expression" and type(index := literal(node.field("index"))) is int and index >= 0:
            _, fields = chain(receiver)
            if fields[-2:] == ("mock", "calls"):
                yield "calls", receiver.field("object").field("object"), bindings
            else:
                yield "length", receiver, bindings
        elif allow_member and kind == "member_expression" and name(node.field("property")) != "length":
            yield "length", receiver, bindings
        yield from required_reads(file, receiver, bindings, seen, allow_member=allow_member)
    elif kind == "call_expression":
        fn = expression(node.field("function"))
        if name(fn.field("property")) in {"find", "at"}:
            yield "length", fn.field("object"), bindings
        else:
            yield from required_reads(file, fn.field("object"), bindings, seen, allow_member=allow_member)
    elif kind == "new_expression" and name(node.field("constructor")) == "Request" and file.binding(
            node.field("constructor")) is None:
        for arg in arguments(node)[:1]:
            yield from required_reads(file, arg, bindings, seen, allow_member=allow_member)


def supplied_prompt_part(file, subject, checks, body):
    """The checked incremental prompt and the sent full prompt share a supplied message fixture."""
    subject = expression(subject)
    if subject is None or subject.kind() != "call_expression":
        return False
    callee = subject.field("function")
    args = arguments(subject)
    if name(callee.field("property")) != "filter" or len(args) != 1 or args[0].kind() not in FUNCTIONS:
        return False
    operator, projection, expected = split_operands(expression(returned_value(args[0])))
    item, fields = chain(projection)
    part = literal(expected)
    if operator != "===" or fields != ("type",) or not isinstance(part, str) or (
            formal_parameters(args[0]) != (name(item),)):
        return False
    message, fields = chain(callee.field("object"))
    if fields != ("content",):
        return False
    owner = next((parent for parent in subject.ancestors() if parent.kind() == "for_in_statement"), None)
    if owner is None or not any(child.kind() == "of" for child in owner.children()) or (
            name(owner.field("left")) != name(message)):
        return False
    captured, fields = chain(owner.field("right"))
    if not name(captured) or fields != ("prompt", "content"):
        return False
    binding = file.binding(captured)
    if binding is None or binding.kind() != "variable_declarator" or not const_declaration(binding):
        return False
    for check in checks:
        actual, projection = chain(check.actual)
        if (not check.guarded and check.operator in {"===", "=="} and projection == ("incrementalPrompt",)
                and file.recorder(actual) == file.recorder(captured)):
            break
    else:
        return False
    constructor = part.split("-")
    method = constructor[0] + "".join(word.title() for word in constructor[1:]) + "Part"
    prior_calls = [call for call in query(body, "call_expression")
                   if offset(call) < offset(binding) and language_model_reference(file, call.field("function")) in {
                       ("LanguageModel", "generateText"), ("LanguageModel", "generateObject")
                   }]
    if not prior_calls:
        return False
    call = max(prior_calls, key=offset)
    opts = arguments(call)
    if not opts or opts[0].kind() != "object":
        return False
    prompt = object_member(opts[0], "prompt")
    if prompt is None or prompt.kind() != "call_expression" or language_model_reference(
            file, prompt.field("function")) != ("Prompt", "fromMessages"):
        return False
    args = arguments(prompt)
    if len(args) != 1:
        return False
    fixture, _ = file.fixed(args[0])
    if fixture is None or fixture.kind() != "array":
        return False
    for entry in query(fixture, "call_expression"):
        reference = language_model_reference(file, entry.field("function"))
        args = arguments(entry)
        if reference == ("Prompt", method) or (
                reference == ("Prompt", "makePart") and args and literal(args[0]) == part):
            return True
    return False


def _filter_field_chain(subject):
    node = expression(subject)
    if node is None or node.kind() != "call_expression":
        return None
    callee = expression(node.field("function"))
    if callee is None or callee.kind() != "member_expression" or name(callee.field("property")) != "filter":
        return None
    base, fields = chain(callee.field("object"))
    if not name(base) or not fields:
        return None
    return base, fields


def _role_guard(subject, item_name, body):
    for parent in subject.ancestors():
        if offset(parent) < offset(body):
            break
        if parent.kind() != "if_statement":
            continue
        op, left, right = split_operands(expression(parent.field("condition")))
        if op not in {"===", "=="}:
            continue
        for lside, rside in ((left, right), (right, left)):
            base, fields = chain(lside)
            val = literal(rside)
            if name(base) == item_name and fields == ("role",) and isinstance(val, str):
                return val
    return None


def _proven_item_with_role(file, var_node, col_rec, role, checks, body):
    binding = file.binding(var_node)
    if binding is None or binding.kind() != "variable_declarator":
        return False
    init = expression(binding.field("value"))
    if init is None:
        return False
    var_rec = file.recorder(var_node)
    from_find = False
    if init.kind() == "call_expression":
        fn = expression(init.field("function"))
        if fn is None or fn.kind() != "member_expression" or name(fn.field("property")) != "find":
            return False
        if file.recorder(fn.field("object")) != col_rec:
            return False
        from_find = True
    elif init.kind() == "subscript_expression":
        if file.recorder(init.field("object")) != col_rec:
            return False
    else:
        return False
    role_matched = role is None
    defined_ok = not from_find
    if from_find:
        for call in query(body, "call_expression"):
            if any(offset(p) >= offset(body) and p.kind() in {"if_statement", "for_in_statement", "for_statement"}
                   for p in call.ancestors()):
                continue
            args = arguments(call)
            if name(call.field("function")) == "assertDefined" and len(args) == 1 and file.recorder(args[0]) == var_rec:
                defined_ok = True
                break
    for check in checks:
        if check.guarded or check.negated:
            continue
        if check.method in {"toBeDefined", "toBeTruthy"} and file.recorder(check.actual) == var_rec:
            defined_ok = True
        if role is not None and check.operator in {"===", "=="}:
            for side, exp in ((check.actual, check.expected), (check.expected, check.actual)):
                base, fields = chain(side)
                if fields == ("role",) and file.recorder(base) == var_rec and literal(exp) == role:
                    role_matched = True
                    defined_ok = True
    if from_find and role is not None and not role_matched:
        args = arguments(init)
        if args and args[0].kind() in FUNCTIONS:
            op, l, r = split_operands(expression(returned_value(args[0])))
            if op == "===":
                for s, v in ((l, r), (r, l)):
                    if chain(s)[1] == ("role",) and literal(v) == role:
                        role_matched = True
    return defined_ok and role_matched


def collection_member_witness(file, subject, absence, checks, body):
    """An unguarded positive filter check on a proven element of the same collection witnesses an absence check."""
    parsed = _filter_field_chain(subject)
    if parsed is None:
        return False
    item_node, fields = parsed
    loop = next((p for p in subject.ancestors()
                 if offset(p) >= offset(body) and p.kind() == "for_in_statement"
                 and any(c.kind() == "of" for c in p.children())
                 and name(p.field("left")) == name(item_node)), None)
    if loop is None:
        return False
    col_rec = file.recorder(loop.field("right"))
    role = _role_guard(subject, name(item_node), body)
    for check in checks:
        if check is absence or check.guarded:
            continue
        counter, pos_sub, presence = check.observation()
        if counter != "length" or presence is not True:
            continue
        pos_parsed = _filter_field_chain(pos_sub)
        if pos_parsed is None or pos_parsed[1] != fields:
            continue
        if _proven_item_with_role(file, pos_parsed[0], col_rec, role, checks, body):
            return True
    return False
