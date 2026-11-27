"""Dart's grammar represents an expression and its selectors as siblings."""

from dataclasses import dataclass
from functools import cache
from pathlib import Path
import os

from ast_grep_py import register_dynamic_language

from .ast_grep import line, shape
from .lint import UNKNOWN
from .source import Comment


COMMENTS = {"comment", "documentation_comment"}
CALLBACKS = {"function_expression", "function_expression_body", "function_body", "local_function_declaration"}
COLLECTIONS = {"list_literal", "set_or_map_literal"}


@cache
def register_grammar():
    library = os.environ.get("TQ_DART_GRAMMAR")
    if not library:
        raise ValueError("Dart requires TQ_DART_GRAMMAR to point to libtree-sitter-dart.so")
    if not Path(library).is_file():
        raise ValueError(f"Dart grammar library does not exist: {library}")
    try:
        register_dynamic_language({"dart": {
            "library_path": str(Path(library).resolve()),
            "language_symbol": "tree_sitter_dart",
            "extensions": ["dart"],
        }})
    except Exception as error:
        raise ValueError(f"Cannot register the Dart grammar at {library}: {error}") from error


def tokens(node):
    return tuple(child for child in node.children() if child.kind() not in COMMENTS)


def named(record):
    return tuple(child for child in record if child.is_named())


def contents(node):
    return named(tokens(node)) if node is not None else ()


def between(nodes, first, last=None):
    start = next((i + 1 for i, node in enumerate(nodes) if node.kind() == first), None)
    if start is None:
        return ()
    end = next((i for i in range(start, len(nodes)) if nodes[i].kind() == last), len(nodes))
    return nodes[start:end]


def unwrap(nodes):
    while len(nodes) == 1 and nodes[0].kind() == "parenthesized_expression":
        nodes = contents(nodes[0])
    return nodes


def kind(nodes):
    return nodes[0].kind() if len(nodes) == 1 else ""


def signature(nodes):
    return tuple(shape(node) for node in unwrap(nodes))


def identifier(node):
    return node.text() if node.kind() in {"identifier", "type_identifier"} else ""


def member(node):
    if node.kind() == "selector":
        selectors = contents(node)
        if len(selectors) == 1 and selectors[0].kind() in {
                "unconditional_assignable_selector", "conditional_assignable_selector"}:
            return next((identifier(child) for child in contents(selectors[0]) if identifier(child)), "")
    return ""


def names(nodes):
    nodes = unwrap(nodes)
    if not nodes or not identifier(nodes[0]):
        return ()
    members = tuple(member(node) for node in nodes[1:] if not type_selector(node))
    return (identifier(nodes[0]), *members) if all(members) else ()


def type_selector(node):
    return node.kind() == "selector" and any(child.kind() == "type_arguments" for child in contents(node))


@dataclass(frozen=True)
class Call:
    node: object
    name: tuple
    method: str
    receiver: tuple
    args: tuple
    keywords: dict
    optional: bool


def call_arguments(arguments):
    args, keywords = [], {}
    for arg in contents(arguments):
        components = contents(arg)
        if kind(components) == "named_argument":
            label, *value = contents(components[0])
            if value and (label_parts := contents(label)):
                keywords[label_parts[0].text()] = tuple(value)
        elif components:
            args.append(components)
    return tuple(args), keywords


def calls(nodes):
    for index, selector in enumerate(nodes[1:], 1):
        if selector.kind() != "selector":
            continue
        argument_part = next((child for child in contents(selector) if child.kind() == "argument_part"), None)
        if argument_part is None:
            continue
        arguments = next((child for child in contents(argument_part) if child.kind() == "arguments"), None)
        if arguments is None:
            continue
        target = tuple(child for child in nodes[:index] if not type_selector(child))
        names_path = names(target)
        method = (names_path[-1] if names_path else member(target[-1])) if target else ""
        args, keywords = call_arguments(arguments)
        optional = any(child.kind() == "conditional_assignable_selector"
                       for node in target if node.kind() == "selector" for child in contents(node))
        yield Call(nodes[0], names_path, method, target[:-1], args, keywords, optional)


def final_call(nodes):
    nodes = unwrap(nodes)
    if nodes and is_argument_selector(nodes[-1]):
        return next(reversed(list(calls(nodes))), None)
    return None


def is_argument_selector(node):
    return node.kind() == "selector" and any(child.kind() == "argument_part" for child in contents(node))


def string(node, *, display=False):
    pieces = []
    for part in contents(node):
        raw = part.text()
        if not raw:
            continue
        quote = raw[1] if raw.startswith("r") and len(raw) > 1 else raw[0]
        if quote not in {"'", '"'}:
            return None
        prefix = int(raw.startswith("r"))
        delimiter = quote * (3 if raw[prefix:].startswith(quote * 3) else 1)
        value = raw[prefix + len(delimiter):-len(delimiter)]
        if not (prefix or display or ("\\" not in value and "$" not in value)):
            return None
        pieces.append(value)
    return "".join(pieces) if pieces else None


def comments(syntax):
    for node in syntax.root.find_all(kind="comment"):
        location = node.range().start
        raw = node.text()
        note = raw[2:-2] if raw.startswith("/*") else raw[2:]
        if raw.startswith(("///", "/**")):
            note = note.removeprefix("/") if raw.startswith("///") else note.removeprefix("*")
        start = syntax.source.rfind("\n", 0, location.index) + 1
        yield Comment(line(node), location.column, note.strip(), not syntax.source[start:location.index].strip())


def split(nodes, operators):
    for index, node in enumerate(nodes):
        if node.kind() in operators or node.kind() in {"relational_operator", "type_test_operator"}:
            return node.text(), named(nodes[:index]), named(nodes[index + 1:])
    return "", (), ()


def collection_entries(node):
    entry = []
    for child in tokens(node):
        if child.kind() in {",", "]", "}"}:
            if entry:
                yield tuple(entry)
                entry.clear()
        elif child.is_named() and child.kind() != "type_arguments":
            entry.append(child)


def plain_literal(nodes):
    nodes = unwrap(nodes)
    if len(nodes) != 1:
        return UNKNOWN
    node = nodes[0]
    match node.kind():
        case "true" | "false" | "null_literal":
            return {"true": True, "false": False, "null_literal": None}[node.kind()]
        case "decimal_integer_literal" | "hex_integer_literal" | "decimal_floating_point_literal":
            text = node.text().replace("_", "")
            try:
                if node.kind() == "decimal_floating_point_literal":
                    return float(text)
                return int(text, 16 if node.kind() == "hex_integer_literal" else 10)
            except ValueError:
                return UNKNOWN
        case "string_literal":
            value = string(node)
            return UNKNOWN if value is None else value
    return UNKNOWN
