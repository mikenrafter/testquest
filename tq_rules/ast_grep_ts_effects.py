"""Follow Effect programs when a test returns, awaits or yields their execution."""

from .ast_grep import children, offset, query
from .ast_grep_ts import FUNCTIONS, arguments, chain, expression, formal_parameters, literal, name, span
from .ast_grep_ts_imports import imported_helper
from .ast_grep_ts_recorders import object_member


# These wrappers retain the input Effect and propagate its failures.
WRAPPERS = {"provide", "provideService", "provideServiceEffect", "scoped", "withSpan", "annotateLogs",
            "map", "as", "asVoid", "flatMap", "tap", "andThen", "orDie", "ensuring", "withTracerEnabled",
            "withTracer", "delay"}


def effect_reference(file, node):
    base, members = chain(node)
    source, imported = file.source_imports.get(name(base), ("", ()))
    if not source or file.binding(base) is not None or file.parameter_scope(base) is not None:
        return ()
    if source in {"effect", "effect/Function"}:
        return (*imported, *members)
    if source in {"effect/Effect", "effect/Predicate", "effect/Deferred"}:
        return (source.split("/")[-1], *imported, *members)
    return ()


def returned_value(callback):
    body = expression(callback.field("body"))
    if body is None or body.kind() != "statement_block":
        return body
    statements = children(body)
    if len(statements) == 1 and statements[0].kind() == "return_statement":
        return next(iter(children(statements[0])), None)
    return None


def effect_program(file, node, supplied, *, runtime=False, seen=frozenset()):
    """Resolve a connected check generator or an unconditional Effect.fail."""
    node = expression(node)
    if node is None or any(child.kind() in {"optional_chain", "?."} for child in node.children()):
        return None
    site = span(node), runtime
    if site in seen:
        return None
    seen = seen | {site}
    if node.kind() != "call_expression":
        value, bindings = file.fixed(node, supplied)
        if value is not None and span(value) != span(node):
            return effect_program(file, value, bindings, runtime=runtime, seen=seen)
        return None
    fn = expression(node.field("function"))
    args = arguments(node)
    reference = effect_reference(file, fn)
    if runtime and reference[:1] == ("Effect",) and reference[1:] in {
            ("runPromise",), ("runSync",)}:
        program = args[0] if args else None
        return effect_program(file, program, supplied, seen=seen)
    if not runtime:
        if reference == ("Effect", "gen") and args:
            callback, _ = file.fixed(args[-1], supplied)
            if callback is not None and callback.kind() in {"generator_function", "generator_function_declaration"}:
                return callback
        if reference in {("Effect", "sync"), ("Effect", "promise"), ("Effect", "tryPromise")} and args:
            if (callback := expression(args[0])) is not None and callback.kind() in FUNCTIONS:
                return callback
        if reference in {("Effect", "fail"), ("Deferred", "await")}:
            if reference == ("Deferred", "await") and len(args) != 1:
                return None
            return node
        if args and sql_transaction(file, fn):
            return effect_program(file, args[0], supplied, seen=seen)
        if reference[:1] == ("Effect",) and len(reference) == 2 and reference[1] in WRAPPERS and args:
            return effect_program(file, args[0], supplied, seen=seen)
        if reference == ("pipe",) and args:
            return piped_program(file, args[0], args[1:], supplied, seen)
        _, members = chain(fn)
        if members and members[-1] == "pipe":
            return piped_program(file, fn.field("object"), args, supplied, seen)
        if fn is not None and fn.kind() == "call_expression":
            if (callback := effect_function(file, fn)) is not None:
                return callback
        if name(fn):
            binding = file.binding(fn)
            if binding is not None and binding.kind() == "variable_declarator":
                if (callback := effect_function(file, expression(binding.field("value")))) is not None:
                    return callback
    target = file.helper(fn)
    if target is not None and file.parameter_scope(fn) is None:
        bindings = {**supplied, **file.fixture_arguments(target, args, supplied)}
        return effect_program(file, returned_value(target), bindings, runtime=runtime, seen=seen)
    return None


def effect_function(file, node):
    if node is None or node.kind() != "call_expression":
        return None
    fn = expression(node.field("function"))
    reference = effect_reference(file, fn)
    if fn is not None and fn.kind() == "call_expression" and len(arguments(fn)) == 1:
        reference = effect_reference(file, fn.field("function"))
    args = arguments(node)
    if reference in {("Effect", "fn"), ("Effect", "fnUntraced")} and len(args) == 1 and args[0].kind() in FUNCTIONS:
        return args[0]
    return None


def sql_transaction(file, fn):
    if fn is None or name(fn.field("property")) != "withTransaction":
        return False
    receiver = fn.field("object")
    binding = file.binding(receiver)
    if binding is None or binding.kind() != "variable_declarator":
        return False
    value = expression(binding.field("value"))
    if value is None or value.kind() != "yield_expression":
        return False
    client = expression(next(iter(children(value)), None))
    if client is None:
        return False
    if client.kind() == "call_expression":
        client = client.field("function").field("object")
    source, _ = file.source_imports.get(name(client), ("", ()))
    if source.startswith(("effect/sql/", "effect/unstable/sql/")):
        return True
    declaration, _ = file.destructuring(client)
    if declaration is None or declaration.field("value") is None:
        return False
    initializer = expression(declaration.field("value"))
    if initializer.kind() != "yield_expression":
        return False
    return any(
        call.field("function").kind() == "import" and len(args := arguments(call)) == 1
        and isinstance(package := literal(args[0]), str) and package.startswith("@effect/sql-")
        for call in query(initializer, "call_expression")
    )


