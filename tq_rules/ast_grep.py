"""Syntax and source locations for the optional ast-grep lint engine."""

from dataclasses import dataclass, field

from ast_grep_py import SgRoot

from .source import Comment


COMMENTS = {"comment", "line_comment", "block_comment"}
FUNCTIONS = {"function_definition", "function_item"}
SCOPES = FUNCTIONS | {"lambda", "class_definition", "closure_expression", "macro_definition"}
CALLS = {"call", "call_expression", "macro_invocation"}
MEMBERS = {"attribute", "field_expression"}
NAMES = {"identifier", "self"}
RUST_ASSERTIONS = {
    prefix + suffix
    for prefix in ("assert", "debug_assert", "prop_assert")
    for suffix in ("", "_eq", "_ne", "_matches")
}


def query(node, *kinds):
    rules = [{"kind": kind} for kind in kinds]
    return node.find_all(**(rules[0] if len(rules) == 1 else {"any": rules}))


def children(node):
    return [child for child in node.children() if child.is_named() and child.kind() not in COMMENTS]


def offset(node):
    return node.range().start.index


def line(node):
    return node.range().start.line + 1


def location(node):
    start = node.range().start
    return start.line + 1, start.column


def text(node):
    return node.text()


def unwrap(node):
    while node is not None and node.kind() in {"parenthesized_expression", "generic_function"}:
        node = next(iter(children(node)), None)
    return node


def parts(node):
    """A binding path or a qualified callee, as AST components."""
    node = unwrap(node)
    if node is None:
        return ()
    kind = node.kind()
    if kind in NAMES | {"field_identifier", "type_identifier", "crate", "super"}:
        return (node.text().removeprefix("r#"),)
    if kind in MEMBERS | {"scoped_identifier", "scoped_type_identifier", "generic_type"}:
        elements = children(node)
        if kind == "generic_type":
            return parts(elements[0])
        if len(elements) == 2:
            base, name = map(parts, elements)
            if base and name:
                return (*base, *name)
    return ()


def macro_tree(node):
    return next((child for child in children(node) if child.kind() == "token_tree"), None)


def macro_name(node):
    return parts(node.field("macro"))


def syntax_errors(root, *, missing=False):
    errors = {line(node) for node in query(root, "ERROR")}
    if missing:
        pending = [root]
        while pending:
            for child in pending.pop().children():
                if child.is_leaf() and not child.text() and child.kind() != "block":
                    errors.add(line(child))
                elif not child.is_leaf():
                    pending.append(child)
    return sorted(errors)



class Expansion(list):
    """Macro arguments together with the syntax tree that owns their nodes."""

    def __init__(self, nodes=(), syntax=None):
        super().__init__(nodes)
        self.syntax = syntax


@dataclass
class Call:
    name: tuple
    arguments: list
    receiver: object = None
    macro: bool = False

    @property
    def method(self):
        return self.name[-1] if self.name else ""


