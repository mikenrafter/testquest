from dataclasses import dataclass
import re

from .lint import UNKNOWN, helper, symmetric_input
from .source import Comment, Test


RAW = re.compile(r'(?:br|cr|r)(#*)"')
QUOTED = re.compile(r'(?:[bc])?"(?:[^"\\]|\\[\s\S])*"')
CHAR = re.compile(r"(?:b)?'(?:[^'\\\n]|\\(?:x[\da-fA-F]{2}|u\{[\da-fA-F_]+\}|[^\n]))'")
WORD = re.compile(r"(?:r#)?(?:[^\W\d]|_)\w*")
NUMBER = re.compile(r"(?:0[xob][0-9a-fA-F_]+|[0-9][0-9_]*)(?:[ui](?:8|16|32|64|128|size))?")
SYMBOL = re.compile(r"::|=>|->|==|!=|<=|>=|&&|\|\||\+=|[^\s]")
CLOSE = {"(": ")", "[": "]", "{": "}"}
ASSERTIONS = {"assert", "assert_eq", "assert_ne", "prop_assert", "prop_assert_eq", "prop_assert_ne",
              "debug_assert", "debug_assert_eq", "debug_assert_ne"}


@dataclass(frozen=True)
class Token:
    text: str
    line: int
    column: int
    kind: str = ""
    children: tuple = ()
    end: int = 0


def tokenize(source):
    tokens, comments = [], []
    offset, line, line_start = 0, 1, 0
    while offset < len(source):
        start = offset
        if source[offset].isspace():
            offset += 1
        elif source.startswith("//", offset):
            end = source.find("\n", offset)
            offset = len(source) if end == -1 else end
            comments.append(Comment(line, start - line_start, source[start + 2:offset].strip(),
                                    not source[line_start:start].strip()))
        elif source.startswith("/*", offset):
            offset += 2
            depth = 1
            while depth and offset < len(source):
                if source.startswith("/*", offset):
                    depth += 1
                    offset += 2
                elif source.startswith("*/", offset):
                    depth -= 1
                    offset += 2
                else:
                    offset += 1
            comments.append(Comment(line, start - line_start, source[start + 2:offset - 2].strip(),
                                    not source[line_start:start].strip()))
        else:
            raw = RAW.match(source, offset)
            if raw:
                delimiter = '"' + raw[1]
                end = source.find(delimiter, raw.end())
                offset = len(source) if end == -1 else end + len(delimiter)
                kind = "raw"
            else:
                for kind, pattern in (("quoted", QUOTED), ("char", CHAR), ("number", NUMBER),
                                      ("word", WORD), ("symbol", SYMBOL)):
                    match = pattern.match(source, offset)
                    if match:
                        offset = match.end()
                        break
            tokens.append(Token(source[start:offset], line, start - line_start, kind))
        if "\n" in source[start:offset]:
            line += source.count("\n", start, offset)
            line_start = source.rfind("\n", start, offset) + 1
    return tokens, comments


def group(tokens):
    stack = [(None, [])]
    for token in tokens:
        if token.text in CLOSE:
            stack.append((token, []))
        elif len(stack) > 1 and token.text == CLOSE[stack[-1][0].text]:
            opening, children = stack.pop()
            stack[-1][1].append(Token(opening.text, opening.line, opening.column,
                                      "group", tuple(children), token.line))
        else:
            stack[-1][1].append(token)
    if len(stack) != 1:
        raise ValueError(f"Unclosed Rust delimiter at line {stack[-1][0].line}")
    return tuple(stack[0][1])


def shape(tokens):
    return tuple((token.text, shape(token.children)) if token.children else token.text for token in tokens)


def split(tokens, delimiter=","):
    start = 0
    generic_depth = 0
    for index, token in enumerate(tokens):
        if token.text == "<" and (generic_depth or (index and tokens[index - 1].text == "::")):
            generic_depth += 1
        elif token.text == ">" and generic_depth:
            generic_depth -= 1
        elif not generic_depth and token.text == delimiter:
            yield tokens[start:index]
            start = index + 1
    if start < len(tokens):
        yield tokens[start:]


