"""Micro-snapshot evidence from JVM assertions."""

from . import ast_grep_micro as micro
from .ast_grep import children, location, offset, shape, text
from .ast_grep_jvm import call_of, member, name


def micro_snapshot(file, test, checks):
    reads = micro.Reads(check.node for check in checks.values())
    fragments = []
    whole = []

    def root(subject):
        receiver, _ = member(subject)
        while receiver is not None:
            subject = receiver
            receiver, _ = member(subject)
        return subject

    def key(check, subject):
        target = root(subject)
        _, binding = file.visible_binding(target, name(target))
        position = offset(binding) if binding is not None else -1
        return reads.key(check.node, shape(subject), reads.assignment(check.node, text(subject), position),
                         reread=micro.has_call(subject))

    for (_, origin), check in checks.items():
        subject = check.fragment_subject
        if subject is not None:
            _, binding = file.visible_binding(subject, name(subject))
            initial = file.immutable_value(subject) or subject
            if binding is not None and binding.kind() == "variable_declarator":
                initial = binding.field("value") or initial
            call = call_of(initial)
            declaration = binding.parent() if binding is not None and binding.kind() in {
                "variable_declarator", "variable_declaration"} else binding
            collection = declaration is not None and any(
                node.kind() == "array_type" or node.kind() in {"type_identifier", "generic_type", "user_type"}
                and node.text().split("<", 1)[0] in {"List", "Set", "Map", "Collection", "Iterable", "Array"}
                for entry in (declaration, binding) for node in children(entry)
            )
            if collection or initial.kind() in {"array_creation_expression", "array_initializer", "string_literal"}:
                continue
            if call is not None:
                _, owner = member(call.receiver)
                if (call.method in {"listOf", "setOf", "mapOf", "arrayOf", "mutableListOf", "mutableSetOf",
                                    "mutableMapOf", "sequenceOf", "toList", "toSet", "split"}
                        or call.method == "of" and owner in {"List", "Set", "Map"}):
                    continue
            if micro.excluded(text(subject), text(initial), test.name):
                continue
            fragments.append(micro.Fragment(
                (*location(check.node), *origin[1:]), text(subject), key(check, subject),
                text(check.fragment_needle), check.fragment_negative,
            ))
        elif check.operator == "==":
            whole.extend((check, operand) for operand in (check.left, check.right) if operand is not None)
        elif check.snapshot_subject is not None:
            whole.append((check, check.snapshot_subject))
    micro.report(test, fragments, whole, key)