def language_model_reference(file, node):
    base, members = chain(node)
    if file.binding(base) is not None or file.parameter_scope(base) is not None:
        return ()
    source, imported = file.source_imports.get(name(base), ("", ()))
    return (*imported, *members) if source == "effect/ai" else ()


def provider_callback(file, source, operators, supplied):
    if source.kind() != "call_expression":
        return None
    operation = language_model_reference(file, source.field("function"))
    if operation not in {("LanguageModel", "generateText"), ("LanguageModel", "generateObject")}:
        return None
    for operator in reversed(operators):
        if operator.kind() != "call_expression" or len(args := arguments(operator)) != 1:
            continue
        imported = imported_helper(file, operator.field("function"))
        if imported is None or imported[0] is None:
            continue
        module, wrapper = imported
        if wrapper is None or wrapper.kind() != "call_expression" or (
                effect_reference(module, wrapper.field("function")) != ("dual",)):
            continue
        definition = arguments(wrapper)
        if len(definition) != 2 or definition[1].kind() not in FUNCTIONS:
            continue
        body = returned_value(definition[1])
        parameters = formal_parameters(definition[1])
        if body is None or body.kind() != "call_expression" or len(parameters) != 2 or (
                effect_reference(module, body.field("function")) != ("Effect", "provideServiceEffect")):
            continue
        service = arguments(body)
        if len(service) != 3 or name(service[0]) != parameters[0] or (
                language_model_reference(module, service[1]) != ("LanguageModel", "LanguageModel")):
            continue
        model = service[2]
        if model.kind() != "call_expression" or (
                language_model_reference(module, model.field("function")) != ("LanguageModel", "make")):
            continue
        fields = arguments(model)
        fake = object_member(fields[0], "generateText") if fields and fields[0].kind() == "object" else None
        if fake is None or fake.kind() not in FUNCTIONS:
            continue
        calls = query(fake.field("body"), "call_expression")
        if not any((name(base), members) == (parameters[1], ("generateText",))
                   for call in calls for base, members in [chain(call.field("function"))]):
            continue
        options, _ = file.fixed(args[0], supplied)
        callback = object_member(options, "generateText") if options is not None and options.kind() == "object" else None
        return callback if callback is not None and callback.kind() in FUNCTIONS else None
    return None


def piped_program(file, source, operators, supplied, seen):
    if all(preserves_effect(file, operator) for operator in operators):
        if (program := effect_program(file, source, supplied, seen=seen)) is not None:
            return program
        if (callback := provider_callback(file, source, operators, supplied)) is not None:
            return callback
    for operator in reversed(operators):
        if operator.kind() == "call_expression":
            args = arguments(operator)
            if effect_reference(file, operator.field("function")) == ("Effect", "andThen") and len(args) == 1:
                if (program := effect_program(file, args[0], supplied, seen=seen)) is not None:
                    return program
        if not preserves_effect(file, operator):
            break
    return None


def preserves_effect(file, operator, seen=frozenset()):
    fn = operator.field("function") if operator.kind() == "call_expression" else operator
    reference = effect_reference(file, fn)
    if len(reference) == 2 and reference[0] == "Effect" and reference[1] in WRAPPERS:
        return True
    if fn is None:
        return False
    callback = file.helper(fn) if fn.kind() not in FUNCTIONS else fn
    if callback is None:
        imported = imported_helper(file, fn)
        if imported is None:
            return False
        file, callback = imported
        if file is None or callback is None:
            return False
    if callback.kind() == "call_expression":
        args = arguments(callback)
        if effect_reference(file, callback.field("function")) == ("dual",) and len(args) == 2 and literal(args[0]) == 2:
            callback = expression(args[1])
    if callback.kind() not in FUNCTIONS or (file.path, offset(callback)) in seen:
        return False
    parameters = formal_parameters(callback)
    if not parameters or not parameters[0]:
        return False
    return forwards_effect(file, returned_value(callback), callback, parameters[0],
                           seen | {(file.path, offset(callback))})


def forwards_effect(file, node, owner, parameter, seen):
    def source(value):
        scope = file.parameter_scope(value)
        return name(value) == parameter and scope is not None and offset(scope) == offset(owner)

    if node is None or node.kind() != "call_expression":
        return source(node)
    callee = node.field("function")
    args = arguments(node)
    if name(callee.field("property")) == "pipe":
        return source(callee.field("object")) and all(preserves_effect(file, item, seen) for item in args)
    if (effect_reference(file, callee) == ("Effect", "flatMap") and len(args) == 2
            and args[1].kind() in FUNCTIONS):
        if forwards_effect(file, returned_value(args[1]), owner, parameter, seen):
            return True
    return bool(args) and source(args[0]) and preserves_effect(file, node, seen)