def literal(tokens):
    if len(tokens) == 3 and shape(tokens[:2]) == ("vec", "!") and tokens[2].kind == "group":
        tokens = tokens[2:]
    if len(tokens) != 1:
        return UNKNOWN
    token = tokens[0]
    if token.kind == "group" and token.text in {"[", "("}:
        values = [literal(part) for part in split(token.children)]
        if UNKNOWN in values:
            return UNKNOWN
        return tuple(values) if token.text == "(" else values
    if token.text in {"true", "false"}:
        return token.text == "true"
    if token.kind == "number":
        number = re.split(r"[ui]", token.text)[0]
        return int(number, 0) if number.startswith(("0x", "0o", "0b")) else int(number)
    if token.kind == "raw":
        prefix = RAW.match(token.text)
        return token.text[prefix.end():-1 - len(prefix[1])]
    if token.kind in {"quoted", "char"} and "\\" not in token.text:
        return token.text[token.text.index('"') + 1:-1] if token.kind == "quoted" else token.text[-2:-1]
    return UNKNOWN


def comparison(tokens):
    for index, token in enumerate(tokens):
        if token.text in {"==", "!=", ">", ">=", "<", "<="} and 0 < index < len(tokens) - 1:
            return token.text, tokens[:index], tokens[index + 1:]
    return "", (), ()


def always_true(tokens):
    alternatives = list(split(tokens, "||"))
    if len(alternatives) > 1:
        return any(map(always_true, alternatives))
    conjunction = list(split(tokens, "&&"))
    if len(conjunction) > 1:
        return all(map(always_true, conjunction))
    if len(tokens) == 1 and tokens[0].text == "(":
        return always_true(tokens[0].children)
    op, left, right = comparison(tokens)
    if op in {"==", "!="}:
        first, second = literal(left), literal(right)
        return UNKNOWN not in (first, second) and (first == second) == (op == "==")
    return literal(tokens) is True


def use_paths(tokens, prefix=()):
    path = prefix
    for index, token in enumerate(tokens):
        if token.text == "{":
            for alternative in split(token.children):
                yield from use_paths(alternative, path)
            return
        if token.text == "as" and index + 1 < len(tokens):
            yield path, tokens[index + 1].text
            return
        if token.kind == "word" and token.text != "self":
            path = (*path, token.text)
    if path:
        yield path, path[-1]


def imports_in(tokens):
    for index, token in enumerate(tokens):
        if token.text == "use":
            clause = next(iter(split(tokens[index + 1:], ";")), ())
            yield from use_paths(clause)


def test_functions(tokens, namespace=(), imports=None):
    aliases = dict(imports or ())
    aliases.update((alias, path) for path, alias in imports_in(tokens))
    attributes = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.text == "#" and index + 1 < len(tokens) and tokens[index + 1].text == "[":
            attributes.append((token, tokens[index + 1].children))
            index += 2
            continue
        if token.text == "fn" and index + 1 < len(tokens) and attributes:
            names = [shape(attribute) for _, attribute in attributes]
            is_test = any(name in {("test",), ("tokio", "::", "test")} for name in names)
            if is_test:
                for body_index in range(index + 2, len(tokens)):
                    body = tokens[body_index]
                    if body.text == ";":
                        break
                    if body.text == "{" and body.kind == "group":
                        panic = any(name and name[0] == "should_panic" for name in names)
                        name = tokens[index + 1].text
                        yield token, name, "::".join((*namespace, name)), attributes[0][0].line, body, panic, aliases
                        index = body_index
                        break
            attributes.clear()
        elif token.text not in {"pub", "async", "const", "unsafe", "extern"} and token.text != "(":
            attributes.clear()
        if token.text == "{" and token.kind == "group":
            module = tokens[index - 1].text if index >= 2 and tokens[index - 2].text == "mod" else None
            inside = (*namespace, module) if module else namespace
            yield from test_functions(token.children, inside, aliases)
        index += 1


FAILURES = {"panic", "unreachable", "todo", "unimplemented"}
RECORDING_METHODS = {"push", "extend", "insert"}


def macro_at(tokens, index, names):
    return (index + 2 < len(tokens) and tokens[index].text in names
            and tokens[index + 1].text == "!" and tokens[index + 2].kind == "group")


def directly_checks(tokens):
    return any(macro_at(tokens, index, ASSERTIONS | FAILURES) for index in range(len(tokens)))


