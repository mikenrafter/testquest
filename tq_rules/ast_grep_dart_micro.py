"""Micro-snapshot evidence from Dart assertions."""

from . import ast_grep_micro as micro
from .ast_grep import location, offset, query
from .ast_grep_dart import constant, returned_expression
from .ast_grep_dart_syntax import COLLECTIONS, calls, contents, final_call, kind, names, signature, unwrap


def string_needle(nodes):
    nodes = unwrap(nodes)
    return kind(nodes) == "string_literal" and nodes[0].find(kind="template_substitution") is None


def subject_text(nodes):
    return "".join(node.text() for node in nodes)


def collection_subject(file, subject, binding):
    binding_type = ()
    if binding is not None and len(names(subject)) == 1:
        declaration = contents(binding.node.parent())
        binding_type = declaration[:next((i for i, child in enumerate(declaration)
                                           if offset(child) == offset(binding.node)), 0)]
        subject = unwrap(binding.initializer)
    if any(node.kind() == "type_identifier" and node.text() in {"List", "Set", "Map", "Iterable"}
           for node in binding_type):
        return True
    if kind(subject) in COLLECTIONS:
        return True
    chain = list(calls(unwrap(subject)))
    if not chain:
        return False
    last = chain[-1]
    if last.method in {"map", "where", "toList", "toSet", "split", "expand", "whereType", "cast"}:
        return True
    if last.method == "join" and any(call.method in {"toList", "toSet", "map"} for call in chain[:-1]):
        return bool(last.args and constant(last.args[0]) in {",", ", ", "|", ";"})
    if len(last.name) == 1 and (helper := file.resolve_call(last, {})) is not None:
        parent = helper.body.parent()
        if parent is not None:
            return any(node.kind() == "type_identifier" and node.text() in {"List", "Set", "Map", "Iterable"}
                       for entry in contents(parent) if offset(entry) < offset(helper.body)
                       for node in query(entry, "type_identifier"))
    return False


def micro_snapshot(file, test, checks):
    reads = micro.Reads(check.node for check in checks)
    fragments = []
    whole = []

    def key(check, subject):
        source = check.file or file
        identity = source.recorder_identity(subject) or signature(subject)
        position = reads.assignment(check.node, subject_text(subject))
        return reads.key(check.node, identity, (source.path, position),
                         reread=next(calls(unwrap(subject)), None) is not None)

    for check in checks:
        matcher, args = check.matcher, check.matcher_args
        negative = matcher == "isNot"
        if negative and args:
            nested = final_call(args[0])
            if nested:
                matcher, args = (check.file or file).framework_name(nested.name), nested.args
        if matcher == "contains" and args and string_needle(args[0]):
            source = check.file or file
            subject = subject_text(check.actual)
            binding = source.binding(check.actual)
            initial = binding.initializer if binding is not None else check.actual
            candidate = final_call(check.actual)
            if candidate is not None and (helper := source.resolve_call(candidate, {})) is not None:
                initial = returned_expression(helper.body)
            if (collection_subject(source, check.actual, binding) or collection_subject(source, initial, None)
                    or string_needle(initial) or micro.excluded(subject, subject_text(initial), test.name)):
                continue
            fragments.append(micro.Fragment(location(check.node), subject, key(check, check.actual),
                                            subject_text(args[0]), negative))
        elif check.operator in {"==", "is"} and matcher != "contains":
            whole.extend((check, subject) for subject in (check.actual, check.expected) if subject)
        elif matcher in {"matchesGoldenFile", "matchesReferenceImage"} and check.matcher == matcher:
            whole.append((check, check.actual))
    micro.report(test, fragments, whole, key)
