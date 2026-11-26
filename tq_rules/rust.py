from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import re

from .lint import (UNKNOWN, SuffixedNumber, assertion, different_callees, argument_claims, fixed_boundary, helper, loose_bounds,
                   named_arguments, prefix_satisfies, symmetric_input, unvaried_arguments)
from .source import Comment, Test


RAW = re.compile(r'(?:br|cr|r)(#*)"')
QUOTED = re.compile(r'(?:[bc])?"(?:[^"\\]|\\[\s\S])*"')
CHAR = re.compile(r"(?:b)?'(?:[^'\\\n]|\\(?:x[\da-fA-F]{2}|u\{[\da-fA-F_]+\}|[^\n]))'")
WORD = re.compile(r"(?:r#)?(?:[^\W\d]|_)\w*")
DECIMAL = r"[0-9][0-9_]*"
EXPONENT = rf"[eE][+-]?_*{DECIMAL}"
FLOAT = re.compile(rf"(?:{DECIMAL}(?:\.{DECIMAL}(?:{EXPONENT})?|{EXPONENT})(?:f32|f64)?"
                   rf"|{DECIMAL}(?:f32|f64|\.(?![.\w])))")
NUMBER = re.compile(r"(?:0[xob][0-9a-fA-F_]+|[0-9][0-9_]*)(?:[ui](?:8|16|32|64|128|size))?")
FLOAT_SUFFIX = re.compile(r"_?(f32|f64)$")
INTEGER_SUFFIX = re.compile(r"_?([ui](?:8|16|32|64|128|size))$")
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
                for kind, pattern in (("quoted", QUOTED), ("char", CHAR), ("float", FLOAT),
                                      ("number", NUMBER),
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


def split(tokens, delimiter=",", *, type_arguments=False):
    start = 0
    generic_depth = 0
    for index, token in enumerate(tokens):
        if token.text == "<" and (type_arguments or generic_depth or (index and tokens[index - 1].text == "::")):
            generic_depth += 1
        elif token.text == ">" and generic_depth:
            generic_depth -= 1
        elif not generic_depth and token.text == delimiter:
            yield tokens[start:index]
            start = index + 1
    if start < len(tokens):
        yield tokens[start:]


def literal(tokens, *, signed_and_float=False):
    signed = (signed_and_float and len(tokens) == 2 and tokens[0].text == "-"
              and tokens[1].kind in {"number", "float"})
    if signed:
        tokens = tokens[1:]
    if len(tokens) == 3 and shape(tokens[:2]) == ("vec", "!") and tokens[2].kind == "group":
        tokens = tokens[2:]
    if len(tokens) != 1:
        return UNKNOWN
    token = tokens[0]
    if token.kind == "group" and token.text in {"[", "("}:
        values = [literal(part, signed_and_float=signed_and_float) for part in split(token.children)]
        if UNKNOWN in values:
            return UNKNOWN
        return tuple(values) if token.text == "(" else values
    if token.text in {"true", "false"}:
        return token.text == "true"
    if signed_and_float and token.text == "None":
        return None
    if token.kind == "number" or (signed_and_float and token.kind == "float"):
        suffix = (FLOAT_SUFFIX if token.kind == "float" else INTEGER_SUFFIX).search(token.text)
        number = (token.text[:suffix.start()] if suffix else token.text).replace("_", "")
        if token.kind == "float":
            value = float(number)
        else:
            value = int(number, 0) if number.startswith(("0x", "0o", "0b")) else int(number)
        if signed:
            value = -value
        return SuffixedNumber(value, suffix[1]) if signed_and_float and suffix else value
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


def use_paths(tokens, prefix=(), *, keep_self=False):
    path = prefix
    for index, token in enumerate(tokens):
        if token.text == "{":
            for alternative in split(token.children):
                yield from use_paths(alternative, path, keep_self=keep_self)
            return
        if token.text == "as" and index + 1 < len(tokens):
            yield path, tokens[index + 1].text
            return
        if token.kind == "word" and (token.text != "self" or
                                     keep_self and not path and index + 1 < len(tokens)):
            path = (*path, token.text)
    if path:
        yield path, path[-1]


def imports_in(tokens, *, keep_self=False):
    for index, token in enumerate(tokens):
        if token.text == "use":
            clause = next(iter(split(tokens[index + 1:], ";")), ())
            yield from use_paths(clause, keep_self=keep_self)


def test_attribute(name):
    """`name` is shape(attribute): a path ending in `test`, or rstest/test_case, with optional arguments."""
    if name and (name[-1] == "(" or (isinstance(name[-1], tuple) and name[-1][0] == "(")):
        name = name[:-1]
    if not name or any(not isinstance(part, str) for part in name):
        return False
    if len(name) == 1:
        return name[0] in {"test", "rstest", "test_case"}
    return name[-1] == "test" and name[-2] == "::" and all(
        part == "::" if index % 2 else part.isidentifier() for index, part in enumerate(name[:-1]))


def test_functions(tokens, namespace=(), imports=None, *, module_scope=True):
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
            is_test = any(test_attribute(name) for name in names)
            if is_test:
                for body_index in range(index + 2, len(tokens)):
                    body = tokens[body_index]
                    if body.text == ";":
                        break
                    if body.text == "{" and body.kind == "group":
                        panic = any(name and name[0] == "should_panic" for name in names)
                        name = tokens[index + 1].text
                        scope = tokens if module_scope else None
                        yield token, name, "::".join((*namespace, name)), attributes[0][0].line, body, panic, aliases, scope
                        index = body_index
                        break
            attributes.clear()
        elif token.text not in {"pub", "async", "const", "unsafe", "extern"} and token.text != "(":
            attributes.clear()
        if token.text == "{" and token.kind == "group":
            module = tokens[index - 1].text if index >= 2 and tokens[index - 2].text == "mod" else None
            inside = (*namespace, module) if module else namespace
            yield from test_functions(token.children, inside, aliases, module_scope=module is not None)
        index += 1


FAILURES = {"panic", "unreachable", "todo", "unimplemented"}
RECORDING_METHODS = {"push", "extend", "insert"}


def macro_at(tokens, index, names):
    return (index + 2 < len(tokens) and tokens[index].text in names
            and tokens[index + 1].text == "!" and tokens[index + 2].kind == "group")


def directly_checks(tokens):
    return any(macro_at(tokens, index, ASSERTIONS | FAILURES) for index in range(len(tokens)))


def integer_literal(tokens):
    negative = len(tokens) == 2 and tokens[0].text == "-"
    digits = tokens[1:] if negative else tokens
    if len(digits) != 1 or digits[0].kind != "number":
        return None
    value = literal(digits)
    return -value if negative else value


def unborrow(tokens):
    if tokens and tokens[0].text == "&":
        start = 2 if len(tokens) > 1 and tokens[1].text == "mut" else 1
        return tokens[start:]
    return tokens


def literal_size(tokens, bound=integer_literal):
    tokens = unborrow(tokens)
    if len(tokens) == 1 and tokens[0].text == "(":
        return literal_size(tokens[0].children, bound)
    if len(tokens) == 1 and tokens[0].text == "[":
        elements = tokens[0].children
    elif len(tokens) == 3 and macro_at(tokens, 0, {"vec"}):
        elements = tokens[2].children
    else:
        return None
    parts = list(split(elements, ";"))
    if len(parts) == 2:
        return bound(parts[1])
    return len(list(split(elements)))


def method_call(tokens):
    if (len(tokens) >= 4 and tokens[-3].text == "." and tokens[-2].kind == "word"
            and tokens[-1].text == "("):
        return tokens[:-3], tokens[-2].text, tokens[-1].children
    return (), "", ()


def iterable_size(tokens, lookup):
    size = literal_size(tokens, lambda count: known_integer(count, lookup))
    if size is not None:
        return size
    tokens = unborrow(tokens)
    if len(tokens) == 1 and tokens[0].text == "(":
        return iterable_size(tokens[0].children, lookup)
    receiver, method, args = method_call(tokens)
    if method in {"iter", "iter_mut", "into_iter", "enumerate", "copied", "cloned", "rev"} and not args:
        return iterable_size(receiver, lookup)
    if method == "zip":
        arguments = list(split(args))
        if len(arguments) == 1:
            sizes = iterable_size(receiver, lookup), iterable_size(arguments[0], lookup)
            return min(sizes) if None not in sizes else None
    for index in range(len(tokens) - 1):
        if shape(tokens[index:index + 2]) == (".", "."):
            upper = tokens[index + 2:]
            inclusive = bool(upper) and upper[0].text == "="
            start = known_integer(tokens[:index], lookup)
            end = integer_bound(upper[1:] if inclusive else upper, lookup)
            return max(0, end - start + inclusive) if None not in (start, end) else None
    if tokens and tokens[-1].text == "(":
        return lookup(tokens[:-1], "helper")
    return lookup(tokens, "size")


def known_integer(tokens, lookup):
    number = integer_literal(tokens)
    return number if number is not None else lookup(tokens, "number")


def integer_bound(tokens, lookup):
    receiver, method, args = method_call(tokens)
    if method == "len" and not args:
        return iterable_size(receiver, lookup)
    return known_integer(tokens, lookup)


def loop_block(tokens, index):
    """Return the loop body index, pattern, and iterable or condition."""
    kind = tokens[index].text
    start = index + 1
    if kind == "for":
        for separator in range(start, len(tokens)):
            text = tokens[separator].text
            if text == "in":
                start = separator + 1
                break
            if text in {";", "<"} or (text == "{" and (
                    separator + 1 == len(tokens) or tokens[separator + 1].text != "in")):
                return None
        else:
            return None
    pending_blocks = 0
    for position in range(start, len(tokens)):
        text = tokens[position].text
        if text == ";":
            break
        if text in {"if", "match", "loop", "async", "unsafe", "const", "try"}:
            pending_blocks += 1
        elif text == "else" and position + 1 < len(tokens) and tokens[position + 1].text != "if":
            pending_blocks += 1
        elif text == "{" and position > start and tokens[position - 1].text not in {"!", "="}:
            if pending_blocks:
                pending_blocks -= 1
                continue
            return position, tokens[index + 1:start - 1], tokens[start:position]
    return None


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


def function_bodies(tokens):
    """Yield (name, body token) for functions declared directly in this scope."""
    for index, token in enumerate(tokens):
        if token.text == "fn" and index + 1 < len(tokens) and tokens[index + 1].kind == "word":
            for body in tokens[index + 2:]:
                if body.text == ";":
                    break
                if body.text == "{" and body.kind == "group":
                    yield tokens[index + 1].text, body
                    break


def invocation_at(tokens, index):
    if tokens[index].kind != "word" or index + 1 >= len(tokens):
        return None
    following = tokens[index + 1]
    if following.kind == "group" and following.text == "(":
        return following, False
    if following.text == "!" and index + 2 < len(tokens) and tokens[index + 2].kind == "group":
        return tokens[index + 2], True
    return None


def callee_start(tokens, index):
    while index >= 2 and tokens[index - 1].text in {".", "::"} and tokens[index - 2].kind == "word":
        index -= 2
    if index and tokens[index - 1].text == "::":
        return index - 1
    if index and tokens[index - 1].text in {".", "fn"}:
        return None
    return index


def check_arguments(tokens):
    for index, token in enumerate(tokens):
        if token.kind == "group":
            yield token
            yield from check_arguments(token.children)
        elif (token.kind == "word" and (not index or tokens[index - 1].text not in {".", "::"})
              and (index + 1 == len(tokens) or tokens[index + 1].text not in {"::", ":"})):
            yield token


def starts_check(tokens, index, macro, known, *, in_helper=False):
    name = tokens[index].text
    if in_helper:
        if name.startswith("debug_assert") or (macro and name in FAILURES):
            return False
        if not macro and name.startswith("expect") and index and tokens[index - 1].text == ".":
            return False
    if macro:
        return name in ASSERTIONS or name in FAILURES or helper(name)
    return helper(name) or name in known


def checks_anywhere(tokens, known):
    for index, token in enumerate(tokens):
        invocation = invocation_at(tokens, index)
        if invocation and starts_check(tokens, index, invocation[1], known, in_helper=True):
            return True
        if token.kind == "group" and checks_anywhere(token.children, known):
            return True
    return False


def checking_helpers(scope, test_bodies):
    """Find non-test functions in this scope that check directly or call a known checker."""
    bodies = [(name, body) for name, body in function_bodies(scope) if id(body) not in test_bodies]
    known = set()
    while True:
        found = {name for name, body in bodies if name not in known
                 and checks_anywhere(body.children, known)}
        if not found:
            return known
        known |= found


def qualified_name(tokens):
    if (tokens and len(tokens) % 2
            and all(token.kind == "word" for token in tokens[::2])
            and all(token.text == "::" for token in tokens[1::2])):
        return tuple(token.text for token in tokens[::2])
    return ()


def item_value(tokens, index):
    declaration = next(iter(split(tokens[index:], ";")), ())
    parts = list(split(declaration, "="))
    return parts[1] if len(parts) == 2 else ()


def absolute_path(scope, name):
    if name[:1] == ("crate",):
        return name[1:]
    while name[:1] == ("super",):
        scope, name = scope[:-1], name[1:]
    return (*scope, *(name[1:] if name[:1] == ("self",) else name))


def parameter_names(tokens):
    for index, token in enumerate(tokens[:-2]):
        if token.text != "fn" or tokens[index + 1].kind != "word":
            continue
        depth = 0
        for part in tokens[index + 2:]:
            if part.text == "<":
                depth += 1
            elif part.text == ">":
                depth -= 1
            elif depth == 0 and part.text == "(":
                names = {}
                for position, arg in enumerate(split(part.children, type_arguments=True)):
                    pattern = arg[1:] if shape(arg[:1]) == ("mut",) else arg
                    if len(pattern) >= 2 and pattern[0].kind == "word" and pattern[1].text == ":":
                        names[position] = pattern[0].text.removeprefix("r#")
                yield tokens[index + 1].text, names
                break
            elif depth == 0 and part.text in {"{", ";", "="}:
                break


def file_parameters(tokens, scope=()):
    for name, names in parameter_names(tokens):
        yield (*scope, name), names
    for index, token in enumerate(tokens):
        if token.text != "{" or index < 2:
            continue
        kind, name = tokens[index - 2:index]
        if name.kind == "word" and kind.text in {"mod", "impl"}:
            yield from file_parameters(token.children, (*scope, name.text))


def constructor(callee):
    if callee[-1][:1].isupper():
        return True
    return len(callee) >= 3 and callee[-2:] == ("::", "new") and callee[-3][:1].isupper()


def expected_constructors(tokens, *, expected=False):
    right_side = False
    for index, token in enumerate(tokens):
        if token.text == "==":
            right_side = True
        elif token.text in {",", "&&", "||", ";"}:
            right_side = False
        elif token.kind == "group":
            in_expected = expected or right_side
            if in_expected and token.text == "(" and index and tokens[index - 1].kind == "word":
                start = callee_start(tokens, index - 1)
                if start is not None and constructor(shape(tokens[start:index])):
                    yield id(token)
            yield from expected_constructors(token.children, expected=in_expected)


def test_only(tokens, index):
    while index:
        previous = tokens[index - 1]
        if previous.text in {"pub", "async", "unsafe", "const"} or (
                previous.text == "(" and index >= 2 and tokens[index - 2].text == "pub"):
            index -= 1
        elif previous.text == "[" and index >= 2 and tokens[index - 2].text == "#":
            attribute = shape(previous.children)
            if test_attribute(attribute) or attribute == ("cfg", ("(", ("test",))):
                return True
            index -= 2
        else:
            break
    return False


def test_module(name):
    return (name in {"test", "tests"} or name.startswith("test_")
            or name.endswith(("_test", "_tests")))


class FileIterables:
    def __init__(self, tokens, path, parameters):
        self.definitions = {}
        self.path = path
        self.aliases = {}
        self.globs = defaultdict(list)
        self.test_items = set()
        self.test_file = test_module(path.stem) or any(map(test_module, path.parent.parts))
        self.parameters = parameters
        self.index(tokens, ())

    def add(self, name, kind, value):
        self.definitions[name] = ("", ()) if name in self.definitions else (kind, value)

    def index(self, tokens, scope):
        self.aliases[scope] = {alias: path for path, alias in imports_in(tokens, keep_self=True)}
        for index, token in enumerate(tokens):
            if token.text in {"const", "static"}:
                start = index + 1
                if start < len(tokens) and tokens[start].text == "mut":
                    continue
                head = tokens[start:start + 2]
                if len(head) == 2 and head[0].kind == "word" and head[1].text == ":":
                    self.add((*scope, head[0].text), "constant", item_value(tokens, start))
            elif token.text == "use":
                path = next(iter(split(tokens[index + 1:], ";")), ())
                if shape(path[-2:]) == ("::", "*"):
                    name = qualified_name(path[:-2])
                    if name:
                        self.globs[scope].append(absolute_path(scope, name))
            elif token.text in {"mod", "impl"}:
                head = tokens[index + 1:index + 3]
                if len(head) == 2 and head[0].kind == "word":
                    child = (*scope, head[0].text)
                    if self.test_file or test_module(head[0].text) or test_only(tokens, index):
                        self.test_items.add(child)
                    if head[1].text == "{":
                        self.index(head[1].children, child)
            elif token.text == "fn" and index + 1 < len(tokens) and test_only(tokens, index):
                self.test_items.add((*scope, tokens[index + 1].text))
        for name, body in function_bodies(tokens):
            self.add((*scope, name), "helper", return_expression(body))

    def resolve(self, scope, name):
        direct = absolute_path(scope, name)
        if direct in self.definitions or name[0] in {"super", "self", "crate"}:
            return direct
        aliases = self.aliases.get(scope, {})
        if name[0] in aliases:
            imported = (*aliases[name[0]], *name[1:])
            if imported[0] in {"super", "self", "crate"}:
                return absolute_path(scope, imported)
            return imported
        matches = [(*path, *name) for path in self.globs.get(scope, ())
                   if (*path, *name) in self.definitions]
        return matches[0] if len(matches) == 1 else ()

    def production(self, path):
        return (not (self.test_file and path in self.definitions)
                and all(path[:depth] not in self.test_items for depth in range(1, len(path) + 1)))

    def shares_constant(self, callee, expected):
        if (not expected or expected[:-1] != callee[:-1]
                or self.definitions.get(expected, ("", ()))[0] != "constant"
                or not self.production(expected)):
            return False
        kind, body = self.definitions.get(callee, ("", ()))
        if kind != "helper":
            return False
        scope = callee[:-1]
        local = {token.text for token in declared_bindings(body)}
        local.update(self.parameters.get(callee, {}).values())
        return any((len(name) > 1 or name[0] not in local)
                   and (scope + name[1:] if name[0] == "Self" else self.resolve(scope, name)) == expected
                   for name in constant_references(body))

    def function(self, path, *, external=False):
        kind, body = self.definitions.get(path, ("", ()))
        if kind == "helper" or not external or not path:
            return body
        for root in self.path.parents:
            if (root / "lib.rs").is_file() or (root / "main.rs").is_file():
                module = root.joinpath(*path[:-1])
                if path[:-1]:
                    candidates = (module.with_suffix(".rs"), module / "mod.rs")
                else:
                    candidates = (root / "lib.rs", root / "main.rs")
                files = [candidate for candidate in candidates if candidate.is_file()]
                if len(files) == 1:
                    return module_functions(files[0]).get(path[-1], ())
                break
        return ()

    def lookup(self, scope, name, kind):
        if not name:
            return None
        path = self.resolve(scope, name)
        source, expression = self.definitions.get(path, ("", ()))
        if source == "constant" and kind == "number":
            return integer_literal(expression)
        if (source, kind) in {("constant", "size"), ("helper", "helper")}:
            return literal_size(expression, lambda count: known_integer(
                count, lambda tokens, kind: self.lookup(path[:-1], qualified_name(tokens), kind)))
        return None


@lru_cache(maxsize=64)
def module_functions(path):
    try:
        tokens, _ = tokenize(path.read_text(encoding="utf-8"))
        return {name: return_expression(body) for name, body in function_bodies(group(tokens))}
    except (OSError, ValueError):
        return {}


def literal_returns(tokens):
    tail_start = max((index + 1 for index, token in enumerate(tokens)
                      if token.text in {";", "{"}), default=0)
    value = number_value(tokens[tail_start:])
    if value is not None:
        yield value
    yield from explicit_literal_returns(tokens)
    tail = tuple(split(tokens, ";"))[-1] if tokens and tokens[-1].text == "{" else ()
    if tail and tail[0].text == "if":
        for index, token in enumerate(tail):
            if token.text == "{" and (index == len(tail) - 1 or tail[index + 1].text == "else"):
                yield from literal_returns(token.children)


def explicit_literal_returns(tokens):
    nested_functions = {id(body) for _, body in function_bodies(tokens)}
    in_closure = False
    for index, token in enumerate(tokens):
        if token.text == ";":
            in_closure = False
        elif token.text == "|":
            in_closure = True
        elif token.text == "return" and not in_closure:
            value = number_value(next(iter(split(tokens[index + 1:], ";")), ()))
            if value is not None:
                yield value
        elif (token.text == "{" and not in_closure and id(token) not in nested_functions
              and (not index or tokens[index - 1].text != "!")):
            yield from explicit_literal_returns(token.children)


def number_value(tokens):
    value = literal(tokens, signed_and_float=True)
    if isinstance(value, SuffixedNumber):
        return value.value
    return value if type(value) in {int, float} else None


def output_fragments(tokens):
    tokens = unborrow(tokens)
    if len(tokens) == 1:
        value = literal(tokens)
        if isinstance(value, str):
            return [value]
        if tokens[0].kind == "quoted" and tokens[0].text.startswith('"'):
            content = tokens[0].text[1:-1]
            escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
            if all(escape in escapes for escape in re.findall(r"\\(.)", content)):
                return [re.sub(r"\\(.)", lambda match: escapes[match[1]], content)]
        return []
    if len(tokens) != 3 or not macro_at(tokens, 0, {"format"}):
        return []
    args = list(split(tokens[-1].children))
    if not args or len(args[0]) != 1 or args[0][0].kind not in {"quoted", "raw"}:
        return []
    template = args[0][0].text
    fragments = [part for text in output_fragments(args[0])
                 for part in re.split(r"\{[^{}]*\}", text) if part]
    fields = re.findall(r"\{[^{}]*\}", template)
    if (len(fields) == len(args) - 1 and "{{" not in template and "}}" not in template
            and all(re.fullmatch(r"\{(?::[<>^]?\d+)?\}", field) for field in fields)):
        fragments.extend(text for arg in args[1:] if isinstance(text := literal(arg), str))
    return fragments


def output_parts(tokens):
    if not tokens or tokens[-1].kind != "word":
        return (), ()
    name = tokens[-1].text
    statements = list(split(tokens[:-1], ";"))
    if not statements or shape(statements[0]) != ("let", "mut", name, "=", "String", "::", "new", "("):
        return (), ()
    prefix, dynamic = [], []

    def appends(parts):
        for index, token in enumerate(parts):
            if token.text == "return":
                return False
            if token.text == name:
                call = parts[index:index + 4]
                if shape(call[:3]) != (name, ".", "push_str") or len(call) != 4 or call[-1].text != "(":
                    return False
                dynamic.extend(output_fragments(call[-1].children))
            if token.kind == "group" and not appends(token.children):
                return False
        return True

    end = 1
    for statement in statements[1:]:
        if shape(statement[:3]) != (name, ".", "push_str") or len(statement) != 4 or statement[3].text != "(":
            break
        prefix.extend(output_fragments(statement[3].children))
        end += 1
    if not all(appends(statement) for statement in statements[end:]):
        return (), ()
    return prefix, dynamic


def return_expression(body):
    value = body.children
    if shape(value[:1]) == ("return",) and shape(value[-1:]) == (";",):
        return value[1:-1]
    return value


SHRINKING_METHODS = {"clear", "dedup_by", "drain", "extract_if", "pop", "pop_back",
                     "pop_front", "remove", "retain", "retain_mut", "split_off", "swap_remove",
                     "truncate"}


def mutated_names(tokens):
    for index, token in enumerate(tokens):
        if token.kind == "group":
            yield from mutated_names(token.children)
        elif token.kind == "word":
            following = tuple(part.text for part in tokens[index + 1:index + 3])
            previous = tuple(part.text for part in tokens[max(0, index - 3):index])
            declaration = previous[-1:] in {("let",), ("const",)} or previous[-2:] == ("let", "mut")
            assignment = (following[:1] in {("=",), ("+=",)}
                          or len(following) == 2 and following[0] in "-*/%&|^" and following[1] == "=")
            shrinks = len(following) == 2 and following[0] == "." and following[1] in SHRINKING_METHODS
            mutably_borrowed = previous[-2:] == ("&", "mut") and previous != ("in", "&", "mut")
            if (assignment and not declaration) or shrinks or mutably_borrowed:
                yield token.text


def pattern_names(tokens):
    return (token.text for token in binding_tokens(tokens))


ORACLE_METHODS = {"to_json", "tobytes", "to_string", "clone", "iter", "into_iter", "collect",
                  "read_bytes", "read_to_string", "as_str", "as_slice", "as_ref", "as_bytes",
                  "unwrap", "expect"}
ORACLE_OPERATORS = {"+", "-", "*", "/", "%", "|", "^"}
ORACLE_FUNCTIONS = {("std", "fs", "read"), ("std", "fs", "read_to_string"),
                    ("serde_json", "to_string"), ("serde_json", "to_vec"), ("serde_json", "to_value")}


def binding_tokens(tokens):
    for token in tokens:
        if token.kind == "group":
            yield from binding_tokens(token.children)
        elif token.kind == "word" and token.text not in {"mut", "ref", "_"}:
            yield token


def declared_bindings(tokens):
    for index, token in enumerate(tokens):
        if token.text in {"let", "const", "static"}:
            end = next((i for i in range(index + 1, len(tokens))
                        if tokens[i].text in {"=", ":", ";"}), len(tokens))
            yield from binding_tokens(tokens[index + 1:end])
        elif token.kind == "group":
            yield from declared_bindings(token.children)


def constant_references(tokens):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "group":
            yield from constant_references(token.children)
        elif token.kind == "word" and (not index or tokens[index - 1].text != "."):
            end = index + 1
            while (end + 1 < len(tokens) and tokens[end].text == "::"
                   and tokens[end + 1].kind == "word"):
                end += 2
            path = qualified_name(tokens[index:end])
            if path[-1].isupper():
                yield path
            index = end
            continue
        index += 1


class Inspect:
    def __init__(self, aliases, helpers, file_iterables, scope, mutated, parameters, local_parameters):
        self.aliases = aliases
        self.helpers = helpers
        self.file_iterables = file_iterables
        self.scope = scope
        self.mutated = mutated
        self.checks = []
        self.bindings = {}
        self.writes = {}
        self.reads = set()
        self.increments = set()
        self.assumes = False
        self.symmetric_calls = defaultdict(list)
        self.parameters = parameters
        self.local_parameters = local_parameters
        self.oracle_locals = {}
        self.expected = set()
        self.calls = defaultdict(list)
        self.check_arguments = []

    def visit(self, tokens, guarded=False, inherited=None):
        locals_here = dict(inherited or ())
        guarded_blocks = conditional_blocks(tokens)
        block_locals = {}

        def lookup(expression, kind):
            name = qualified_name(expression)
            if name and name[0] in locals_here:
                return locals_here[name[0]].get(kind) if len(name) == 1 else None
            return self.file_iterables.lookup(self.scope, name, kind)

        declaration_names = set()
        for index, token in enumerate(tokens):
            text = token.text
            if text in {"for", "while"}:
                loop = loop_block(tokens, index)
                if loop is not None:
                    position, pattern, header = loop
                    block_locals[position] = tuple(pattern_names(pattern))
                    if text == "for":
                        runs = (iterable_size(header, lookup) or 0) > 0
                    else:
                        runs = shape(header) == ("true",)
                    if not runs:
                        guarded_blocks.add(position)
            if text in {"let", "const"}:
                declaration = next(iter(split(tokens[index + 1:], ";")), ())
                parts = list(split(declaration, "="))
                pattern = next(iter(split(parts[0], ":")), ()) if parts else ()
                names = tuple(pattern_names(pattern))
                if text == "const" and (not pattern or pattern[0].text == "fn"):
                    names = ()
                previous = tokens[index - 1].text if index else ""
                if previous in {"if", "while"}:
                    position = next((i for i in range(index + 1, len(tokens))
                                     if tokens[i].text == "{"), None)
                    if position is not None:
                        block_locals[position] = names
                else:
                    locals_here.update((name, {}) for name in names)
                    bare = pattern[1:] if shape(pattern[:1]) == ("mut",) else pattern
                    if len(parts) == 2 and len(bare) == 1 and names:
                        name = names[0]
                        if name not in self.mutated:
                            value = parts[1]
                            locals_here[name] = {
                                "size": literal_size(value, lambda count: known_integer(count, lookup)),
                                "number": integer_literal(value),
                            }
            if text == "let":
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

            invocation = invocation_at(tokens, index)
            if invocation:
                arguments, macro = invocation
                args = list(split(arguments.children))
                if assertion(text):
                    self.check_arguments.append(arguments)
                    if text in ASSERTIONS and text.endswith("_eq") and len(args) >= 2:
                        self.expected.update(expected_constructors(args[1], expected=True))
                    elif text in {"assert", "debug_assert", "prop_assert"} and args:
                        self.expected.update(expected_constructors(args[0]))
                checking = starts_check(tokens, index, macro, self.helpers)
                if checking:
                    self.checks.append((token.line, text, args, guarded))

                start = callee_start(tokens, index)
                callee = shape(tokens[start:index + 1]) if start is not None else None
                literals = {position: literal(arg, signed_and_float=True)
                            for position, arg in enumerate(args)}
                if callee and not (macro or assertion(text)
                                   or text in {"if", "while", "for", "match", "return", "break", "move"}):
                    first = tokens[start]
                    self.calls[callee].append(((first.line, first.column), arguments, literals))

                if not checking and not text[0].isupper() and not (macro and text == "vec"):
                    key = (*callee, "!") if callee and macro else callee
                    legacy = dict(enumerate(map(literal, args)))
                    shapes = {position: shape(arg) for position, arg in enumerate(args)}
                    self.symmetric_calls[key].append((text, token.line, arguments, legacy, literals, shapes))
            if token.kind == "group":
                in_block = locals_here
                if index in block_locals:
                    in_block = {**locals_here, **dict.fromkeys(block_locals[index], {})}
                self.visit(token.children, guarded or index in guarded_blocks, in_block)

    def resolve(self, tokens, line):
        while len(tokens) == 1 and tokens[0].kind == "word":
            previous = [(pos, rhs) for pos, rhs in self.bindings.get(tokens[0].text, ()) if pos <= line]
            if len(previous) != 1 or previous[0][1] == tokens:
                break
            _, tokens = previous[0]
            line = min(line - 1, tokens[0].line) if tokens else line - 1
        return tokens

    def oracle_path(self, name, line):
        if (not name or name[0] in self.local_parameters
                or self.oracle_locals.get(name[0], line + 1) <= line):
            return ()
        found = self.file_iterables.resolve(self.scope, name)
        if found:
            return found
        return (*self.aliases.get(name[0], name[:1]), *name[1:])

    def oracle_calls(self, tokens, line, seen=frozenset()):
        while tokens and tokens[0].text in {"&", "mut", "*"}:
            tokens = tokens[1:]
        if not tokens or id(tokens[0]) in seen:
            return None
        if len(tokens) == 1 and tokens[0].kind == "word":
            seen = seen | {id(tokens[0])}
        tokens = self.resolve(tokens, line)
        if not tokens:
            return None
        line = tokens[0].line
        if len(tokens) == 1 and tokens[0].text == "(":
            return self.oracle_calls(tokens[0].children, line, seen)
        # Expression groups already hide operators inside call arguments and indexes.
        for index in range(1, len(tokens) - 1):
            if tokens[index].text in ORACLE_OPERATORS:
                left = self.oracle_calls(tokens[:index], line, seen)
                right = self.oracle_calls(tokens[index + 1:], line, seen)
                return left | right if left is not None and right is not None else None
        if tokens[-1].text == "[" or (len(tokens) >= 3 and tokens[-2].text == "."):
            return self.oracle_calls(tokens[:-1] if tokens[-1].text == "[" else tokens[:-2], line, seen)
        if tokens[-1].text == "(":
            member = next((i for i in range(len(tokens) - 3, 0, -1) if tokens[i].text == "."), 0)
            if member and tokens[member + 1].text in ORACLE_METHODS:
                return self.oracle_calls(tokens[:member], line, seen)
            name = qualified_name(tokens[:-1])
            path = self.oracle_path(name, line)
            if path in ORACLE_FUNCTIONS:
                first = next(iter(split(tokens[-1].children)), ())
                return self.oracle_calls(first, line, seen)
            if (path and path[0] not in {"std", "core", "alloc"}
                    and not any(map(test_module, path[:-1]))
                    and self.file_iterables.production(path)
                    and (self.file_iterables.definitions.get(path, ("", ()))[0] == "helper"
                         or (len(path) >= 2 and all(not part[:1].isupper() for part in path)))):
                return {path}
        if literal(tokens, signed_and_float=True) is not UNKNOWN or self.oracle_constant(tokens, line):
            return set()
        return None

    def oracle_constant(self, tokens, line):
        tokens = self.resolve(tokens, line)
        if tokens and tokens[-1].text == "[":
            tokens = tokens[:-1]
        path = self.oracle_path(qualified_name(tokens), tokens[0].line) if tokens else ()
        return path if path and self.file_iterables.definitions.get(path, ("", ()))[0] == "constant" else ()

    def uses_operand(self, value, other, line):
        if (not other or other[0].kind != "word"
                or len(other) > 1 and other[1].text not in {"[", "."}):
            return False
        value = self.resolve(value, line)
        if len(value) > 1 and value[0].kind == "word" and value[1].text in {"[", "."}:
            value = self.resolve(value[:1], value[0].line)
        return any(token.text == other[0].text
                   for part in value if part.text == "("
                   for token in check_arguments(part.children) if token.kind == "word")

    def coupled(self, left, right, line):
        actual = self.oracle_calls(left, line)
        expected = self.oracle_calls(right, line)
        return (different_callees(actual, expected)
                and not (self.uses_operand(left, right, line) or self.uses_operand(right, left, line))
                or (actual and len(actual) == 1 and self.file_iterables.shares_constant(
                    next(iter(actual)), self.oracle_constant(right, line)))
                or (expected and len(expected) == 1 and self.file_iterables.shares_constant(
                    next(iter(expected)), self.oracle_constant(left, line))))

    def observed_calls(self, tokens, line, seen=frozenset()):
        while tokens and tokens[0].text in {"!", "&", "mut", "*"}:
            tokens = tokens[1:]
        if not tokens:
            return
        head = tokens[0]
        if id(head) in seen:
            return
        seen = seen | {id(head)}
        if head.kind == "group" and head.text == "(":
            yield from self.observed_calls(head.children, line, seen)
            return
        if head.text in self.bindings:
            value = self.resolve((head,), line)
            if value and value != (head,):
                yield from self.observed_calls(value, min(line, value[0].line - 1), seen)
        for index, token in enumerate(tokens):
            invocation = invocation_at(tokens, index)
            if invocation:
                arguments, macro = invocation
                if macro:
                    return
                start = callee_start(tokens, index)
                if start is not None and (start == 0 or tokens[start - 1].text != "::"):
                    yield arguments
            elif token.kind != "word" and token.text not in {".", "::", "("}:
                if token.text != "[":
                    return

    def fixed_output_check(self, tokens, line):
        alternatives = list(split(tokens, "||"))
        if len(alternatives) < 2:
            return False
        receivers, needles = [], []
        for part in alternatives:
            subject, name, args = method_call(part)
            needle = literal(args)
            if name != "contains" or type(needle) is not str or not needle:
                return False
            receivers.append(self.resolve(subject, line))
            needles.append(needle)
        value = receivers[0]
        if not value or value[-1].text != "(" or not all(shape(value) == shape(other) for other in receivers):
            return False
        callee = qualified_name(value[:-1])
        path = self.oracle_path(callee, line)
        prefix, body = output_parts(self.file_iterables.function(path, external=callee[:1] == ("crate",)))
        return prefix_satisfies(needles, prefix, body)

    def order_without_cardinality(self, line, name, args):
        if name.endswith("_eq") and len(args) >= 2:
            subject = ()
            for original, sorted_copy in (args[:2], args[:2][::-1]):
                original = unborrow(original)
                sorted_copy = unborrow(sorted_copy)
                if original and original[0].text == "*":
                    original = original[1:]
                if len(sorted_copy) != 1:
                    continue
                source, method, _ = method_call(self.resolve(sorted_copy, line))
                if method == "clone" and shape(original) == shape(source):
                    sort_calls = self.calls.get((sorted_copy[0].text, ".", "sort"), ())
                    if any(sorted_copy[0].line >= loc[0] > source[0].line for loc, _, _ in sort_calls):
                        subject = source
                        break
        elif name in {"assert", "debug_assert", "prop_assert"}:
            subject, method, parts = method_call(self.resolve(args[0], line))
            if method == "all":
                subject, method, width = method_call(subject)
                if method != "windows" or integer_literal(width) != 2 or not parts:
                    return False
            elif method != "is_sorted":
                return False
        else:
            return False
        if not subject or subject[0].kind != "word" or subject[0].text in self.mutated:
            return False
        if len(subject) != 1 and not all(
                token.text == "." if index % 2 else token.kind == "word"
                for index, token in enumerate(subject)):
            return False
        subjects = {subject[0].text}
        if len(subject) == 1:
            value = self.resolve(subject, line)
            if shape(value) in {shape(subject), ("Vec", "::", "new", "("), ("vec", "!", "[")}:
                return False
            if len(value) > 4 and value[-2].text == "clone":
                return False
            if self.fixed_projection(value, line):
                return False
            while source := self.projection_source(value):
                if source in subjects:
                    break
                subjects.add(source)
                value = self.resolve(value[:1], line)
            for alias, bindings in self.bindings.items():
                if len(bindings) == 1 and self.projection_source(bindings[0][1]) in subjects:
                    subjects.add(alias)
        for other_line, other_name, other_args, _ in self.checks:
            if other_line == line or not other_args:
                continue
            if (other_name.endswith("_eq") and len(other_args) >= 2
                    and all(method_call(arg)[1] == "len" for arg in other_args[:2])):
                continue
            if any(token.text in subjects for argument in other_args[:2]
                   for token in check_arguments(argument) if token.kind == "word"):
                return False
        return True

    @staticmethod
    def projection_source(tokens):
        if (len(tokens) > 7 and tokens[0].kind == "word"
                and shape(tokens[1:4]) in {(".", "iter", "("), (".", "into_iter", "(")}
                and {token.text for index, token in enumerate(tokens) if index and tokens[index - 1].text == "."}
                == {tokens[2].text, "map", "collect"}):
            return tokens[0].text
        return None

    def fixed_projection(self, tokens, line):
        if tokens and tokens[-1].text == "(":
            path = self.oracle_path(qualified_name(tokens[:-1]), line)
            body = self.file_iterables.function(path)
            if body:
                tokens = tuple(split(body, ";"))[-1]
        return (len(tokens) > 5 and tokens[0].text == "("
                and tuple(token.text for token in tokens[1:6]) == (".", "map", "(", ".", "collect")
                and any(shape(tokens[0].children[index:index + 2]) == (".", ".")
                        for index in range(len(tokens[0].children) - 1)))

    def file_measurement(self, callee, line):
        if len(callee) != 1:
            return False
        body = self.resolve(callee, line)
        if not body or body[0].text != "|":
            return False
        stack = [body]
        while stack:
            tokens = stack.pop()
            if any(shape(tokens[index:index + 5]) in {
                    ("std", "::", "fs", "::", "read_dir"),
                    ("std", "::", "fs", "::", "read_to_string"),
                    } for index in range(len(tokens) - 4)):
                return True
            stack.extend(token.children for token in tokens if token.children)
        return False

    def argument_calls(self, test_name):
        checked, observed, direct, fixtures = set(), set(), set(), set()
        bound_checks = []
        claims = argument_claims(test_name)
        for line, name, args, _ in self.checks:
            if name not in ASSERTIONS or not args:
                continue
            boolean = name in {"assert", "debug_assert", "prop_assert"}
            if (boolean and self.fixed_output_check(args[0], line)
                    or self.order_without_cardinality(line, name, args)):
                yield line
            if name.endswith(("_eq", "_ne")) and len(args) >= 2:
                predicates = [("==" if name.endswith("_eq") else "!=", *args[:2])]
            else:
                predicates = []
                for disjunction in split(args[0], "||"):
                    for clause in split(disjunction, "&&"):
                        op, left, right = comparison(clause)
                        predicates.append((op, left if op else clause, right))
            for op, left, right in predicates:
                for operand, other in ((left, right), (right, left)):
                    for result in self.observed_calls(operand, line):
                        observed.add(id(result))
                        for arg in split(result.children):
                            fixtures.update(id(fixture) for fixture in self.observed_calls(arg, line))
                    value = self.resolve(operand, line)
                    if len(value) > 1 and value[-1].text == "(" and shape(value[:-1]) in self.calls:
                        first, arguments = value[0], value[-1]
                        direct.add(id(arguments))
                        inputs = {position: literal(arg, signed_and_float=True)
                                  for position, arg in enumerate(split(arguments.children))}
                        threshold = number_value(other)
                        bounded = op in {">", ">=", "<", "<="} and threshold is not None
                        path = self.oracle_path(qualified_name(value[:-1]), line) if bounded else ()
                        exempt = bounded and (self.file_measurement(value[:-1], line)
                                              or threshold in literal_returns(self.file_iterables.function(path)))
                        bound_checks.append((shape(value[:-1]), (first.line, first.column), inputs, bounded, exempt))

        for arguments in self.check_arguments:
            for token in check_arguments(arguments.children):
                if token.kind == "group":
                    checked.add(id(token))
                elif token.text in self.bindings:
                    value = self.resolve((token,), token.line)
                    if len(value) > 1 and value[-1].text == "(" and shape(value[:-1]) in self.calls:
                        checked.add(id(value[-1]))

        unvaried, coverage = [], []
        for callee, calls in self.calls.items():
            ignored = {position for position, name in self.parameters_for(callee).items() if name.startswith("_")}
            nested = []
            for location, arguments, literals in calls:
                if id(arguments) not in self.expected:
                    inputs = {position: value for position, value in literals.items() if position not in ignored}
                    unvaried.append((callee, location, inputs, id(arguments) in observed))
                    if any(claims):
                        coverage.append((callee, location, inputs, id(arguments) in observed, id(arguments) in fixtures))
                    if id(arguments) in fixtures and id(arguments) not in observed:
                        nested.append((location, inputs))
            if fixed_boundary([inputs for _, inputs in nested]):
                yield min(location for location, _ in nested)[0]
        for group in self.symmetric_calls.values():
            peers = [(shapes, values) for _, _, args, _, values, shapes in group
                     if id(args) in checked and id(args) not in self.expected]
            for name, line, args, legacy, _, shapes in group:
                if symmetric_input(name, legacy, shapes, peers, observed=id(args) in direct):
                    yield line
        yield from unvaried_arguments(unvaried)
        yield from named_arguments(coverage, claims, self.parameters_for)
        yield from loose_bounds(bound_checks)

    def parameters_for(self, callee):
        if "." in callee:
            return {}
        name = tuple(part for part in callee if part != "::")
        if len(name) == 1 and name[0] in self.local_parameters:
            return self.local_parameters[name[0]]
        if name[0] in self.aliases:
            name = (*self.aliases[name[0]], *name[1:])
        return self.parameters.get(absolute_path(self.scope, name),
                                   self.parameters.get(name, {}))

    def issues(self, test_name):
        findings = [(line, "nondistinguishing-literal") for line in self.argument_calls(test_name)]
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
    grouped = group(tokens)
    found = list(test_functions(grouped))
    if not found:
        return tests, comments
    test_bodies = {id(body) for _, _, _, _, body, _, _, _ in found}
    helpers = {}
    parameters = dict(file_parameters(grouped))
    file_iterables = FileIterables(grouped, path, parameters)
    for token, name, identifier, start, body, panic, aliases, scope in found:
        if scope is not None and id(scope) not in helpers:
            helpers[id(scope)] = checking_helpers(scope, test_bodies)
        previous = {c.line for c in comments if c.standalone and c.column == token.column}
        while start - 1 in previous:
            start -= 1
        test = Test(name, identifier, token.line, start, body.end, token.column)
        namespace = tuple(identifier.split("::")[:-1])
        inspector = Inspect(aliases, helpers.get(id(scope), frozenset()), file_iterables,
                            namespace, set(mutated_names(body.children)), parameters,
                            dict(parameter_names(body.children)))
        for local in declared_bindings(body.children):
            inspector.oracle_locals.setdefault(local.text, local.line)
        inspector.visit(body.children)
        test.issues, test.measured = inspector.issues(name)
        if not inspector.checks and not panic:
            test.issues.append((token.line, "no-assert"))
        test.guarded = bool(inspector.checks) and not panic and all(
            guarded for _, _, _, guarded in inspector.checks)
        test.conditional = inspector.assumes
        tests.append(test)
    return tests, comments
