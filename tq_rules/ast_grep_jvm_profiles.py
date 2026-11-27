"""Resolve a Kotlin output's early return against a test's stubbed profile."""

from .ast_grep import children, query, shape, unwrap
from .ast_grep_jvm import (
    FUNCTIONS, body_of, call_of, child, declaration_name, has_modifier, member, name, node_key, owner_of,
)
from .ast_grep_jvm_checks import literal, negate, split
from .ast_grep_jvm_controls import declared_value, stubbed_calls


def constructor_arguments(owner, call):
    constructor = child(owner, "primary_constructor")
    if constructor is None:
        return {}
    parameters = {declaration_name(param): param for param in children(constructor)
                  if param.kind() == "class_parameter"}
    if not call.named.keys() <= parameters.keys() or len(call.args) > len(parameters):
        return {}
    positional = (argument for argument in call.args if all(argument is not arg for arg in call.named.values()))
    bound = {}
    for identifier, param in parameters.items():
        value = call.named.get(identifier) if identifier in call.named else next(positional, None)
        if value is None:
            tokens = param.children()
            value = next((tokens[index + 1] for index, token in enumerate(tokens[:-1])
                          if token.kind() == "="), None)
        bound[identifier] = value
    return bound


def disabled_profile_field(file, constructed, field):
    call = call_of(constructed)
    if call is None:
        return None
    if field in call.named:
        return f"{call.method}.{field}" if literal(file.syntax, call.named[field]) is False else None
    target = file.type_named(call.callee.text())
    if target is None or not has_modifier(target[1], "data"):
        return None
    origin, owner = target
    arguments = constructor_arguments(owner, call)
    if literal(origin.syntax, arguments.get(field)) is False:
        return f"{call.method}.{field}"
    return None


def steps_of(declaration):
    body = body_of(declaration)
    if body is None:
        return []
    return children(child(body, "statements") or body)


def direct_call(file, step):
    if step.kind() == "property_declaration":
        step = file.initializer(child(step, "variable_declaration"), immutable=True)
    return call_of(step)


def constructed_fixture(file, receiver):
    value = receiver if call_of(receiver) is not None else declared_value(file, receiver)[2]
    call = call_of(value)
    if call is None:
        return None
    if call.method[:1].isupper():
        return file, call
    helper = file.helper(value, call)
    if helper is None:
        return None
    origin, declaration = helper
    steps = steps_of(declaration)
    if len(steps) == 1 and (constructed := call_of(steps[0])) is not None and constructed.method[:1].isupper():
        return origin, constructed
    return None


def setup_accessors(file, site, recorder):
    accessors = set()
    class_body = child(owner_of(site), "class_body")
    if class_body is None:
        return accessors
    for declaration in children(class_body):
        if declaration.kind() not in FUNCTIONS or not any(
                annotation in {"Before", "BeforeEach"} for annotation, _ in file.annotations(declaration)):
            continue
        for step in steps_of(declaration):
            for stub, called in stubbed_calls(file, step):
                parts = children(step)
                if (step.kind() == "infix_expression" and len(parts) == 3
                        and node_key(parts[0]) == node_key(stub) and name(parts[1]) == "returns"
                        and file.recorder(parts[2]) == recorder):
                    accessors.add((file.recorder(called.receiver), called.method, tuple(map(shape, called.args))))
    return accessors


def output_matches(origin, owner, invocation, fixture, arguments, recorder, accessors):
    receiver = name(invocation.receiver)
    scope, _ = origin.visible_binding(invocation.receiver, receiver)
    if scope is None or node_key(scope) != node_key(owner):
        return False
    if receiver in arguments and fixture.recorder(arguments[receiver]) == recorder:
        return True
    for scope, binding in origin.bindings.get(receiver, ()):
        if node_key(scope) != node_key(owner):
            continue
        value = origin.initializer(binding, immutable=True)
        if value is not None and value.kind() == "as_expression":
            value = children(value)[0]
        accessor = call_of(value)
        if accessor is None or (parameter := name(accessor.receiver)) not in arguments:
            continue
        key = fixture.recorder(arguments[parameter]), accessor.method, tuple(map(shape, accessor.args))
        if key in accessors:
            return True
    return False


def or_terms(node, inverted=False):
    node = unwrap(node)
    if (operand := negate(node)) is not None:
        if operand.kind() != "parenthesized_expression":
            yield from or_terms(operand, not inverted)
        return
    operator, left, right = split(node)
    if operator == "||":
        # Kotlin's grammar can wrap the full disjunction in the left operand's prefix.
        yield from or_terms(left, inverted)
        yield from or_terms(right)
    elif inverted:
        yield node


def disabled_guard_fields(origin, guard, lock):
    branches = children(guard)
    if guard.kind() != "if_expression" or len(branches) != 2:
        return
    condition, branch = branches
    steps = children(child(branch, "statements") or branch)
    if not steps or steps[-1].kind() != "jump_expression":
        return
    returned = children(steps[-1])
    target = child(steps[-1], "label")
    if (not returned or steps[-1].children()[0].kind() not in {"return", "return@"}
            or (target.text() if target is not None else None) != lock
            or literal(origin.syntax, returned[-1]) is not False):
        return
    for term in or_terms(condition):
        base, field = member(term)
        provider = call_of(declared_value(origin, base)[2])
        if field and provider is not None and not provider.args and (parameter := name(provider.receiver)):
            _, binding = origin.visible_binding(provider.receiver, parameter)
            if binding is not None and binding.kind() == "class_parameter":
                yield parameter, provider.method, field


