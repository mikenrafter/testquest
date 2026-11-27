"""Resolve JVM controls to fixture declarations and observed results."""

from collections import defaultdict

from .ast_grep import children, offset, query, shape, unwrap
from .ast_grep_jvm import (
    CLASSES, FUNCTIONS, JVM_LANGUAGES, LAMBDAS, Recorder, body_of, call_of, child, declaration_name,
    member, name, node_key, read_file,
)
from .ast_grep_jvm_checks import literal
from .lint import UNKNOWN


MOCKS = {"mockk", "mock", "spyk", "spy"}
STUBS = {"every", "coEvery"}


def declared_value(file, node):
    node = unwrap(node)
    if not name(node):
        return None, None, None
    scope, binding = file.visible_binding(node, name(node))
    value = file.stable_initializer(node, scope, binding)
    if value is not None and value.kind() == "postfix_expression" and value.text().endswith("!!"):
        value = children(value)[0]
    return scope, binding, value


def mock_type(file, binding, value):
    called = call_of(value)
    if called is None or file.resolve_name(called.method) not in MOCKS:
        return None
    declared = child(binding, "user_type") or binding.parent().field("type")
    generic = child(child(value, "call_suffix"), "type_arguments")
    if generic is not None:
        declared = child(child(generic, "type_projection"), "user_type")
    if declared is None and called.args and called.args[0].kind() == "class_literal":
        declared = called.args[0].field("type") or next(iter(children(called.args[0])), None)
    return qualified_type(file, declared.text()) if declared is not None else None


def qualified_type(file, identifier):
    if (target := file.type_named(identifier)) is not None:
        return target[0].path, node_key(target[1])
    return file.imports.get(identifier, (file.package, identifier))


def verified_call(file, receiver, method):
    recorder = file.recorder(receiver)
    if recorder is None:
        return ()
    observations = [("calls", recorder)]
    scope, binding, value = declared_value(file, receiver)
    if scope is not None and scope.kind() not in CLASSES and recorder.shared is None:
        if (target := mock_type(file, binding, value)) is not None:
            observations.append(("mock-method", Recorder(recorder.identity, (target, method))))
            channel = "mock-type" if file.syntax.language == "java" else "captured-mock"
            observations.append((channel, Recorder(recorder.identity, (target,))))
        elif file.syntax.language == "java" and (factory := java_mock_factory(file, value)) is not None:
            observations.append(("mock-factory", Recorder(factory + (method,), factory + (method,))))
    return tuple(observations)


def java_mock_factory(file, value):
    called = call_of(value)
    if called is None or any(literal(file.syntax, arg) is UNKNOWN for arg in called.args):
        return None
    helper = file.helper(value, called)
    if helper is None:
        return None
    origin, declaration = helper
    steps = children(body_of(declaration))
    if not steps or steps[-1].kind() != "return_statement":
        return None
    returned = next(iter(children(steps[-1])), None)
    _, binding, initial = declared_value(origin, returned)
    if binding is not None and mock_type(origin, binding, initial) is not None:
        return origin.path, node_key(declaration), tuple(shape(arg) for arg in called.args)
    return None


def stubbed_calls(file, container):
    for entry in query(container, "call_expression"):
        stub = call_of(entry)
        if stub is not None and stub.method in STUBS:
            for callback in stub.callbacks():
                statements = child(callback, "statements")
                if statements is not None:
                    for expression in children(statements):
                        if (called := call_of(expression)) is not None:
                            yield entry, called


def capture_controls(file, body, witnesses):
    """An observed capture proves that the injected mock was reached."""
    if file.syntax.language != "kotlin":
        return set()
    expanded = set()
    sizes = {recorder.identity for channel, recorder in witnesses if channel in {"size", "capture-slot"}}
    if not sizes:
        return expanded
    for _, stubbed in stubbed_calls(file, body):
        for argument in stubbed.args:
            capture = call_of(argument)
            if capture is None or capture.method != "capture" or len(capture.args) != 1:
                continue
            recorder = file.recorder(capture.args[0])
            if recorder is not None and recorder.identity in sizes:
                expanded.update((channel, target) for channel, target in verified_call(
                    file, stubbed.receiver, stubbed.method) if channel != "mock-method")
    return expanded