@dataclass
class Syntax:
    root: object
    source: str
    language: str
    origin: int = 0
    prefix: int = 0
    expansions: dict = field(default_factory=dict)
    macro_errors: list = field(default_factory=list)

    @classmethod
    def parse(cls, source, language, *, origin=0, prefix=0):
        return cls(SgRoot(source, language).root(), source, language, origin, prefix)

    def absolute(self, node):
        return self.origin + offset(node) - self.prefix

    def comments(self):
        kinds = ("comment",) if self.language == "python" else ("line_comment", "block_comment")
        for node in query(self.root, *kinds):
            loc = node.range().start
            raw = node.text()
            content = raw[2:-2] if raw.startswith("/*") else raw[1:] if raw.startswith("#") else raw[2:]
            start = self.source.rfind("\n", 0, loc.index) + 1
            yield Comment(loc.line + 1, loc.column, content.strip(), not self.source[start:loc.index].strip())

    def call(self, node):
        if node.kind() == "macro_invocation":
            name = macro_name(node)
            return Call(name, self.macro_arguments(node), macro=True)
        callee = unwrap(node.field("function"))
        args = node.field("arguments")
        if args is None or callee is None:
            return Call((), [])
        receiver = children(callee)[0] if callee.kind() in MEMBERS else None
        name = parts(callee)
        if receiver is not None and not name:
            name = parts(children(callee)[-1])
        return Call(name, [args] if args.kind() == "generator_expression" else children(args), receiver)

    def macro_arguments(self, node):
        key = offset(node), node.text()
        if key not in self.expansions:
            self.expansions[key] = self.parse_arguments(node)
        return self.expansions[key]

    def parse_arguments(self, node):
        tree = macro_tree(node)
        if tree is None or not children(tree):
            return []
        name = macro_name(node)
        if name and name[-1] in RUST_ASSERTIONS and name[-1].endswith(("_eq", "_ne")):
            opening, closing = tree.text()[0], tree.text()[-1]
            match = node.find(pattern="::".join(name) + "!" + opening + "$LEFT, $RIGHT" + closing)
            if match is not None:
                operands = [match.get_match(side) for side in ("LEFT", "RIGHT")]
                if all(operand is not None and operand.kind() in NAMES | {
                        "integer_literal", "float_literal", "string_literal", "raw_string_literal",
                        "boolean_literal", "char_literal"} for operand in operands):
                    return Expansion(operands, self)
        prefix = "fn tq() { tq("
        parsed = Syntax.parse(prefix + tree.text()[1:-1] + "); }", "rust",
                              origin=self.absolute(tree) + 1, prefix=len(prefix))
        arguments = parsed.root.find(kind="arguments")
        if arguments is not None and not syntax_errors(parsed.root, missing=True):
            return Expansion(children(arguments), parsed)
        if name and name[-1] in RUST_ASSERTIONS:
            self.macro_errors.append(self.absolute(node))
        return []

    def property_items(self, node):
        tree = macro_tree(node)
        if tree is None or not tree.text().startswith("{"):
            return
        prefix = "mod tq {"
        source = tree.text()[1:]
        # The Rust parser recovers proptest's `name in strategy` parameter
        # syntax. Reparse the bindings and strategies as call arguments.
        template = Syntax.parse(prefix + source, "rust")
        strategies_at = {}
        edits = []
        for item in query(template.root, "function_item"):
            parameters = item.field("parameters")
            if parameters is None or item.field("body") is None:
                continue
            start = offset(parameters) + 1
            end = parameters.range().end.index - 1
            span = list((prefix + source)[start:end])
            for marker in query(parameters, "identifier"):
                if marker.text() == "in":
                    span[offset(marker) - start:offset(marker) - start + 2] = ", "
            call = Syntax.parse("fn tq() { tq(" + "".join(span) + "); }", "rust")
            args = call.root.find(kind="arguments")
            entries = children(args) if args is not None else []
            strategies = {}
            if len(entries) % 2 == 0 and not syntax_errors(call.root, missing=True):
                if all(name.kind() == "identifier" for name in entries[::2]):
                    strategies = {name.text(): value for name, value in zip(entries[::2], entries[1::2])}
            if any(parent.kind() == "function_item" for parent in item.ancestors()):
                continue
            edits.append((start, end))
            strategies_at[offset(item)] = strategies
        if not edits:
            return
        text = prefix + source
        for start, end in reversed(edits):
            padding = "".join("\n" if char == "\n" else " " for char in text[start:end])
            text = text[:start] + padding + text[end:]
        parsed = Syntax.parse(text, "rust", origin=self.absolute(tree) + 1, prefix=len(prefix))
        for function in query(parsed.root, "function_item"):
            if offset(function) in strategies_at:
                yield parsed, function, strategies_at[offset(function)]



def shape(node):
    """Compare syntax without layout, comments or grouping parentheses."""
    node = unwrap(node)
    if node is None:
        return None
    components = [child for child in node.children() if child.kind() not in COMMENTS]
    if not components:
        return node.kind(), node.text()
    return node.kind(), tuple(shape(child) for child in components)


def split_operands(node):
    node = unwrap(node)
    if node is not None and node.kind() in {"binary_expression", "binary_operator", "comparison_operator", "boolean_operator"}:
        elements = [child for child in node.children() if child.kind() not in COMMENTS]
        if len(elements) == 3:
            return elements[1].text(), elements[0], elements[2]
    return "", None, None


def opposite(node):
    node = unwrap(node)
    if node is not None and node.kind() in {"unary_expression", "not_operator", "unary_operator"}:
        elements = [child for child in node.children() if child.kind() not in COMMENTS]
        if len(elements) == 2 and elements[0].text() in {"!", "not"}:
            return elements[1]
    return None


def attributes(node):
    collected = []
    sibling = node.prev()
    while sibling is not None and sibling.kind() in {"attribute_item", *COMMENTS}:
        if sibling.kind() == "attribute_item":
            collected.append(sibling)
        sibling = sibling.prev()
    return list(reversed(collected))


def attribute_name(node):
    attribute = next(iter(children(node)), None)
    return parts(next(iter(children(attribute)), None)) if attribute is not None else ()


def namespace(node):
    owners = []
    for parent in node.ancestors():
        if parent.kind() in {"class_definition", "mod_item"}:
            owners.append(parent.field("name").text())
        elif parent.kind() == "impl_item":
            owners.append(parent.field("type").text())
        elif parent.kind() in FUNCTIONS:
            owners.append(parent.field("name").text())
    return tuple(reversed(owners))
