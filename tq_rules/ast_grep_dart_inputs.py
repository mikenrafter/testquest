"""Prove lower bounds for Dart loops over statically available inputs."""

from .ast_grep import offset, query
from .ast_grep_dart import collection_size, expression_operator
from .ast_grep_dart_syntax import (
    COLLECTIONS, between, calls, collection_entries, contents, final_call, kind, member, named, names, tokens, unwrap,
)


# The SDK declarations are available without loading an installed Flutter SDK.
# https://api.flutter.dev/flutter/dart-ui/Brightness.html
# https://api.flutter.dev/flutter/foundation/TargetPlatform.html
FLUTTER_ENUMS = {
    "Brightness": ({"dart:ui", "package:flutter/material.dart", "package:flutter/widgets.dart",
                    "package:flutter/painting.dart"}, ("dark", "light")),
    "TargetPlatform": ({"package:flutter/foundation.dart", "package:flutter/material.dart",
                        "package:flutter/widgets.dart"}, ("android", "fuchsia", "iOS", "linux", "macOS", "windows")),
}


def enum_values(file, nodes):
    nodes = unwrap(nodes)
    path = names(nodes)
    if path[-1:] != ("values",) or file.binding(nodes):
        return ()
    owner = path[:-1]
    if len(owner) == 1 and owner[0] in file.enum_members:
        return file.enum_members[owner[0]]
    if imported := file.imported_values(owner, "enum_members"):
        return imported[1]
    if len(owner) in {1, 2} and owner[-1] in FLUTTER_ENUMS:
        libraries, values = FLUTTER_ENUMS[owner[-1]]
        prefix = owner[0] if len(owner) == 2 else ""
        if any(entry.uri in libraries and entry.prefix == prefix and entry.exposes(owner[-1])
               for entry in file.imports):
            return values
    return ()


def initial_collection(file, nodes):
    path = names(nodes)
    binding = file.binding(nodes)
    if binding is not None:
        if len(path) == 1 and (binding.const or file.unchanged(binding)):
            return file, binding.initializer
        return None
    if len(path) == 2 and path in file.static_constants:
        return file, file.static_constants[path]
    if path and (imported := file.imported_values(path, "static_constants")):
        return imported
    return None


def field_argument(file, call, field):
    parameters = file.constructors.get(call.name)
    if parameters is None or call.optional or kind(call.receiver) not in {"", "identifier"}:
        return ()
    positional = iter(call.args)
    for parameter in contents(parameters):
        formals = contents(parameter) if parameter.kind() == "optional_formal_parameters" else (parameter,)
        keyword = any(child.kind() == "{" for child in tokens(parameter))
        for formal in formals:
            if formal.kind() != "formal_parameter":
                continue
            initializing = next((node for node in contents(formal) if node.kind() == "constructor_param"), None)
            target = next((node.text() for node in contents(initializing) if node.kind() == "identifier"), "")
            argument = call.keywords.get(target, ()) if keyword else next(positional, ())
            if target == field and target in file.final_fields[call.name[0]]:
                return argument
    return ()


def only_reads_other_fields(file, use, other_fields):
    argument = use.parent()
    if argument.kind() != "argument" or contents(argument) != (use,):
        return False
    selector = argument.parent().parent().parent()
    for call in calls(contents(selector.parent())):
        if (use,) not in call.args:
            continue
        target = file.resolve_name((call.node, *call.receiver[1:]), {}, path=call.name) if call.name else None
        if target is None:
            return False
        supplied = target.bind(call.args, call.keywords).supplied
        key = next((key for key, arg in supplied.items() if arg == (use,)), None)
        for reference in query(target.body, "identifier"):
            binding = target.file.binding((reference,))
            if binding is None or offset(binding.node) != key:
                continue
            expression = contents(reference.parent())
            if len(expression) < 2 or expression[0] != reference or member(expression[1]) not in other_fields:
                return False
        return key is not None
    return False


def fixed_field_size(file, binding, field, seen):
    source_file, collection = initial_collection(file, unwrap(binding.iterable)) or (file, binding.iterable)
    collection = unwrap(collection)
    if kind(collection) != "list_literal":
        return 0
    items = [final_call(entry) for entry in collection_entries(collection[0])]
    if not items or items[0] is None or not items[0].name or not all(
            item is not None and item.name[:1] == items[0].name[:1] for item in items):
        return 0
    fields = source_file.final_fields[items[0].name[0]] - {field}
    for use in query(binding.scope, "identifier"):
        if (use.text() == binding.node.text() and file.binding((use,)) == binding
                and file.mutates_collection(use) and not only_reads_other_fields(file, use, fields)):
            return 0
    return min(minimum_size(source_file, field_argument(source_file, item, field), seen) for item in items)


