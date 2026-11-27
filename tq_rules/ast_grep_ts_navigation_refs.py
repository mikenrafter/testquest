"""Carry source expressions across the calls and JSX props of a click handler."""

from dataclasses import dataclass, field

from . import lint
from .ast_grep import children, offset, split_operands
from .ast_grep_ts import FUNCTIONS, arguments, chain, compare, expression, literal, name, text_literal
from .ast_grep_ts_imports import imported_helper
from .ast_grep_ts_ui import reaching_initializer, return_expression


@dataclass
class Reference:
    file: object
    node: object
    bindings: dict = field(default_factory=dict)

    def at(self, node):
        return Reference(self.file, expression(node), self.bindings)

    def bind(self, arguments=(), props=None):
        formal = self.node.field("parameters")
        entries = children(formal) if formal is not None else [self.node.field("parameter")]
        bound = {}
        for index, entry in enumerate(entries):
            if entry is None:
                continue
            pattern = entry.field("pattern") or entry
            argument = arguments[index] if index < len(arguments) else None
            if name(pattern):
                bound[name(pattern)] = argument
            elif pattern.kind() == "object_pattern":
                for item in children(pattern):
                    alias = name(item.field("value") or item)
                    key = name(item.field("key") or item)
                    if alias:
                        bound[alias] = props.get(key) if props is not None else None
        return Reference(self.file, self.node, {**self.bindings, offset(self.node): bound})

    def key(self):
        return self.file.path, offset(self.node), self.node.range().end.index


def object_reference(ref, key):
    for entry in reversed(children(ref.node)):
        if entry.kind() == "spread_element":
            return None
        if entry.kind() == "pair":
            prop = entry.field("key")
            if prop.kind() == "computed_property_name":
                return None
            if (name(prop) or text_literal(prop)) == key:
                return ref.at(entry.field("value"))
        if entry.kind() in {"shorthand_property_identifier", "method_definition"}:
            if name(entry.field("name") or entry) == key:
                return ref.at(entry)
    return None


def resolve(ref, fields=(), seen=frozenset()):
    """Resolve a value or member, returning None if the source cannot be followed."""
    if ref is None or ref.node is None:
        return None
    file, node = ref.file, ref.node
    key = (*ref.key(), fields)
    if key in seen or len(seen) >= 24:
        return ref if not fields else None
    seen = seen | {key}
    kind = node.kind()
    if kind == "member_expression":
        imported = imported_helper(file, node)
        if imported is not None and imported[0] is not None:
            return resolve(Reference(imported[0], expression(imported[1])), fields, seen)
        member = resolve(ref.at(node.field("object")), (name(node.field("property")), *fields), seen)
        return member or (ref if not fields else None)
    if name(node):
        binding = file.binding(node)
        for parent in node.ancestors():
            if binding is not None and offset(binding) > offset(parent):
                break
            parameters = ref.bindings.get(offset(parent), {}) if parent.kind() in FUNCTIONS else {}
            if name(node) in parameters:
                return resolve(parameters[name(node)], fields, seen)
        if file.parameter_scope(node) is not None:
            return None
        value, member = reaching_initializer(file, node)
        if value is not None:
            return resolve(ref.at(value), (member, *fields) if member else fields, seen)
        if (helper := file.helper(node)) is not None and not fields and binding.kind() != "variable_declarator":
            return ref.at(helper)
        if (imported := imported_helper(file, node)) is not None and imported[0] is not None:
            return resolve(Reference(imported[0], expression(imported[1])), fields, seen)
    if kind == "object" and fields:
        return resolve(object_reference(ref, fields[0]), fields[1:], seen)
    if kind == "call_expression" and fields:
        callback = resolve(ref.at(node.field("function")), seen=seen)
        if callback is not None and callback.node.kind() in FUNCTIONS:
            invoked = callback.bind([ref.at(arg) for arg in arguments(node)])
            return resolve(invoked.at(return_expression(callback.node)), fields, seen)
    return ref if not fields else None


def static_value(ref, seen=frozenset()):
    resolved = resolve(ref)
    if resolved is None or resolved.key() in seen or len(seen) >= 12:
        return lint.UNKNOWN
    seen = seen | {resolved.key()}
    node = resolved.node
    if node.kind() == "template_string":
        parts = []
        for part in children(node):
            if part.kind() == "string_fragment":
                parts.append(part.text())
            elif part.kind() == "template_substitution" and children(part):
                value = static_value(resolved.at(children(part)[0]), seen)
                if not isinstance(value, str):
                    return lint.UNKNOWN
                parts.append(value)
            else:
                return lint.UNKNOWN
        return "".join(parts)
    if node.kind() == "call_expression" and not arguments(node):
        _, members = chain(node.field("function"))
        if members[-1:] == ("toLowerCase",):
            receiver = static_value(resolved.at(node.field("function").field("object")), seen)
            if isinstance(receiver, str):
                return receiver.lower()
    return literal(node)


def branch_possible(ref, branch):
    """Keep runtime guards, but require a resolved positive equality on a supplied input."""
    operator, left, right = split_operands(ref.node)
    if operator in {"&&", "||"}:
        possible = (branch_possible(ref.at(part), branch) for part in (left, right))
        return all(possible) if branch == (operator == "&&") else any(possible)
    if operator in {"===", "==", "!==", "!="}:
        lhs, rhs = (static_value(ref.at(part)) for part in (left, right))
        if lhs is not lint.UNKNOWN and rhs is not lint.UNKNOWN:
            value = compare(operator, lhs, rhs)
            return value is lint.UNKNOWN or value == branch
        if branch == (operator in {"===", "=="}) and (lhs is lint.UNKNOWN) != (rhs is lint.UNKNOWN):
            known = right if lhs is lint.UNKNOWN else left
            if literal(known) is lint.UNKNOWN:
                return False
    value = static_value(ref)
    return value is lint.UNKNOWN or bool(value) == branch