def disabled_verifications(file, call):
    """Find literal OFF state connected to the verified mock's enablement stub."""
    disabled = set()
    for callback in call.callbacks():
        for recorded in children(child(callback, "statements") or callback):
            invocation = call_of(recorded)
            if invocation is None:
                continue
            scope, binding, value = declared_value(file, invocation.receiver)
            factory = call_of(value)
            if factory is None or file.resolve_name(factory.method) not in MOCKS:
                continue
            values = []
            for stub, invoked in stubbed_calls(file, value):
                if invoked.method != "isEnabled" or invoked.receiver is not None or invoked.args:
                    continue
                expression = stub.parent()
                parts = children(expression)
                if expression.kind() != "infix_expression" or len(parts) != 3 or name(parts[1]) != "returns":
                    continue
                returned = parts[-1]
                profile, flag = member(returned)
                if profile is not None:
                    source = call_of(declared_value(file, profile)[2])
                    if source is not None:
                        returned = source.named.get(flag)
                values.append(literal(file.syntax, returned))
            if len(values) == 1 and values[0] is False:
                disabled.add(file.recorder(invocation.receiver))
    return disabled


def collection_controls_for(file, node, present):
    recorder = file.collection_recorder(node)
    if recorder is None:
        return ()
    keys = [("size", recorder)]
    for registration in callback_registrations(file, recorder):
        keys.append(("recorder-callback", Recorder(recorder.identity, registration)))
    receiver, field = member(node)
    scope, binding, value = declared_value(file, receiver)
    target = receiver_type(file, binding, value)
    if target is not None and is_recording_field(*target, field):
        base = file.recorder(receiver)
        keys.append(("spy-size", base))
        if base.shared is None:
            defining_file, owner = target
            keys.append(("spy-type", Recorder(base.identity, (defining_file.path, node_key(owner)))))
    if (called := call_of(value)) is not None and called.receiver is not None and field:
        flags = [index for index, arg in enumerate(called.args) if type(literal(file.syntax, arg)) is bool]
        if len(flags) == 1:
            index = flags[0]
            if literal(file.syntax, called.args[index]) is present:
                signature = tuple(None if position == index else shape(arg)
                                  for position, arg in enumerate(called.args))
                test = next((parent for parent in binding.ancestors() if parent.kind() in FUNCTIONS), scope)
                key = (file.path, node_key(test), shape(called.receiver), called.method, signature, field)
                keys.append(("switch-size", Recorder(key)))
    return tuple(keys)


def callback_registrations(file, recorder):
    origins = callback_index(file).get(recorder.identity, set())
    if recorder.shared is not None and len(recorder.shared) == 2:
        path, _ = recorder.shared
        declaring_file = file if path == file.path else read_file(path, file.module)
        if declaring_file is not None:
            return origins | callback_index(declaring_file).get(recorder.shared, set())
    return origins


def callback_index(file):
    """Index direct recorder feeds by the receiving API and callback parameter."""
    if file.callback_origins is not None:
        return file.callback_origins
    origins = defaultdict(set)
    file.callback_origins = origins
    kotlin = file.syntax.language == "kotlin"
    if not kotlin and "::" not in file.syntax.source:
        return origins
    if kotlin and not any(mark in file.syntax.source for mark in ("+=", ".add(", ".offer(", ".append(")):
        return origins
    for entry in query(file.syntax.root, "call_expression" if kotlin else "method_invocation"):
        call = call_of(entry)
        if call is None:
            continue
        if kotlin:
            if not call.method[:1].isupper():
                continue
            owner = qualified_type(file, call.callee.text())
            for position, argument in enumerate(call.args):
                if argument.kind() not in LAMBDAS:
                    continue
                slot = callback_parameter(file, call, argument, position)
                for written in callback_writes(argument):
                    if (recorder := file.recorder(written)) is not None:
                        origins[recorder.identity].add((owner, "", slot))
        elif call.receiver is not None and name(call.receiver)[:1].isupper():
            owner = qualified_type(file, name(call.receiver))
            for position, argument in enumerate(call.args):
                if argument.kind() != "method_reference":
                    continue
                referenced = children(argument)
                if len(referenced) == 2 and name(referenced[1]) in {"add", "offer", "append"}:
                    if (recorder := file.recorder(referenced[0])) is not None:
                        origins[recorder.identity].add((owner, call.method, position))
    return origins