def profile_setting(file, mock, method, field):
    factory = call_of(mock)
    if factory is None or file.resolve_name(factory.method) != "mockk":
        return None
    values = []
    for stub, called in stubbed_calls(file, mock):
        if called.method == method and called.receiver is None and not called.args:
            parent = stub.parent()
            parts = children(parent)
            values.append(parts[-1] if parent.kind() == "infix_expression"
                          and len(parts) == 3 and name(parts[1]) == "returns" else None)
    return disabled_profile_field(file, values[0], field) if len(values) == 1 else None


def disabled_output(file, body, site, recorder, method):
    if file.syntax.language != "kotlin" or not method or "mockk" not in file.syntax.source:
        return None
    if recorder.shared is None:
        return None
    accessors = setup_accessors(file, site, recorder)
    for entry in query(body, "call_expression"):
        invoked = call_of(entry)
        if invoked is None or not name(invoked.receiver):
            continue
        fixture = constructed_fixture(file, invoked.receiver)
        if fixture is None:
            continue
        fixture_file, constructor = fixture
        if not constructor.named:
            continue
        target = fixture_file.type_named(constructor.callee.text())
        if target is None:
            continue
        origin, owner = target
        candidates = [function for scope, function in origin.functions[invoked.method]
                      if node_key(scope) == node_key(owner)]
        if len(candidates) != 1:
            continue
        arguments = constructor_arguments(owner, constructor)
        if not arguments:
            continue
        functions = candidates.copy()
        for step in steps_of(candidates[0]):
            called = direct_call(origin, step)
            if called is not None and called.receiver is None:
                methods = [function for scope, function in origin.functions[called.method]
                           if node_key(scope) == node_key(owner)]
                if len(methods) == 1:
                    functions.extend(methods)
        for function in functions:
            steps = steps_of(function)
            wrapper = call_of(steps[0]) if len(steps) == 1 else None
            lock = None
            if wrapper is not None and wrapper.method == "withLock" and len(wrapper.callbacks()) == 1:
                lock = wrapper.method
                steps = steps_of(wrapper.callbacks()[0])
            for index, step in enumerate(steps):
                for provider, getter, field in disabled_guard_fields(origin, step, lock):
                    setting = profile_setting(fixture_file, arguments.get(provider), getter, field)
                    if setting is None:
                        continue
                    for later in steps[index + 1:]:
                        for output in query(later, "call_expression"):
                            call = call_of(output)
                            if call is not None and call.method == method and output_matches(
                                    origin, owner, call, fixture_file, arguments, recorder, accessors):
                                return setting
    return None


def access_path(node):
    if (call := call_of(node)) is not None and call.receiver is not None:
        base, path = access_path(call.receiver)
        return base, (*path, ("call", call.method, tuple(map(shape, call.args))))
    receiver, field = member(node)
    if receiver is not None:
        base, path = access_path(receiver)
        return base, (*path, ("property", field))
    return node, ()


def returned_expression(declaration):
    body = body_of(declaration)
    if body is not None and any(token.kind() == "=" for token in body.children()):
        return next(iter(children(body)), None)
    return None


def returned_accesses(file, declaration, owner, depth=0):
    expression = returned_expression(declaration)
    if expression is None:
        return
    conditional = {"lambda_literal", "anonymous_function", "if_expression", "when_expression",
                   "conjunction_expression", "disjunction_expression", "elvis_expression"}
    for entry in query(expression, "navigation_expression", "call_expression"):
        if any(parent.kind() in conditional for parent in entry.ancestors()):
            continue
        base, path = access_path(entry)
        if name(base) and path:
            yield name(base), path
        if depth == 0 and (call := call_of(entry)) is not None and call.receiver is None:
            helpers = [function for scope, function in file.functions[call.method]
                       if node_key(scope) == node_key(owner)]
            if len(helpers) == 1:
                yield from returned_accesses(file, helpers[0], owner, depth + 1)


def observed_mock_reads(file, site, invoked):
    test = next((parent for parent in site.ancestors() if parent.kind() in FUNCTIONS), None)
    if test is None:
        return
    stubs = set()
    for step in steps_of(test):
        parts = children(step)
        if step.kind() != "infix_expression" or len(parts) != 3 or name(parts[1]) not in {"returns", "answers"}:
            continue
        stub = call_of(parts[0])
        if stub is None or stub.method not in {"every", "coEvery"} or len(stub.callbacks()) != 1:
            continue
        expressions = steps_of(stub.callbacks()[0])
        if len(expressions) != 1:
            continue
        base, path = access_path(expressions[0])
        if path and path[0][0] == "call" and (recorder := file.recorder(base)) is not None:
            stubs.add((recorder, path))
    if not stubs or (fixture := constructed_fixture(file, invoked.receiver)) is None:
        return
    fixture_file, constructor = fixture
    target = fixture_file.type_named(constructor.callee.text())
    if target is None:
        return
    origin, owner = target
    methods = [function for scope, function in origin.functions[invoked.method]
               if node_key(scope) == node_key(owner)]
    if len(methods) != 1:
        return
    arguments = constructor_arguments(owner, constructor)
    for parameter, path in returned_accesses(origin, methods[0], owner):
        recorder = fixture_file.recorder(arguments.get(parameter))
        if recorder is not None and (recorder, path) in stubs:
            yield recorder


def observed_mock_controls(witnesses):
    reads = {recorder for channel, recorder in witnesses if channel == "read-returned-true"}
    return {("calls", recorder) for channel, recorder in witnesses
            if channel == "read-returned-false" and recorder in reads}