def conditional_blocks(tokens):
    """Return the blocks whose direct checks can all be skipped."""
    guarded = set()
    for index, token in enumerate(tokens):
        if token.text not in {"if", "else", "match"}:
            continue
        position = next((i for i in range(index + 1, len(tokens)) if tokens[i].text in {"{", ";"}), None)
        if position is None or tokens[position].text != "{":
            continue
        block = tokens[position]
        if token.text == "match":
            arms = [next(iter(reversed(list(split(arm, "=>")))), ()) for arm in split(block.children)]
            exhaustive = bool(arms) and all(directly_checks(arm[0].children if
                len(arm) == 1 and arm[0].text == "{" else arm) for arm in arms)
            if not exhaustive:
                guarded.add(position)
        elif token.text == "if":
            following = tokens[position + 1:position + 3]
            both = (len(following) == 2 and following[0].text == "else"
                    and following[1].text == "{" and directly_checks(block.children)
                    and directly_checks(following[1].children))
            if not both:
                guarded.add(position)
                if len(following) == 2 and following[0].text == "else" and following[1].text == "{":
                    guarded.add(position + 2)
    return guarded


class Inspect:
    def __init__(self, aliases):
        self.aliases = aliases
        self.checks = []
        self.bindings = {}
        self.writes = {}
        self.reads = set()
        self.increments = set()
        self.assumes = False
        self.literal_issues = []

    def visit(self, tokens, guarded=False):
        guarded_blocks = conditional_blocks(tokens)
        declaration_names = set()
        for index, token in enumerate(tokens):
            text = token.text
            if text == "let":
                declaration = next(iter(split(tokens[index + 1:], ";")), ())
                parts = list(split(declaration, "="))
                if len(parts) == 2:
                    lhs, rhs = parts
                    target = next((item for item in lhs if item.kind == "word" and item.text != "mut"), None)
                    if target:
                        declaration_names.add(id(target))
                        self.bindings.setdefault(target.text, []).append((token.line, rhs))
            following = shape(tokens[index + 1:index + 3])
            if len(following) == 2 and following[0] == "." and following[1] in RECORDING_METHODS:
                self.writes.setdefault(text, token.line)
            elif token.kind == "word" and id(token) not in declaration_names:
                self.reads.add(text)
            if index + 1 < len(tokens) and tokens[index + 1].text == "+=":
                increment = next(iter(split(tokens[index + 2:], ";")), ())
                value = literal(increment)
                if type(value) is int and value > 0:
                    self.increments.add(text)
            if text == "prop_assume" and following[:1] == ("!",):
                self.assumes = True

            macro = index + 2 < len(tokens) and tokens[index + 1].text == "!"
            group_index = index + (2 if macro else 1)
            if token.kind == "word" and group_index < len(tokens):
                arguments = tokens[group_index]
                if arguments.kind == "group" and (macro or arguments.text == "("):
                    args = list(split(arguments.children))
                    if (macro and text in ASSERTIONS | FAILURES) or helper(text):
                        self.checks.append((token.line, text, args, guarded))
                    elif (not text[0].isupper() and not text.startswith(("assert_", "check_"))
                          and symmetric_input(text, map(literal, args))):
                        self.literal_issues.append((token.line, "nondistinguishing-literal"))
            if token.kind == "group":
                self.visit(token.children, guarded or index in guarded_blocks)

    def resolve(self, tokens, line):
        while len(tokens) == 1 and tokens[0].kind == "word":
            previous = [(pos, rhs) for pos, rhs in self.bindings.get(tokens[0].text, ()) if pos <= line]
            if len(previous) != 1 or previous[0][1] == tokens:
                break
            _, tokens = previous[0]
            line = min(line - 1, tokens[0].line) if tokens else line - 1
        return tokens

    def origin(self, tokens):
        if len(tokens) < 2 or tokens[-1].text != "(":
            return "", ""
        call = tokens[:-1]
        if not all(token.kind == "word" or token.text == "::" for token in call):
            return "", ""
        path = tuple(token.text for token in call if token.kind == "word")
        if not path:
            return "", ""
        path = (*self.aliases.get(path[0], path[:1]), *path[1:])
        if path[0] in {"std", "core", "alloc"} or any(part[:1].isupper() for part in path):
            return "", ""
        return "::".join(path), "::".join(path[:-1])

    def coupled(self, left, right, line):
        actual, expected = self.resolve(left, line), self.resolve(right, line)
        first, first_module = self.origin(actual)
        second, second_module = self.origin(expected)
        if not ((first and first == second) or (first_module and first_module == second_module)):
            return False
        if (shape(actual) == shape(expected) and shape(left) != shape(right)
                and any(word in first.rsplit("::", 1)[-1]
                        for word in ("read", "snapshot", "digest", "identity", "listing", "tree"))):
            return False
        return True

    def issues(self):
        findings = self.literal_issues.copy()
        recorders = {
            name for name, bindings in self.bindings.items()
            if shape(bindings[0][1]) in {("Vec", "::", "new", "("), ("vec", "!", "[")}
        }
        negative = {}
        positive = set()
        measured = False
        for line, name, args, _ in self.checks:
            if name in ASSERTIONS and name.endswith(("_eq", "_ne")) and len(args) >= 2:
                op, left, right = ("==" if name.endswith("_eq") else "!="), *args[:2]
                a, b = literal(left), literal(right)
                if UNKNOWN not in (a, b) and (a == b) == (op == "=="):
                    findings.append((line, "const-truth"))
            elif name in ASSERTIONS and args:
                op, left, right = comparison(args[0])
                if always_true(args[0]):
                    findings.append((line, "const-truth"))
                if len(args[0]) == 1 and args[0][0].text in recorders:
                    positive.add(args[0][0].text)
                if shape(args[0])[:1] == ("!",) and args[0][1:]:
                    expression = shape(args[0][1:])
                    if expression[0] in recorders and expression[1:] == (".", "is_empty", "("):
                        positive.add(expression[0])
                expression = shape(args[0])
                if expression and expression[0] in recorders and expression[1:] == (".", "is_empty", "("):
                    negative.setdefault(expression[0], []).append(line)
            else:
                continue
            if op in {"==", "!="} and shape(left) == shape(right):
                findings.append((line, "self-compare"))
            if op == "==" and self.coupled(left, right, line):
                findings.append((line, "self-derived-expected"))
            reverse = {">": "<", ">=": "<=", "<": ">", "<=": ">="}
            for operand, other, ordered in ((left, right, op), (right, left, reverse.get(op, op))):
                expression = shape(operand)
                if not expression:
                    continue
                value = literal(other)
                subject = expression[0]
                if (subject in self.increments and len(expression) == 1 and
                        ((ordered == ">" and value == 0) or
                         (ordered in {"==", ">="} and type(value) is int and value > 0))):
                    measured = True
                if subject not in recorders or expression[1:] not in {(), (".", "len", "(")}:
                    continue
                nonempty_vector = (len(other) == 3 and shape(other[:2]) == ("vec", "!")
                                   and bool(other[2].children))
                if ordered == "==" and value in (0, [], ()):
                    negative.setdefault(subject, []).append(line)
                elif ((ordered == "==" and ((value is not UNKNOWN and bool(value)) or nonempty_vector))
                      or (ordered in {"!=", ">"} and value in (0, [], ()))
                      or (ordered == ">=" and type(value) is int and value > 0)):
                    positive.add(subject)
        for name in recorders - positive:
            if name in negative:
                findings.extend((line, "absence-no-control") for line in negative[name])
            elif name in self.writes and name not in self.reads:
                findings.append((self.writes[name], "absence-no-control"))
        return findings, measured


def parse(source, path):
    tokens, comments = tokenize(source)
    tests = []
    for token, name, identifier, start, body, panic, aliases in test_functions(group(tokens)):
        previous = {c.line for c in comments if c.standalone and c.column == token.column}
        while start - 1 in previous:
            start -= 1
        test = Test(name, identifier, token.line, start, body.end, token.column)
        inspector = Inspect(aliases)
        inspector.visit(body.children)
        test.issues, test.measured = inspector.issues()
        if not inspector.checks and not panic:
            test.issues.append((token.line, "no-assert"))
        test.guarded = bool(inspector.checks) and not panic and all(
            guarded for _, _, _, guarded in inspector.checks)
        test.conditional = inspector.assumes
        tests.append(test)
    return tests, comments