def callback_parameter(file, call, argument, position):
    for key, value in call.named.items():
        if value is argument:
            return key
    target = file.type_named(call.callee.text())
    constructor = child(target[1], "primary_constructor") if target is not None else None
    params = [entry for entry in children(constructor) if entry.kind() == "class_parameter"] if constructor else []
    if position < len(params):
        return declaration_name(params[position])
    return position


def callback_writes(callback):
    statements = body_of(callback)
    if statements is None:
        return
    for entry in children(statements):
        parts = children(entry)
        if entry.kind() == "assignment" and len(parts) >= 2 and any(
                token.kind() == "+=" for token in entry.children()):
            target = child(parts[0], "navigation_expression", "simple_identifier") or parts[0]
            yield target
        elif (call := call_of(entry)) is not None and call.method in {"add", "offer", "append"}:
            yield call.receiver


def captured_slot(file, operand):
    operand = unwrap(operand)
    if operand is None or "?." in operand.text():
        return None
    while (receiver := member(operand)[0]) is not None:
        if member(operand)[1] == "captured":
            _, binding, value = declared_value(file, receiver)
            factory = call_of(value)
            if binding is not None and factory is not None and factory.method == "slot":
                return file.recorder(receiver)
        operand = receiver
    return None


def receiver_type(file, binding, value):
    if (constructed := file.constructed_type(value)) is not None:
        defining_file, type_name = constructed
        return defining_file.type_named(type_name)
    if binding is not None:
        declared = child(binding, "user_type") or binding.parent().field("type")
        if declared is not None:
            return file.type_named(declared.text())
    return None


def is_recording_field(file, owner, field):
    for entry in query(owner, "call_expression" if file.syntax.language == "kotlin" else "method_invocation"):
        called = call_of(entry)
        if called is not None and called.method in {"add", "addLast", "offer", "append", "put"}:
            receiver, identifier = member(called.receiver)
            if identifier == field and (receiver is None or receiver.text() == "this"):
                scope, binding = file.visible_binding(entry, identifier)
                if scope is not None and node_key(scope) == node_key(owner):
                    return True
    return False


def passing_evidence(exits):
    from .ast_grep_jvm_flow import merge

    passing = [evidence for reason, evidence in exits.items()
               if reason not in {"throw", "assertion-failure"}]
    return merge(passing or exits.values())


def analyze(file, body, sizes=None):
    from .ast_grep_jvm_flow import Flow

    flow = Flow(file, sizes)
    exits = flow.visit(body_of(body) if body.kind() in LAMBDAS else body)
    return flow, passing_evidence(exits)


def shared_keys(witnesses):
    return {(channel, recorder.shared) for channel, recorder in witnesses if recorder.shared is not None}


def sibling_controls(file, wanted, cache):
    if not wanted or file.module is None:
        return set()
    paths_key = "jvm-test-sources", file.module
    if paths_key not in cache:
        roots = (source_set for source_set in (file.module / "src").glob("*")
                 if "test" in source_set.name.lower())
        listed = {key[-1] for key in cache if key[:2] == ("jvm", file.module)}
        listed.update(path for root in roots for path in root.rglob("*")
                      if path.suffix in JVM_LANGUAGES and path.is_file() and not path.is_symlink())
        cache[paths_key] = sorted(listed)
    known = set()
    for path in cache[paths_key]:
        if path == file.path:
            continue
        key = "jvm", file.module, path
        if key not in cache:
            controls = set()
            if (sibling := read_file(path, file.module)) is not None:
                for _, body, ignored, _ in sibling.inventory():
                    if not ignored:
                        _, evidence = analyze(sibling, body)
                        witnesses = evidence.witnesses | capture_controls(sibling, body, evidence.witnesses)
                        controls.update(shared_keys(witnesses))
            cache[key] = controls
        known.update(wanted & cache[key])
        if wanted <= known:
            break
    return known


def sized_source(file, node):
    """Identify the measured producer with its receiver and arguments."""
    node = unwrap(node)
    if node is None:
        return None
    if name(node):
        recorder = file.recorder(node)
        if recorder is not None and recorder.shared is not None:
            return recorder.shared
        node = declared_value(file, node)[2]
    called = call_of(node)
    if called is None or called.receiver is None or called.callbacks():
        return None
    receiver = file.recorder(called.receiver)
    if receiver is None:
        return None
    args = tuple((next((key for key, value in called.named.items() if value is arg), ""), shape(arg))
                 for arg in called.args)
    return receiver.shared or receiver.identity, called.method, args