def _unchanged_setup_collection(file, binding, setup_offsets):
    for use in query(binding.scope, "identifier"):
        if use.text() != binding.node.text() or file.binding((use,)) != binding:
            continue
        parent = use.parent()
        if parent.kind() == "assignable_expression":
            operation = parent.parent()
            if operation.kind() == "assignment_expression":
                if offset(operation) in setup_offsets:
                    continue
                return False
            if file.writes_binding(use):
                return False
        nodes = contents(parent)
        if any(call.method in {"clear", "remove", "removeAt", "removeWhere", "removeLast",
                               "removeRange", "retainWhere", "replaceRange"}
               and call.receiver and call.receiver[0] == use for call in calls(nodes)):
            return False
    return True


def minimum_size(file, nodes, seen=frozenset(), values=None, setup_collections=None):
    """Return zero when the AST cannot prove a positive minimum cardinality."""
    nodes = unwrap(nodes)
    if not nodes:
        return 0
    key = file, nodes[0].range().start.index, nodes[-1].range().end.index
    if key in seen:
        return 0
    seen |= {key}

    values_map = values
    if values_map and len(nodes) == 1 and (path := names(nodes)) and len(path) == 1:
        binding = file.binding(nodes)
        if binding is not None and (file, offset(binding.node)) in values_map:
            caller_file, raw_arg = values_map[file, offset(binding.node)]
            return minimum_size(caller_file, raw_arg, seen, values_map, setup_collections)

    if setup_collections and len(nodes) == 1 and (path := names(nodes)) and len(path) == 1:
        col_map, setup_offsets = setup_collections
        binding = file.binding(nodes)
        if binding is not None and (file, offset(binding.node)) in col_map and _unchanged_setup_collection(
                file, binding, setup_offsets):
            return minimum_size(file, col_map[file, offset(binding.node)], seen, values_map, setup_collections)

    if kind(nodes) == "string_literal":
        if any(child.kind() == "template_substitution" for child in contents(nodes[0])):
            return 0
        raw = nodes[0].text()
        stripped = raw.removeprefix("r").strip("'\"")
        return 1 if stripped else 0

    enum_vals = enum_values(file, nodes)
    if enum_vals:
        return len(enum_vals)
    if kind(nodes) in COLLECTIONS:
        minimum = collection_size(nodes)
        for entry in collection_entries(nodes[0]):
            if kind(entry) == "for_element" and kind(nodes) == "list_literal":
                element = entry[0]
                body = named(between(tokens(element), ")"))
                if body and body[0].kind() not in {"spread_element", "if_element", "for_element"}:
                    minimum += minimum_size(file, named(between(tokens(element), "in", ")")), seen, values_map)
        return minimum
    # Support Permutations(k, items)() / Combinations(k, items)() from package:trotter or helper.
    if len(nodes) >= 2 and kind(nodes[-1:]) == "selector":
        args_node = next(iter(query(nodes[-1], "arguments")), None)
        if args_node is not None and not named(contents(args_node)):
            base = unwrap(nodes[:-1])
            base_call = final_call(base)
            if base_call and base_call.method in {"Permutations", "Combinations"} and len(base_call.args) >= 2:
                k = file.fixed_number(base_call.args[0])
                n = minimum_size(file, base_call.args[1], seen, values_map)
                if type(k) is int and k >= 0 and n >= k:
                    return 1
            if len(base) == 1 and (b := file.binding(base)) is not None:
                init_call = final_call(b.initializer)
                if init_call and init_call.method in {"Permutations", "Combinations"} and len(init_call.args) >= 2:
                    k = file.fixed_number(init_call.args[0])
                    n = minimum_size(file, init_call.args[1], seen, values_map)
                    if type(k) is int and k >= 0 and n >= k:
                        return 1
    path = names(nodes)
    if member(nodes[-1]) in {"entries", "values", "keys", "indexed", "reversed"}:
        return minimum_size(file, nodes[:-1], seen, values_map, setup_collections)
    if source := initial_collection(file, nodes):
        return minimum_size(*source, seen, values_map, setup_collections)
    if len(path) == 2 and (binding := file.binding(nodes)) and binding.iterable:
        return fixed_field_size(file, binding, path[-1], seen)
    if (call := final_call(nodes)) and call.name in {
            ("Iterable", "generate"), ("List", "filled"), ("List", "generate")} and call.args:
        count = file.fixed_number(call.args[0])
        return count if type(count) is int and count > 0 else 0
    return 0


def enum_filter(file, body, variable, iterable):
    """Skip a leading continue when a remaining enum member reaches the rest."""
    if body is None or body.kind() != "block" or not (items := contents(body)):
        return None
    condition = items[0]
    if condition.kind() != "if_statement" or condition.field("alternative") is not None:
        return None
    branch = condition.field("consequence")
    if kind(contents(branch) if branch is not None and branch.kind() == "block" else ((branch,) if branch is not None else ())) != "continue_statement":
        return None
    predicate = named(between(tokens(condition), "(", ")"))
    operator, left, right = expression_operator(predicate)
    if names(right) == (variable,):
        left, right = right, left
    compared = names(right)
    values = enum_values(file, iterable)
    if (operator != "==" or not values or names(left) != (variable,) or not compared
            or compared[:-1] != names(iterable)[:-1] or compared[-1] not in values):
        return None
    return items[1:] if len(values) > 1 else None
