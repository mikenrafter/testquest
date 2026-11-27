from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import re
from string import Formatter

from .lint import (UNKNOWN, SuffixedNumber, assertion, different_callees, argument_claims, fixed_boundary, fixed_operand, helper, loose_bounds,
                   named_arguments, prefix_satisfies, DISTINCT_INPUTS, claims_effect, OUTCOME_CLAIM, REPEATABILITY,
                   symmetric_input, unvaried_arguments, IMPORT_FIELDS, IMPORT_TOKEN, inverse_codecs, path_directories, pins_path)
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
    end_column: int = 0


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
                                      "group", tuple(children), token.line, token.column + 1))
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
            template = any(name in {("template",), ("rstest_reuse", "::", "template")} for name in names)
            is_test = not template and any(test_attribute(name) for name in names)
            if is_test:
                for body_index in range(index + 2, len(tokens)):
                    body = tokens[body_index]
                    if body.text == ";":
                        break
                    if body.text == "{" and body.kind == "group":
                        panic = any(name and name[0] == "should_panic" for name in names)
                        ignored = any(name and name[0] == "ignore" for name in names)
                        name = tokens[index + 1].text
                        scope = tokens if module_scope else None
                        header = next((part.children for part in tokens[index + 2:body_index]
                                       if part.text == "("), ())
                        yield (token, name, "::".join((*namespace, name)), attributes[0][0].line,
                               body, panic, ignored, aliases, scope, header, attributes[0][0].column)
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


def function_items(tokens):
    for index, token in enumerate(tokens[:-1]):
        if token.text == "fn" and tokens[index + 1].kind == "word":
            for body_index in range(index + 2, len(tokens)):
                body = tokens[body_index]
                if body.text == ";":
                    break
                if body.text == "{":
                    yield index, body_index
                    break


def function_bodies(tokens):
    """Yield (name, body token) for functions declared directly in this scope."""
    for start, end in function_items(tokens):
        yield tokens[start + 1].text, tokens[end]


def type_arguments_end(tokens, start):
    depth = 0
    for index in range(start, len(tokens)):
        text = tokens[index].text
        if text == "<":
            depth += 1
        elif text == ">":
            depth -= 1
            if depth == 0:
                return index
    return None


def invocation_at(tokens, index, *, generic=False):
    if tokens[index].kind != "word" or index + 1 >= len(tokens):
        return None
    following = tokens[index + 1]
    if generic and following.text == "::" and index + 2 < len(tokens) and tokens[index + 2].text == "<":
        end = type_arguments_end(tokens, index + 2)
        if end is not None and end + 1 < len(tokens):
            following = tokens[end + 1]
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


def walk_tokens(tokens):
    for token in tokens:
        yield token
        if token.kind == "group":
            yield from walk_tokens(token.children)


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


def macro_expansions(tokens):
    for index in range(len(tokens) - 3):
        declaration = tokens[index:index + 4]
        if (declaration[0].text != "macro_rules" or declaration[1].text != "!"
                or declaration[2].kind != "word" or declaration[3].kind != "group"):
            continue
        arms = declaration[3].children
        expansions = [arm.children for marker, arm in zip(arms, arms[1:])
                      if marker.text == "=>" and arm.kind == "group"]
        yield declaration[2].text, tuple(expansions)


def statement_lists(tokens):
    yield tokens
    for token in tokens:
        if token.text == "{":
            yield from statement_lists(token.children)


def macro_template_tests(tokens):
    for _, arms in macro_expansions(tokens):
        for body in arms:
            yield from (id(row[4]) for row in test_functions(body))
    for token in tokens:
        if token.text == "{":
            yield from macro_template_tests(token.children)


def validates_before_extraction(tokens, index):
    if (index < 3 or tokens[index].text not in {"unwrap", "expect"}
            or tokens[index - 1].text != "."):
        return False
    callee = tokens[index - 3]
    return (tokens[index - 2].text == "(" and callee.kind == "word"
            and callee.text.startswith(("validate", "verify", "check")))


def has_trait_bound(signature):
    if not signature or signature[0].text != "<":
        return False
    end = type_arguments_end(signature, 0)
    if end is None:
        return False
    predicates = list(split(signature[1:end], type_arguments=True))
    for index in range(end + 1, len(signature)):
        if signature[index].text == "where":
            predicates.extend(split(signature[index + 1:], type_arguments=True))
            break
    for predicate in predicates:
        if predicate[0].text in {"const", "'"}:
            continue
        parts = list(split(predicate, ":"))
        if len(parts) > 1 and any(bound[0].kind == "word" for bound in split(parts[1], "+")):
            return True
    return False


class CheckEvidence:
    """Resolve calls that supply evidence for no-assert."""

    def __init__(self, file_iterables, tests):
        self.file_iterables = file_iterables
        self.functions = file_iterables.function_sources.copy()
        self.macros = file_iterables.macro_sources.copy()
        self.methods = file_iterables.method_sources.copy()
        self.imported_modules = {}
        for aliases in file_iterables.aliases.values():
            for name in aliases.values():
                if name[:1] == ("crate",) and len(name) >= 3 and name[-1][0].isupper():
                    self.index_imported_type(name[1:])
        self.method_names = {path[-1] for path in self.methods}
        self.trait_checks = set()
        self.known = set()
        for scope, tokens in self.functions.copy().items():
            self.index_local(scope, tokens)
        for _, _, identifier, _, body, *_ in tests:
            name = tuple(identifier.split("::"))
            if name not in self.functions:
                self.functions[name] = body.children
                self.index_local(name, body.children)
        pending = [(False, name, name, (tokens,)) for name, tokens in self.functions.items()]
        pending.extend((True, name, name[:-1], arms) for name, arms in self.macros.items())
        while pending:
            remaining = []
            for macro, name, scope, bodies in pending:
                if any(self.checks(body, scope, in_helper=not macro) for body in bodies):
                    self.known.add((macro, name))
                else:
                    remaining.append((macro, name, scope, bodies))
            if len(remaining) == len(pending):
                break
            pending = remaining

    def index_imported_type(self, owner):
        module = owner[:-1]
        if module not in self.imported_modules:
            path = self.file_iterables.module_file(module)
            self.imported_modules[module] = module_items(path) if path else None
        items = self.imported_modules[module]
        if items is None:
            return
        for name, tokens in items.method_sources.items():
            path = (*module, *name)
            if path[:-1] == owner:
                self.methods[path] = self.functions[path] = tokens
        if any(path[:-1] == owner for path in self.methods):
            for name, tokens in items.function_sources.items():
                if len(name) == 1:
                    self.functions.setdefault((*module, *name), tokens)

    def index_local(self, scope, tokens):
        for name, arms in macro_expansions(tokens):
            self.macros[(*scope, name)] = arms
        for start, end in function_items(tokens):
            name = tokens[start + 1].text
            path = (*scope, name)
            signature = tokens[start + 2:end]
            if name.startswith(("assert", "check")) and has_trait_bound(signature):
                self.trait_checks.add(path)
            body = tokens[end].children
            self.functions[path] = body
            self.index_local(path, body)

    def resolve(self, scope, name, macro):
        definitions = self.macros if macro else self.functions
        if len(name) == 1:
            for depth in range(len(scope), -1, -1):
                owner = scope[:depth]
                path = (*owner, *name)
                if (macro or owner in self.functions) and path in definitions:
                    return path
        scope = self.module_scope(scope)
        if macro:
            path = absolute_path(scope, name)
            if path in definitions:
                return path
        path = self.file_iterables.resolve(scope, name)
        if not path and not macro and scope in self.imported_modules:
            return absolute_path(scope, name)
        return path

    def module_scope(self, scope):
        while scope in self.functions:
            scope = scope[:-1]
        return scope

    def receiver_type(self, receiver, scope, before):
        if receiver == "self" and scope in self.methods:
            return scope[:-1]
        body = self.functions.get(scope, ())
        types = set()
        for tokens in statement_lists(body):
            for index, token in enumerate(tokens):
                if (token.line, token.column) >= before:
                    break
                if token.text != "let":
                    continue
                declaration = next(iter(split(tokens[index + 1:], ";")), ())
                parts = list(split(declaration, "="))
                if len(parts) != 2:
                    continue
                pattern = list(split(parts[0], ":"))
                if shape(pattern[0]) not in {(receiver,), ("mut", receiver)}:
                    continue
                if len(pattern) == 2:
                    types.add(qualified_name(unborrow(pattern[1])))
                else:
                    initializer = unborrow(parts[1])
                    if initializer and initializer[-1].text == "(":
                        name = qualified_name(initializer[:-1])
                        types.add(name[:-1])
                    else:
                        types.add(())
        if len(types) != 1 or not (name := types.pop()):
            return ()
        if name == ("Self",) and scope in self.methods:
            return scope[:-1]
        module = self.module_scope(scope)
        return self.file_iterables.resolve(module, name) or absolute_path(module, name)

    def checks(self, tokens, scope, *, in_helper=False, ignore_status=False, visited=frozenset()):
        def known_check(macro, path):
            if (macro, path) not in self.known:
                return False
            if not ignore_status:
                return True
            if (macro, path) in visited:
                return False
            seen = visited | {(macro, path)}
            bodies = self.macros[path] if macro else (self.functions[path],)
            return any(self.checks(body, path[:-1] if macro else path, in_helper=not macro,
                                   ignore_status=True, visited=seen) for body in bodies)

        declarations = dict(function_items(tokens))
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if index in declarations:
                index = declarations[index] + 1
                continue
            if token.text == "let" and index + 4 < len(tokens):
                binding = tuple(part.text for part in tokens[index + 1:index + 5])
                if binding == ("_", ":", "fn", "(") and item_value(tokens, index):
                    return True
            if token.text == "macro_rules" and index + 1 < len(tokens) and tokens[index + 1].text == "!":
                index += 4
                continue
            invocation = invocation_at(tokens, index, generic=True)
            if invocation:
                arguments, macro = invocation
                if (ignore_status and macro and token.text in ASSERTIONS
                        and success_check(token.text, list(split(arguments.children)))):
                    index += 3
                    continue
                start = callee_start(tokens, index)
                if start is not None and tokens[start].text == "::":
                    start += 1
                name = qualified_name(tokens[start:index + 1]) if start is not None else ()
                path = self.resolve(scope, name, macro) if name else ()
                definitions = self.macros if macro else self.functions
                method = not macro and index >= 2 and tokens[index - 1].text == "."
                if method and token.text in self.method_names:
                    receiver = tokens[index - 2]
                    if receiver.kind == "word" and (index < 3 or tokens[index - 3].text != "."):
                        owner = self.receiver_type(receiver.text, scope, (token.line, token.column))
                        path = (*owner, token.text) if owner else ()
                if (ignore_status and method and token.text == "expect"
                        and scope and scope[-1].endswith("_request_format")
                        and integer_literal(arguments.children) is not None):
                    index += 2
                    continue
                if method and path in self.methods:
                    if known_check(False, path):
                        return True
                elif path in definitions:
                    if macro:
                        if known_check(True, path):
                            return True
                        index += 3
                        continue
                    if path in self.trait_checks or (known_check(False, path) and (
                            self.module_scope(path) == self.module_scope(scope) or helper(path[-1]))):
                        return True
                elif (starts_check(tokens, index, macro, (), in_helper=in_helper)
                      or (in_helper and macro and token.text == "panic"
                          and (scope in self.methods or scope[-1].startswith(("assert", "check", "verify"))))
                      or (method and token.text.startswith(("debug_assert", "assert", "check", "verify"))
                          and (not in_helper or scope in self.methods))):
                    return True
                if (scope in self.methods and method and token.text in {"expect", "unwrap"}
                        and index >= 3 and tokens[index - 2].text == "("
                        and tokens[index - 3].text in {"recv_timeout", "wait_timeout", "timeout"}):
                    return True
                if not ignore_status and validates_before_extraction(tokens, index):
                    return True
            if token.kind == "group" and self.checks(token.children, scope, in_helper=in_helper,
                                                     ignore_status=ignore_status, visited=visited):
                return True
            index += 1
        return False


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
        self.function_sources = {}
        self.delegates = {}
        self.macro_sources = {}
        self.method_sources = {}
        self.collection_sources = set()
        self.type_names = set()
        self.path = path
        self.aliases = {}
        self.globs = defaultdict(list)
        self.relative_globs = defaultdict(list)
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
                        if name[0] in {"crate", "self", "super"}:
                            self.relative_globs[scope].append(name)
            elif token.text in {"struct", "enum", "union", "type", "trait"}:
                if index + 1 < len(tokens) and tokens[index + 1].kind == "word":
                    self.type_names.add((*scope, tokens[index + 1].text))
            elif token.text == "impl":
                head = tokens[index + 1:index + 3]
                if len(head) == 2 and head[0].kind == "word" and head[1].text == "{":
                    owner = (*scope, head[0].text)
                    self.method_sources.update(((*owner, name), body.children)
                                               for name, body in function_bodies(head[1].children))
                    if self.test_file or test_module(head[0].text) or test_only(tokens, index):
                        self.test_items.add(owner)
                    self.index(head[1].children, owner)
            elif token.text == "mod":
                head = tokens[index + 1:index + 3]
                if len(head) == 2 and head[0].kind == "word":
                    child = (*scope, head[0].text)
                    if self.test_file or test_module(head[0].text) or test_only(tokens, index):
                        self.test_items.add(child)
                    if head[1].text == "{":
                        self.index(head[1].children, child)
            elif token.text == "fn" and index + 1 < len(tokens) and test_only(tokens, index):
                self.test_items.add((*scope, tokens[index + 1].text))
        for start, end in function_items(tokens):
            name = (*scope, tokens[start + 1].text)
            body = tokens[end]
            self.add(name, "helper", return_expression(body))
            self.function_sources[name] = body.children
            signature = tokens[start + 2:end]
            for index, token in enumerate(signature):
                if token.text == "->":
                    result = signature[index + 1:]
                    head = next(iter(split(result, "<")), ())
                    collection = qualified_name(head)
                    if (shape(result[:1]) == ("[",) or collection and collection[-1] in {
                            "Vec", "VecDeque", "HashSet", "BTreeSet", "HashMap", "BTreeMap"}):
                        self.collection_sources.add(name)
                    break
        self.macro_sources.update(((*scope, name), bodies) for name, bodies in macro_expansions(tokens))

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
        module = self.module_file(path[:-1])
        return module_functions(module).get(path[-1], ()) if module else ()

    def delegate(self, path):
        if path not in self.delegates:
            self.delegates[path] = self.find_delegate(path)
        return self.delegates[path]

    def find_delegate(self, path):
        body = self.function(path)
        parameters = self.parameters.get(path, {})
        index, local_path = self, path
        if not body and path[:-1] and (source := self.module_file(path[:-1])):
            if source != self.path and (module := module_items(source)):
                index, local_path = module, path[-1:]
                body = index.function(local_path)
                parameters = index.parameters.get(local_path, {})
        if not index.production(local_path):
            return ()
        bindings, result = local_returns(body)
        name, arguments = function_call(result)
        if (not name or name[0] in bindings or name[0] in parameters.values()
                or len(arguments) != len(parameters)):
            return ()
        used = set()
        for position, argument in enumerate(arguments):
            argument = unborrow(argument)
            if len(argument) == 1 and argument[0].text in bindings:
                local = argument[0].text
                used.add(local)
                argument = unborrow(bindings[local])
            original = converted_input(argument) or argument
            if shape(unborrow(original)) != (parameters.get(position),):
                return ()
        if used != bindings.keys():
            return ()
        target = index.resolve(local_path[:-1], name)
        if index is not self and target and name[0] != "crate":
            if name[0] not in index.aliases.get(local_path[:-1], {}):
                target = (*path[:-1], *target)
        return target

    def module_file(self, path):
        for root in self.path.parents:
            if (root / "lib.rs").is_file() or (root / "main.rs").is_file():
                module = root.joinpath(*path)
                if path:
                    candidates = (module.with_suffix(".rs"), module / "mod.rs")
                else:
                    candidates = (root / "lib.rs", root / "main.rs")
                files = [candidate for candidate in candidates if candidate.is_file()]
                return files[0] if len(files) == 1 else None
        return None

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
def module_items(path):
    try:
        tokens, _ = tokenize(path.read_text(encoding="utf-8"))
        grouped = group(tokens)
        return FileIterables(grouped, path, dict(file_parameters(grouped)))
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=64)
def module_functions(path):
    try:
        tokens, _ = tokenize(path.read_text(encoding="utf-8"))
        return {name: return_expression(body) for name, body in function_bodies(group(tokens))}
    except (OSError, ValueError):
        return {}


def function_call(tokens):
    if tokens and tokens[-1].text == "(":
        name = qualified_name(tokens[:-1])
        if name:
            return name, tuple(split(tokens[-1].children, type_arguments=True))
    return (), ()


def arithmetic_operands(tokens):
    operators = {"+", "-", "*", "/", "+=", "-=", "*=", "/="}
    for index, token in enumerate(tokens):
        if token.children:
            yield from arithmetic_operands(token.children)
        if token.kind != "word" or index and tokens[index - 1].text in {".", "::"}:
            continue
        end = index + 1
        path = [token.text]
        while end < len(tokens):
            part = tokens[end]
            if part.text == "." and end + 1 < len(tokens) and tokens[end + 1].kind != "group":
                path.append(tokens[end + 1].text)
                end += 2
            elif part.text == "[" and integer_literal(part.children) is not None:
                path.append(str(integer_literal(part.children)))
                end += 1
            elif part.text == "as" and end + 1 < len(tokens):
                end += 2
            else:
                break
        if end < len(tokens) and tokens[end].text in {"(", "::"}:
            continue
        if index and (operator := tokens[index - 1].text) in operators:
            if operator != "*" or index >= 2 and tokens[index - 2].kind in {"word", "number", "float", "group"}:
                yield tuple(path), operator[0]
        if end < len(tokens) and (operator := tokens[end].text) in operators:
            yield tuple(path), operator[0]


def stored_operands(body, methods):
    if len(body) != 2 or body[0].kind != "word" or body[1].text != "{":
        return {}
    fields = {}
    for field in split(body[1].children):
        parts = shape(field)
        if len(parts) == 1 and field[0].kind == "word":
            fields[parts[0]] = parts[0]
        elif len(parts) == 3 and parts[1] == ":" and field[0].kind == field[2].kind == "word":
            fields[parts[0]] = parts[2]
        else:
            return {}
    operands = defaultdict(set)
    for method in methods:
        for path, operator in arithmetic_operands(method):
            if len(path) >= 2 and path[0] == "self" and path[1] in fields:
                operands[fields[path[1]]].add(operator)
    return operands


def local_returns(body):
    statements = list(split(body, ";"))
    if not statements:
        return {}, ()
    result = statements[-1]
    if body[-1].text == ";":
        if result[0].text != "return":
            return {}, ()
        result = result[1:]
    bindings = {}
    for statement in statements[:-1]:
        declaration = list(split(statement[1:], "="))
        if (statement[0].text != "let" or len(declaration) != 2 or not declaration[0]
                or declaration[0][0].kind != "word"):
            return {}, ()
        name = declaration[0][0].text
        if name == "mut" or name in bindings:
            return {}, ()
        bindings[name] = declaration[1]
    return bindings, result


def input_binding(tokens, bindings):
    tokens = unborrow(tokens)
    seen = set()
    while len(tokens) == 1 and (name := tokens[0].text) in bindings and name not in seen:
        seen.add(name)
        tokens = unborrow(bindings[name])
    return tokens


def mapped_input(tokens):
    receiver, method, arguments = method_call(tokens)
    if method != "collect" or arguments:
        return (), ()
    iterator, method, converter = method_call(receiver)
    source, iteration, arguments = method_call(iterator)
    if method == "map" and iteration in {"iter", "into_iter"} and not arguments:
        return source, converter
    return (), ()


def converted_input(tokens):
    source, converter = mapped_input(tokens)
    if source:
        if name := qualified_name(converter):
            return source if name[-1].startswith(("from_", "to_")) else ()
        if len(converter) > 3 and shape(converter[:3:2]) == ("|", "|"):
            receiver, method, arguments = method_call(converter[3:])
            if (shape(receiver) == (converter[1].text,) and not arguments
                    and method.startswith(("from_", "to_"))):
                return source
        return ()
    receiver, method, args = method_call(tokens)
    if not args and method.startswith(("from_", "to_")):
        return receiver
    name, args = function_call(tokens)
    return args[0] if len(args) == 1 and name[-1].startswith(("from_", "to_")) else ()


@dataclass
class PairedCall:
    invocation: tuple
    position: int
    path: tuple
    arguments: tuple
    bindings: dict
    empty_parameter: str | None

    def uses_empty_input(self):
        if self.empty_parameter is None or not self.arguments:
            return False
        source = self.arguments[0]
        for _ in range(len(self.bindings) + 1):
            source = input_binding(source, self.bindings)
            if shape(source) == (self.empty_parameter,):
                return True
            source, _ = mapped_input(source)
            if not source:
                break
        return False


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


def growth_receivers(tokens):
    names = set()
    for index, token in enumerate(tokens):
        if token.kind == "group":
            names.update(growth_receivers(token.children))
        elif (token.kind == "word" and index + 3 < len(tokens) and tokens[index + 1].text == "."
              and tokens[index + 2].text in {"push", "push_back", "push_front", "extend", "insert", "append", "resize"}
              and tokens[index + 3].text == "("):
            names.add(token.text)
    return names


def binding_path(tokens):
    if not tokens or tokens[0].kind != "word" or len(tokens) % 2 != 1:
        return ()
    for dot, field in zip(tokens[1::2], tokens[2::2]):
        if dot.text != "." or field.kind not in {"word", "number"}:
            return ()
    return shape(tokens)


def contains_call(tokens):
    return any((token.kind == "word" and invocation_at(tokens, i))
               or (token.kind == "group" and contains_call(token.children))
               for i, token in enumerate(tokens))


def assertion_comparison(name, args):
    if name in ASSERTIONS and name.endswith(("_eq", "_ne")) and len(args) >= 2:
        return ("==" if name.endswith("_eq") else "!="), *args[:2]
    if name in ASSERTIONS and args:
        return comparison(args[0])
    return "", (), ()


def adjacent_pair_order(pair, arguments):
    predicate = next(split(arguments), ())
    operator, left, right = comparison(predicate)
    if operator not in {"<", "<=", ">", ">="} or min(len(left), len(right)) < 2:
        return False
    return (left[0].text == right[0].text == pair and left[1].text == right[1].text == "["
            and {integer_literal(left[1].children), integer_literal(right[1].children)} == {0, 1}
            and shape(left[2:]) == shape(right[2:])
            and (len(left) == 2 or binding_path((left[0], *left[2:]))))


def recorder_initializer(tokens):
    if shape(tokens) in {("Vec", "::", "new", "("), ("vec", "!", "[")}:
        return "vector"
    if literal(tokens) in (False, 0):
        return "scalar"
    if (len(tokens) >= 4 and shape(tokens[-4:-1]) == ("Cell", "::", "new")
            and tokens[-1].text == "(" and literal(tokens[-1].children) in (False, 0)):
        return "cell"
    return ""


def pattern_names(tokens):
    return (token.text for token in binding_tokens(tokens))


ORACLE_METHODS = {"to_json", "tobytes", "to_string", "clone", "iter", "into_iter", "collect",
                  "read_bytes", "read_to_string", "as_str", "as_slice", "as_ref", "as_bytes",
                  "unwrap", "expect"}
STRING_TRANSFORMS = {"trim": "strip", "trim_start": "lstrip", "trim_end": "rstrip",
                     "to_lowercase": "lower", "to_uppercase": "upper"}
IDENTIFIER_STRATEGY = re.compile(r"\[(?:a-z|A-Z|_)+\](?:\+|\[(?:a-z|A-Z|0-9|_)+\]\{0,\d+\})?")
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
        self.collector_writes = defaultdict(list)
        self.callback_writes = {}
        self.counter_reads = set()
        self.recorder_inputs = defaultdict(set)
        self.binding_changes = defaultdict(list)
        self.alias_bindings = {}
        self.increments = set()
        self.assumes = False
        self.symmetric_calls = defaultdict(list)
        self.parameters = parameters
        self.local_parameters = local_parameters
        self.oracle_locals = {}
        self.expected = set()
        self.calls = defaultdict(list)
        self.check_arguments = []
        self.window_order = {}
        self.strategies = {}
        self.paired_bindings = {}
        self.parity_mutations = set()
        self.guard_iterables = None

    def visit(self, tokens, guarded=False, inherited=None, *, closure=False,
              collector_path=(), collector_comparison=False, counters=None, guard_imports=None):
        if self.guard_iterables:
            from . import rust_guard

        locals_here = dict(inherited or ())
        counter_blocks = {}
        if self.guard_iterables:
            counters = dict(counters or ())
            guard_imports = dict(guard_imports or ())
            guard_imports.update((alias, path) for path, alias in imports_in(tokens, keep_self=True))
        check_prefix = True
        loop_growth = {}
        guarded_blocks = conditional_blocks(tokens)
        block_locals = {}
        collector_blocks = {}

        def lookup(expression, kind):
            name = qualified_name(expression)
            if name and name[0] in locals_here:
                return locals_here[name[0]].get(kind) if len(name) == 1 else None
            return self.file_iterables.lookup(self.scope, name, kind)

        def guard_lookup(expression, kind):
            name = qualified_name(expression)
            if name and name[0] in locals_here:
                return locals_here[name[0]].get(kind) if len(name) == 1 else None
            value = self.file_iterables.lookup(self.scope, name, kind)
            return value if value is not None else self.guard_iterables.lookup(
                self.scope, expression, kind, guard_imports)

        def first_iteration_lookup(expression, kind):
            if kind == "number" and (counter := counters.get(qualified_name(expression))):
                return counter.start
            return guard_lookup(expression, kind)

        declaration_names = set()
        in_closure = closure
        for index, token in enumerate(tokens):
            text = token.text
            if self.guard_iterables and text in rust_guard.EXITS:
                check_prefix = False
            if text == "||" and (not index or tokens[index - 1].text in {",", "=", "move", "return"}):
                in_closure = True
            elif text in {",", ";"}:
                in_closure = closure
            if text in {"for", "while"}:
                loop = loop_block(tokens, index)
                if loop is not None:
                    position, pattern, header = loop
                    block_locals[position] = dict.fromkeys(pattern_names(pattern), {})
                    if text == "for":
                        receiver, method, width = method_call(header)
                        if method == "windows" and integer_literal(width) == 2 and len(pattern) == 1:
                            for statement in split(tokens[position].children, ";"):
                                if (macro_at(statement, 0, {"assert", "debug_assert", "prop_assert"})
                                        and adjacent_pair_order(pattern[0].text, statement[2].children)):
                                    self.window_order[statement[0].line] = receiver
                        runs = (iterable_size(header, lookup) or 0) > 0
                        if self.guard_iterables:
                            loop_lookup = guard_lookup
                            facts = rust_guard.iterable(header, loop_lookup)
                            if counters and (not facts or facts.minimum == 0):
                                probe = rust_guard.iterable(header, first_iteration_lookup)
                                if probe and probe.minimum:
                                    facts, loop_lookup = probe, first_iteration_lookup
                                    counter_blocks[position] = {
                                        name: range(reach.start, reach.start + 1)
                                        for name, reach in counters.items()
                                    }
                            if check_prefix and facts and facts.minimum > 0:
                                runs = True
                                block_locals[position].update(
                                    {name: evidence for name, evidence in rust_guard.pattern_facts(
                                        pattern, facts.rows, guard_lookup).items()
                                     if name not in self.mutated})
                            name = qualified_name(pattern)
                            loop_tokens = tokens[position].children
                            if runs and check_prefix and not any(
                                    part.text in rust_guard.EXITS for part in walk_tokens(loop_tokens)):
                                if (len(name) == 1 and name[0] not in self.mutated
                                        and (bounds := rust_guard.range_bounds(header, loop_lookup))
                                        and bounds[0] < bounds[1]):
                                    counter_blocks.setdefault(position, {})[name] = range(*bounds)
                                if facts and facts.minimum and loop_lookup is guard_lookup:
                                    shadowed = {part.text for part in declared_bindings(loop_tokens)}
                                    shadowed.update(block_locals[position])
                                    loop_growth[position] = (facts, [
                                        (name, kind) for name, kind in rust_guard.direct_insertions(loop_tokens)
                                        if name not in shadowed and name not in self.mutated
                                    ])
                    else:
                        runs = shape(header) == ("true",)
                        if self.guard_iterables and check_prefix:
                            receiver = rust_guard.join_set_receiver(header)
                            join_set = locals_here.get(receiver, {}).get("join_set")
                            runs = runs or bool(join_set and join_set.minimum)
                    if not runs:
                        guarded_blocks.add(position)
                        source = binding_path(unborrow(header))
                        collector_blocks[position] = (source[0] if len(source) == 1 else "", False)
            if text in {"if", "match"}:
                position = next((i for i in range(index + 1, len(tokens))
                                 if tokens[i].text in {"{", ";"}), len(tokens))
                condition = tokens[index + 1:position]
                if text == "if" and position in guarded_blocks and comparison(condition)[0]:
                    collector_blocks[position] = (None, True)
                if self.guard_iterables and check_prefix and position in guarded_blocks and counters:
                    if text == "if" and (witness := rust_guard.branch_witness(condition, counters, guard_lookup)):
                        guarded_blocks.discard(position)
                        name, reach = witness
                        counter_blocks[position] = {name: reach}
                    elif text == "match" and (reach := counters.get(qualified_name(condition))):
                        if rust_guard.arm_checks(tokens[position].children, reach):
                            guarded_blocks.discard(position)
            if text in {"let", "const"}:
                declaration = next(iter(split(tokens[index + 1:], ";")), ())
                parts = list(split(declaration, "="))
                pattern = next(iter(split(parts[0], ":")), ()) if parts else ()
                bare = pattern[1:] if shape(pattern[:1]) == ("mut",) else pattern
                names = tuple(pattern_names(pattern))
                if text == "const" and (not pattern or pattern[0].text == "fn"):
                    names = ()
                previous = tokens[index - 1].text if index else ""
                if previous in {"if", "while"}:
                    position = next((i for i in range(index + 1, len(tokens))
                                     if tokens[i].text == "{"), None)
                    if position is not None:
                        block_locals[position] = dict.fromkeys(names, {})
                else:
                    locals_here.update((name, {}) for name in names)
                    if counters:
                        for name in names:
                            counters.pop((name,), None)
                    if len(parts) == 2 and len(bare) == 1 and names:
                        name = names[0]
                        if name not in self.mutated:
                            value = parts[1]
                            locals_here[name] = {
                                "size": literal_size(value, lambda count: known_integer(count, lookup)),
                                "number": integer_literal(value),
                            }
                            if self.guard_iterables:
                                annotation = parts[0][len(pattern):]
                                locals_here[name].update(
                                    iterable=rust_guard.iterable(value, guard_lookup,
                                                                vector=shape(annotation[:3]) == (":", "Vec", "<")),
                                    number=rust_guard.integer(value, guard_lookup))
                                if shape(value) == ("tokio", "::", "task", "::", "JoinSet", "::", "new", "("):
                                    locals_here[name]["join_set"] = rust_guard.Iterable(0, 0)
            if text == "let":
                if len(parts) == 2:
                    lhs, rhs = parts
                    if len(pattern) == 1 and pattern[0].text == "(":
                        pair = list(split(pattern[0].children))
                        if len(pair) == 2 and all(len(name) == 1 and name[0].kind == "word" for name in pair):
                            for position, name in enumerate(pair):
                                self.paired_bindings[name[0].text] = rhs, position
                    target = next((item for item in lhs if item.kind == "word" and item.text != "mut"), None)
                    if target:
                        declaration_names.add(id(target))
                        self.bindings.setdefault(target.text, []).append((token.line, rhs))
                        if inherited is None and previous not in {"if", "while"} and rhs and binding_path(bare) == (target.text,):
                            self.alias_bindings[target.text] = ((rhs[-1].line, rhs[-1].column), rhs)
            following = shape(tokens[index + 1:index + 3])
            if token.kind == "word" and (not index or tokens[index - 1].text not in {".", "::"}):
                end = index + 1
                while end < len(tokens):
                    if tokens[end].text == "[":
                        end += 1
                    elif end + 1 < len(tokens) and tokens[end].text == "." and tokens[end + 1].kind in {"word", "number"}:
                        end += 2
                    else:
                        break
                after_path = tuple(part.text for part in tokens[end:end + 2])
                assigned = (after_path[:1] in {("=",), ("+=",)}
                            or len(after_path) == 2 and after_path[0] in {"-", "*", "/", "%", "&", "|", "^"} and after_path[1] == "=")
                borrowed = shape(tokens[max(0, index - 2):index]) == ("&", "mut")
                method = following[1] if len(following) == 2 and following[0] == "." else ""
                invokes_method = end > index + 1 and after_path[:1] in {("(",), ("::",)}
                if assigned or borrowed or invokes_method or id(token) in declaration_names:
                    self.binding_changes[text].append((token.line, token.column))
                if method in RECORDING_METHODS:
                    self.writes.setdefault(text, token.line)
                    self.collector_writes[text].append((collector_path, collector_comparison))
                elif in_closure and id(token) not in declaration_names and (
                        end == index + 1 and assigned or method == "set"):
                    kind = "cell" if method == "set" else "scalar"
                    self.callback_writes.setdefault(text, (token.line, kind))
                    if method == "set" and end < len(tokens) and tokens[end].text == "(":
                        update = tokens[end].children
                        if (len(update) == 6 and shape(update[:5]) == (text, ".", "get", "(", "+")
                                and type(literal(update[5:])) is int):
                            self.counter_reads.add(id(update[0]))
                elif id(token) not in declaration_names and id(token) not in self.counter_reads:
                    self.reads.add(text)
            elif token.kind == "word":
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
                if (self.guard_iterables and check_prefix and not in_closure
                        and text in {"push", "extend"} and len(args) == 1 and index >= 2
                        and tokens[index - 1].text == "."
                        and (index == 2 or tokens[index - 3].text in {";", "{"})):
                    name = tokens[index - 2].text
                    added = (rust_guard.Iterable(1, 1) if text == "push"
                             else rust_guard.iterable(args[0], guard_lookup))
                    rust_guard.extend_local(locals_here, name, added)
                if assertion(text):
                    self.check_arguments.append(arguments)
                    if text in ASSERTIONS and text.endswith("_eq") and len(args) >= 2:
                        self.expected.update(expected_constructors(args[1], expected=True))
                    elif text in {"assert", "debug_assert", "prop_assert"} and args:
                        self.expected.update(expected_constructors(args[0]))
                checking = starts_check(tokens, index, macro, self.helpers)
                if checking:
                    self.checks.append((token.line, text, args, guarded or not check_prefix))

                start = callee_start(tokens, index)
                callee = shape(tokens[start:index + 1]) if start is not None else None
                literals = {position: literal(arg, signed_and_float=True)
                            for position, arg in enumerate(args)}
                if callee and not macro:
                    for slot, arg in enumerate(args):
                        if len(arg) == 3 and shape(arg[:2]) == ("&", "mut") and arg[2].kind == "word":
                            self.recorder_inputs[arg[2].text].add(((callee, slot), token.line))
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
                    in_block = {**locals_here, **block_locals[index]}
                source, mismatch = collector_blocks.get(index, ("" if index in guarded_blocks else None, False))
                path = (*collector_path, source) if source is not None else collector_path
                block_counters = None
                if self.guard_iterables:
                    block_counters = {name: reach for name, reach in counters.items()
                                      if name[0] not in block_locals.get(index, ())}
                    block_counters.update(counter_blocks.get(index, ()))
                self.visit(token.children, guarded or not check_prefix or index in guarded_blocks, in_block,
                           closure=in_closure or self.closure_block(tokens, index),
                           collector_path=path, collector_comparison=collector_comparison or mismatch,
                           counters=block_counters, guard_imports=guard_imports)
                if index in loop_growth:
                    growth, names = loop_growth[index]
                    for name, kind in names:
                        rust_guard.extend_local(locals_here, name, growth, kind)
                if self.guard_iterables and token.text == "{" and any(
                        part.text in rust_guard.EXITS for part in walk_tokens(token.children)):
                    check_prefix = False

    @staticmethod
    def closure_block(tokens, index):
        return index > 1 and tokens[index].text == "{" and tokens[index - 1].text == "|" and any(
            token.text == "|" for token in tokens[:index - 1])

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

    @staticmethod
    def equality(name, args):
        if name in ASSERTIONS and name.endswith("_eq") and len(args) >= 2:
            return args[:2]
        if name in {"assert", "debug_assert", "prop_assert"} and args:
            op, left, right = comparison(args[0])
            if op == "==":
                return left, right
        return (), ()

    def literal_pins(self):
        for line, name, args, guarded in self.checks:
            if guarded:
                continue
            left, right = self.equality(name, args)
            for operand, other in ((left, right), (right, left)):
                if literal(self.resolve(other, line), signed_and_float=True) is UNKNOWN:
                    continue
                resolved = self.resolve(operand, line)
                if qualified_name(resolved) and (constant := self.oracle_constant(resolved, line)):
                    yield constant
                if resolved and resolved[-1].text == "(":
                    yield from self.oracle_calls(resolved, line) or ()

    def sized_from(self, tokens, constant, line, seen=frozenset()):
        tokens = self.resolve(tokens, line)
        if not tokens or id(tokens[0]) in seen:
            return False
        seen = seen | {id(tokens[0])}
        for index, token in enumerate(tokens):
            if token.text in {"+", "-", "*"}:
                left, right = tokens[:index], tokens[index + 1:]
                for operand, other in ((left, right), (right, left)):
                    if integer_literal(other) is not None and self.oracle_constant(operand, line) == constant:
                        return True
            elif token.kind == "group":
                for part in split(token.children, ";" if token.text == "[" else ","):
                    if self.sized_from(part, constant, token.line, seen):
                        return True
            elif token.kind == "word" and token.text in self.bindings and token.text not in self.mutated:
                original = self.resolve((token,), token.line)
                if original != (token,) and self.sized_from(original, constant, token.line, seen):
                    return True
        return False

    def shared_constant(self, calls, actual, expected, line, pins):
        constant = self.oracle_constant(expected, line)
        return (calls and len(calls) == 1 and constant not in pins
                and self.file_iterables.shares_constant(next(iter(calls)), constant)
                and not self.sized_from(actual, constant, line))

    def composition(self, actual, expected, pins, strings):
        if not different_callees(actual, expected):
            return False
        directories = []
        for path in actual | expected:
            if "path" not in path[-1]:
                continue
            body = self.file_iterables.function(path)
            segments = (literal(token.children) for index, token in enumerate(body)
                        if token.text == "(" and shape(body[max(0, index - 2):index]) == (".", "join"))
            if parts := path_directories(segment for segment in segments if isinstance(segment, str)):
                directories.append(parts)
        if directories:
            return any(not pins_path(parts, strings) for parts in directories)
        oracle = expected if len(actual) == 1 else actual
        return bool(oracle - pins)

    def stable_expression(self, tokens, line):
        tokens = unborrow(tokens)
        seen = set()
        while tokens and id(tokens[0]) not in seen:
            seen.add(id(tokens[0]))
            if len(tokens) == 1 and tokens[0].text == "(":
                tokens = tokens[0].children
                continue
            if (len(tokens) == 1 and tokens[0].kind == "word"
                    and tokens[0].text not in self.mutated and tokens[0].text not in self.parity_mutations):
                prior = [(pos, rhs) for pos, rhs in self.bindings.get(tokens[0].text, ()) if pos <= line]
                if len(prior) == 1 and prior[0][1] != tokens:
                    line, resolved = prior[0]
                    tokens = unborrow(resolved)
                    continue
            break
        return tokens

    def observed_result(self, operand, line):
        operand = self.stable_expression(operand, line)
        projection = ()
        receiver, method, args = method_call(operand)
        if method == "len" and not args:
            projection = ("len",)
            operand = self.stable_expression(receiver, line)
        while len(operand) > 2 and operand[-2].text == "." and operand[-1].kind == "word":
            projection = (operand[-1].text, *projection)
            operand = self.stable_expression(operand[:-2], line)
        return projection, operand

    def pipeline_call(self, tokens):
        while True:
            receiver, method, _ = method_call(tokens)
            if method not in ORACLE_METHODS:
                break
            tokens = receiver
        name, arguments = function_call(tokens)
        return (self.oracle_path(name, tokens[0].line), arguments) if name else ((), ())

    def identifier_strategy(self, tokens):
        if len(tokens) != 1 or tokens[0].text in self.mutated:
            return False
        strategy = self.strategies.get(tokens[0].text, ())
        path, arguments = self.pipeline_call(strategy)
        if path and not arguments:
            strategy = self.file_iterables.function(path)
        receiver, method, arguments = method_call(strategy)
        if (method == "prop_map" and len(arguments) == 4
                and shape(arguments)[::2] == ("|", "|")
                and arguments[1].kind == "word" and arguments[1].text == arguments[3].text):
            strategy = receiver
        value = literal(strategy)
        return isinstance(value, str) and bool(IDENTIFIER_STRATEGY.fullmatch(value))

    def source_text(self, tokens):
        value = literal(tokens)
        if isinstance(value, str):
            return value
        if len(tokens) == 1 and tokens[0].kind == "quoted":
            parts = output_fragments(tokens)
            return parts[0] if len(parts) == 1 else None
        if len(tokens) != 3 or not macro_at(tokens, 0, {"format"}):
            return None
        parts = list(split(tokens[-1].children))
        template = self.source_text(parts[0]) if parts else None
        if template is None:
            return None
        try:
            fields = list(Formatter().parse(template))
        except ValueError:
            return None
        if any(field not in (None, "") or spec or conversion for _, field, spec, conversion in fields):
            return None
        if (sum(field == "" for _, field, _, _ in fields) != len(parts) - 1
                or not all(self.identifier_strategy(self.stable_expression(arg, tokens[0].line)) for arg in parts[1:])):
            return None
        return "".join(span + ("\0" if field == "" else "") for span, field, _, _ in fields)

    def fixture(self, tokens, line):
        tokens = self.stable_expression(tokens, line)
        if not tokens:
            return ()
        source, converter = mapped_input(tokens)
        if qualified_name(converter)[-1:] == ("from_parsed_file",):
            tokens = self.stable_expression(source, tokens[0].line)
            if not tokens:
                return ()
        value = literal(tokens)
        if value in ([], (), ""):
            return "empty", type(value)
        if macro_at(tokens, 0, {"vec"}) and len(tokens) == 3:
            tokens = tokens[-1].children
        elif len(tokens) == 1 and tokens[0].text == "[":
            tokens = tokens[0].children
        path, args = self.pipeline_call(tokens)
        if path and path[-1] == "parse_file" and len(args) == 2:
            filename = literal(args[0])
            if not isinstance(filename, str) or not filename.endswith((".ts", ".tsx", ".js", ".jsx", ".py", ".rs")):
                return ()
            tokens = self.stable_expression(args[1], args[1][0].line)
        source = self.source_text(tokens)
        if source is not None and not IMPORT_TOKEN.search(source):
            # A generated identifier can spell a keyword, so only accept holes
            # in positions where they cannot form an import declaration.
            if "\0" not in source or re.fullmatch(
                    r"\s*function\s+\x00\(\)\s*\{\s*(?:\x00\(\);\s*)?\}\s*", source):
                return "import-free", source
        return ()

    def paired_result(self, operand):
        if len(operand) != 1 or operand[0].text in self.mutated:
            return None
        rhs, position = self.paired_bindings.get(operand[0].text, ((), 0))
        if not rhs or rhs[0].line >= operand[0].line:
            return None
        path, arguments = self.pipeline_call(rhs)
        bindings, result = local_returns(self.file_iterables.function(path))
        if len(result) != 1 or result[0].text != "(":
            return None
        pair = tuple(split(result[0].children))
        if len(pair) != 2:
            return None
        member = input_binding(pair[position], bindings)
        name, args = function_call(member)
        if not name:
            return None
        scope = path[:-1]
        callee = self.file_iterables.resolve(scope, name) or name
        empty_parameter = None
        if len(arguments) == 1 and self.fixture(arguments[0], rhs[0].line)[:1] == ("empty",):
            empty_parameter = self.parameters.get(path, {}).get(0)
        return PairedCall(rhs, position, callee, args, bindings, empty_parameter)

    def empty_parity(self, left, right, line):
        projection, first = left
        other_projection, second = right
        if projection != other_projection:
            return False
        left_call, left_args = self.pipeline_call(first)
        right_call, right_args = self.pipeline_call(second)
        if not left_call or not right_call:
            pair = self.paired_result(first)
            other = self.paired_result(second)
            return (pair and other and pair.invocation is other.invocation and pair.path != other.path
                    and pair.uses_empty_input() and other.uses_empty_input())
        if left_call == right_call or not left_args or len(left_args) != len(right_args):
            return False
        if any(shape(a) != shape(b) for a, b in zip(left_args[1:], right_args[1:])):
            return False
        one, two = (self.fixture(args[0], line) for args in (left_args, right_args))
        return bool(one and one == two and (one[0] == "empty" or IMPORT_FIELDS.intersection(projection)))

    def result_key(self, observation, line):
        projection, operand = observation
        path, args = self.pipeline_call(operand)
        if path:
            return path, projection, tuple(shape(self.stable_expression(arg, arg[0].line)) for arg in args if arg)
        if pair := self.paired_result(operand):
            return pair.path, projection, (id(pair.invocation), pair.position)
        return None

    def result_pins(self):
        for line, name, args, guarded in self.checks:
            if guarded:
                continue
            if name in {"assert", "debug_assert", "prop_assert"} and args:
                receiver, method, arguments = method_call(args[0])
                if method == "is_empty" and not arguments:
                    projection, call = self.observed_result(receiver, line)
                    if result := self.result_key(((*projection, "len"), call), line):
                        yield result
            left, right = self.equality(name, args)
            for operand, other in ((left, right), (right, left)):
                value = literal(self.stable_expression(other, line), signed_and_float=True)
                if value is not UNKNOWN:
                    if result := self.result_key(self.observed_result(operand, line), line):
                        yield result
                        if value in ([], ""):
                            path, projection, arguments = result
                            yield path, (*projection, "len"), arguments

    def wrapper_arguments(self, first, second, line):
        first_path, first_args = self.pipeline_call(first)
        second_path, second_args = self.pipeline_call(second)
        if first_path and second_path:
            return first_path, second_path, first_args, second_args, lambda arg: self.stable_expression(arg, arg[0].line if arg else line)
        pair, other = self.paired_result(first), self.paired_result(second)
        if pair and other and pair.invocation is other.invocation:
            return pair.path, other.path, pair.arguments, other.arguments, lambda arg: input_binding(arg, pair.bindings)
        return (), (), (), (), None

    def wrapper_parity(self, left, right, line, strings):
        first_projection, first = left
        second_projection, second = right
        one, two, first_args, second_args, resolve = self.wrapper_arguments(first, second, line)
        if not one or not two or one == two:
            return None
        for wrapper, base, wrapper_args, base_args in ((one, two, first_args, second_args),
                                                      (two, one, second_args, first_args)):
            if self.file_iterables.delegate(wrapper) != base:
                continue
            if first_projection != second_projection or len(wrapper_args) != len(base_args):
                return False
            for adapted, direct in zip(wrapper_args, base_args):
                adapted, direct = resolve(adapted), resolve(direct)
                source = converted_input(adapted)
                if shape(adapted) != shape(direct) and (not source or shape(resolve(source)) != shape(direct)):
                    return False
            if "path" in base[-1]:
                return self.composition({wrapper}, {base}, set(), strings)
            return True
        return None

    def literal_identity(self, left, right, line):
        first, second = (self.stable_expression(value, line) for value in (left, right))
        calls = self.oracle_calls(first, line)
        if not calls or len(calls) != 1 or calls != self.oracle_calls(second, line):
            return False
        path, first_args = self.pipeline_call(first)
        _, second_args = self.pipeline_call(second)
        if not path or len(first_args) != 1 or len(second_args) != 1:
            return False
        value = literal(self.stable_expression(first_args[0], first[0].line))
        transformed = self.stable_expression(second_args[0], second[0].line)
        receiver, method, args = method_call(transformed)
        if not isinstance(value, str) or not value.isascii():
            return False
        if not method or args or literal(self.stable_expression(receiver, line)) != value:
            inner, args = self.pipeline_call(transformed)
            if inner != path or len(args) != 1 or literal(self.stable_expression(args[0], line)) != value:
                return False
            body = self.file_iterables.function(path)
            receiver, method, args = method_call(body)
            if args or shape(receiver) != (self.parameters.get(path, {}).get(0),):
                return False
        return method in STRING_TRANSFORMS and getattr(value, STRING_TRANSFORMS[method])() == value

    def codec_parity(self, outer, expected, line, result_pins):
        projection, call = outer
        path, args = self.pipeline_call(call)
        if projection or len(args) != 1 or not args[0]:
            return False
        inner = self.stable_expression(args[0], args[0][0].line)
        inner_path, inner_args = self.pipeline_call(inner)
        if (not inverse_codecs(path, inner_path) or len(inner_args) != 1 or not inner_args[0]
                or not all(self.file_iterables.production(callee)
                           and self.file_iterables.function(callee, external=True)
                           for callee in (path, inner_path))):
            return False
        if self.result_key(((), inner), line) in result_pins:
            return False
        argument = self.stable_expression(inner_args[0], inner_args[0][0].line)
        return shape(argument) == shape(self.stable_expression(expected, line))

    def coupled(self, left, right, line, pins, strings, result_pins):
        first, second = (self.observed_result(operand, line) for operand in (left, right))
        parity = (True if self.empty_parity(first, second, line)
                  else self.wrapper_parity(first, second, line, strings))
        if parity is not None:
            return parity and not any(self.result_key(observation, line) in result_pins
                                      for observation in (first, second))
        if self.codec_parity(first, right, line, result_pins) or self.codec_parity(second, left, line, result_pins):
            return True
        actual = self.oracle_calls(left, line)
        expected = self.oracle_calls(right, line)
        return (self.composition(actual, expected, pins, strings)
                and not (self.uses_operand(left, right, line) or self.uses_operand(right, left, line))
                or self.shared_constant(actual, left, right, line, pins)
                or self.shared_constant(expected, right, left, line, pins)
                or self.literal_identity(left, right, line)
                or self.literal_identity(right, left, line))

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

    def order_without_cardinality(self, line, name, args, check_guarded):
        if line in self.window_order:
            subject = self.window_order[line]
        elif name.endswith("_eq") and len(args) >= 2:
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
                    start = (source[0].line, source[0].column)
                    end = (sorted_copy[0].line, sorted_copy[0].column)
                    if any(start < loc < end and not params.children
                           for sort in ("sort", "sort_unstable")
                           for loc, params, _ in self.calls.get((sorted_copy[0].text, ".", sort), ())):
                        subject = source
                        break
        elif name in {"assert", "debug_assert", "prop_assert"}:
            subject, method, parts = method_call(self.resolve(args[0], line))
            if method == "all":
                subject, method, width = method_call(subject)
                if method != "windows" or integer_literal(width) != 2 or not parts:
                    return False
            elif method in {"is_sorted", "is_sorted_by", "is_sorted_by_key"}:
                receiver, iterator, params = method_call(subject)
                if iterator in {"iter", "into_iter"} and not params:
                    subject = receiver
            else:
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
        for other_line, other_name, other_args, guarded in self.checks:
            if other_line == line or not other_args or (guarded and not check_guarded):
                continue
            if other_name.endswith("_eq") and len(other_args) >= 2:
                first, second = other_args[:2]
                if all(method_call(arg)[1] == "len" for arg in (first, second)):
                    continue
                if (any(shape(measured) == (*shape(subject), ".", "len", "(")
                        and integer_literal(expected) in {2, 3}
                        for measured, expected in ((first, second), (second, first)))
                        and self.sorts_hash_iteration(self.resolve(subject, line), line)):
                    continue
            if any(token.text in subjects for argument in other_args[:2]
                   for token in check_arguments(argument) if token.kind == "word"):
                return False
        return True

    def sorts_hash_iteration(self, value, line):
        callee, _ = function_call(value)
        _, _, body = self.call_definition(callee, line)
        if len(body) < 2 or body[-1].kind != "word" or body[-2].text != ";":
            return False
        result = body[-1].text
        bindings = {}
        for offset, token in enumerate(body[:-1]):
            if token.text != "let":
                continue
            head = next(split(body[offset + 1:], ";"), ())
            if shape(head[:1]) == ("mut",):
                head = head[1:]
            parts = list(split(head, "="))
            if len(parts) == 2 and all(parts) and parts[0][0].kind == "word":
                bindings[parts[0][0].text] = parts
        if result not in bindings:
            return False
        _, initialization = bindings[result]
        receiver, collection, args = method_call(initialization)
        parent, iteration, inputs = method_call(receiver)
        if (collection != "collect" or args or iteration not in {"into_iter", "iter", "keys", "into_keys"}
                or inputs or len(parent) != 1 or parent[0].text not in bindings):
            return False
        definition = bindings[parent[0].text]
        if not any(token.text in {"HashSet", "HashMap"} for part in definition for token in part):
            return False
        return any(shape(body[offset:offset + 4]) in {
                       (result, ".", "sort", "("),
                       (result, ".", "sort_unstable", "("),
                   } for offset in range(len(body) - 3)
                   if (body[offset].line, body[offset].column) >
                   (initialization[0].line, initialization[0].column))

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
        for line, name, args, guarded in self.checks:
            if name not in ASSERTIONS or not args:
                continue
            boolean = name in {"assert", "debug_assert", "prop_assert"}
            if (boolean and self.fixed_output_check(args[0], line)
                    or self.order_without_cardinality(line, name, args, guarded)):
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
        for callee, group in self.symmetric_calls.items():
            peers = [(shapes, values) for _, _, args, _, values, shapes in group
                     if id(args) in checked and id(args) not in self.expected]
            for name, line, args, legacy, _, shapes in group:
                if not symmetric_input(name, legacy, shapes, peers, observed=id(args) in direct):
                    continue
                if id(args) not in checked and id(args) not in observed:
                    _, _, body = self.call_definition(callee, line)
                    if shape(body[:2]) == ("format", "!") or name in self.bindings:
                        continue
                yield line
        yield from unvaried_arguments(
            unvaried, lambda callee, line, values: self.fixed_operands(callee, line, values, direct))
        yield from named_arguments(coverage, claims, self.parameters_for)
        yield from loose_bounds(bound_checks)

    def call_definition(self, callee, line):
        index = self.file_iterables
        name = tuple(part for part in callee or () if part != "::")
        if not name or "." in name or "!" in name:
            return index, (), ()
        path = self.oracle_path(name, line)
        if path in index.function_sources:
            return index, path, index.function_sources[path]
        for depth in range(len(path) - 1, 0, -1):
            source = index.module_file(path[:depth])
            if source and (module := module_items(source)):
                local = path[depth:]
                if local in module.function_sources:
                    return module, local, module.function_sources[local]
        return index, (), ()

    def fixed_operands(self, callee, line, fixed, direct):
        if "." in callee:
            return ()
        index, path, body = self.call_definition(callee, line)
        if not path:
            return fixed.values()
        names = index.parameters.get(path, {})
        if path[-1] == "new" and len(path) > 1 and not any(
                id(arguments) in direct for _, arguments, _ in self.calls[callee]):
            operations = stored_operands(body, (method for owner, method in index.method_sources.items()
                                                if owner[:-1] == path[:-1] and owner != path))
        else:
            operations = defaultdict(set)
            destructured = {}
            for offset in range(len(body) - 4):
                if (body[offset].text == "let" and body[offset + 1].text == "("
                        and body[offset + 2].text == "=" and body[offset + 4].text == ";"):
                    for name in split(body[offset + 1].children):
                        if len(name) == 1 and name[0].kind == "word":
                            destructured[name[0].text] = body[offset + 3].text
            for operand, operator in arithmetic_operands(body):
                name = destructured.get(operand[0], operand[0])
                operations[name].add(operator)
        return [value for position, value in fixed.items()
                if fixed_operand(value, operations.get(names.get(position), set()))]

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

    def alias_path(self, operand, location):
        receiver, method, args = method_call(operand)
        path = binding_path(receiver if method == "clone" and not args else operand)
        if not path:
            return ()
        root = path[0]
        binding = self.alias_bindings.get(root)
        if not binding or len(self.bindings[root]) != 1:
            return path
        start, value = binding
        if start >= location:
            return path
        if shape(value[:2]) == ("&", "mut"):
            return path
        value = unborrow(value)
        receiver, method, args = method_call(value)
        value = receiver if method == "clone" and not args else value
        source = binding_path(value)
        if not source:
            return path
        original = self.alias_path(value, start)
        if original and not any(
                start < change < location for name in {root, source[0], original[0]}
                for change in self.binding_changes[name]):
            return (*original, *path[1:])
        return path

    def self_comparison(self, op, left, right, line, test_name, line_comments):
        if op not in {"==", "!="} or not left or not right:
            return False
        if shape(left) != shape(right):
            location = (left[0].line, left[0].column)
            if (left[0].text == "&") == (right[0].text == "&"):
                left, right = unborrow(left), unborrow(right)
            path = self.alias_path(left, location)
            return bool(path) and path == self.alias_path(right, location)
        if op != "==" or not contains_call(left):
            return True
        comment = " ".join(line_comments.get(loc, "") for loc in (line - 1, line))
        if DISTINCT_INPUTS.search(test_name) or DISTINCT_INPUTS.search(comment):
            return True
        if REPEATABILITY.search(test_name) or REPEATABILITY.search(comment):
            return False
        for other_line, name, args, _ in self.checks:
            if other_line == line:
                continue
            operator, first, second = assertion_comparison(name, args)
            for call, other in ((first, second), (second, first)):
                if shape(call) != shape(left):
                    continue
                expected = self.resolve(other, other_line)
                if (operator == "!=" and shape(other) != shape(call)) or (operator == "==" and (
                        literal(expected, signed_and_float=True) is not UNKNOWN
                        or literal_size(expected) is not None)):
                    return False
        return True

    @staticmethod
    def collection_input(tokens):
        while True:
            receiver, method, _ = method_call(tokens)
            if method not in {"iter", "into_iter", "filter", "filter_map", "map", "collect", "copied", "cloned"}:
                return tokens
            tokens = receiver

    def collection_origins(self, operand, line, present):
        operand = unborrow(operand)
        if binding_path(operand) and operand[0].text in self.mutated:
            return
        value = self.resolve(operand, line)
        if present:
            value = self.collection_input(value)
        if path := binding_path(value):
            if path[0] in self.mutated:
                return
            value = self.resolve(value, line)
        if not value or value[-1].text != "(":
            return
        name = qualified_name(value[:-1])
        if not name or len(name) == 1 and name[0] in self.oracle_locals:
            return
        origin = self.file_iterables.resolve(self.scope, name)
        if origin not in self.file_iterables.collection_sources:
            return
        yield origin
        if present:
            result = self.file_iterables.function(origin)
            source = self.collection_input(result)
            parameters = self.parameters.get(origin, {})
            args = list(split(value[-1].children))
            if source != result and len(source) == 1 and len(args) == 1 and (
                    source[0].text == parameters.get(0)):
                yield from self.collection_origins(args[0], line, True)

    def collection_observations(self):
        for line, name, args, guarded in self.checks:
            if not args or guarded:
                continue
            op, left, right = assertion_comparison(name, args)
            candidates = []
            if not op and name in ASSERTIONS:
                predicate = args[0]
                negated = predicate[0].text == "!"
                receiver, method, params = method_call(predicate[1:] if negated else predicate)
                if method == "is_empty" and not params:
                    candidates.append((receiver, negated))
                elif method in {"contains", "contains_key", "any", "is_some"} and not negated:
                    if method == "is_some":
                        receiver, search, _ = method_call(receiver)
                        if search not in {"find", "first", "last", "get"}:
                            continue
                    candidates.append((receiver, True))
            reverse = {">": "<", ">=": "<=", "<": ">", "<=": ">="}
            for operand, other, ordered in ((left, right, op), (right, left, reverse.get(op, op))):
                receiver, method, params = method_call(operand)
                size = integer_literal(other) if method == "len" and not params else literal_size(other)
                if size is None:
                    continue
                if ordered == "==" or ordered in {"!=", ">"} and size == 0 or ordered == ">=" and size > 0:
                    candidates.append((receiver if method == "len" else operand, ordered != "==" or size > 0))
            yield from ((operand, present, line) for operand, present in candidates)

    def collection_checks(self, observations=None):
        if observations is None:
            observations = self.collection_observations()
        for operand, present, line in observations:
            for path in self.collection_origins(operand, line, present):
                yield path, present, line

    def sweep_input(self, operand, line):
        value = self.resolve(unborrow(operand), line)
        receiver, method, _ = method_call(value)
        if not method and shape(value[-2:]) == (">", "("):
            for index in range(len(value) - 2, 0, -1):
                if (shape(value[index - 1:index + 1]) == (".", "collect")
                        and invocation_at(value, index, generic=True) == (value[-1], False)):
                    receiver, method = value[:index - 1], "collect"
                    break
        if method != "collect":
            return ()
        filtered = False
        while receiver:
            value = receiver
            receiver, method, _ = method_call(value)
            filtered |= method in {"filter", "filter_map"}
            if method not in {"iter", "into_iter", "filter", "filter_map", "map", "flatten",
                              "copied", "cloned", "enumerate", "unwrap", "expect"}:
                break
        if not filtered:
            return ()
        if (source := binding_path(value)) and source[0] in self.mutated:
            return ()
        return self.resolve(unborrow(value), line)

    def runtime_inventory(self, tokens):
        while tokens:
            receiver, method, _ = method_call(tokens)
            if method in {"read_dir", "glob", "glob_with"}:
                return True
            if method in {"unwrap", "expect", "unwrap_or_default", "flatten", "map", "filter",
                          "filter_map", "collect", "into_iter"}:
                tokens = receiver
                continue
            if tokens[-1].text == "(":
                name = qualified_name(tokens[:-1])
                return bool(name and (name[-1] in {"read_dir", "glob", "glob_with"}
                                      or name[-2:] == ("WalkDir", "new")))
            break
        return False

    def runtime_sweep_checks(self, observations):
        witnessed = {self.resolve(unborrow(operand), line) for operand, present, line in observations if present}
        for operand, present, line in observations:
            if not present and (source := self.sweep_input(operand, line)) and source not in witnessed:
                if self.runtime_inventory(source):
                    yield line

    def checked_iteration_sources(self):
        for line, name, args, guarded in self.checks:
            if guarded or name not in ASSERTIONS or not args:
                continue
            receiver, method, _ = method_call(args[0])
            if method != "contains":
                continue
            value = self.resolve(receiver, line)
            while value:
                base, method, _ = method_call(value)
                if method == "iter":
                    path = binding_path(base)
                    if len(path) == 1 and path[0] not in self.mutated:
                        yield path[0]
                    break
                if method not in {"collect", "map"}:
                    break
                value = base

    def recorder_origin(self, name, line, evidence, scope):
        if name in self.writes or len(self.bindings[name]) != 1:
            return ()
        inputs = self.recorder_inputs[name]
        if not inputs or not any(call_line <= line for _, call_line in inputs):
            return ()
        keys = {key for key, _ in inputs}
        if len(keys) != 1:
            return ()
        (callee, slot), = keys
        if len(callee) == 3 and callee[1] == ".":
            receiver, _, method = callee
            owners = {evidence.receiver_type(receiver, scope, loc) for loc, _, _ in self.calls[callee]}
            if len(owners) != 1 or not (owner := owners.pop()):
                return ()
            path = (*owner, method)
            if path not in evidence.methods and owner not in self.file_iterables.type_names:
                imported = [(*module, owner[-1], method)
                            for module in self.file_iterables.globs.get(owner[:-1], ())
                            if (*module, owner[-1], method) in evidence.methods]
                if len(imported) == 1:
                    path, = imported
            if path not in evidence.methods:
                return ()
        elif "." not in callee and callee[0] not in self.oracle_locals:
            path = evidence.resolve(scope, tuple(part for part in callee if part != "::"), False)
        else:
            return ()
        return (path, slot) if path in self.file_iterables.function_sources else ()

    def issues(self, test, pins, strings, line_comments, evidence, recorder_witnesses, recorder_absences):
        test_name = test.name
        scope = tuple(test.identifier.split("::"))
        findings = [(line, "nondistinguishing-literal") for line in self.argument_calls(test_name)]
        result_pins = set(self.result_pins())
        initializers = {name: recorder_initializer(bindings[0][1]) for name, bindings in self.bindings.items()}
        recorders = {name for name, kind in initializers.items() if kind == "vector"}
        negative = {}
        positive = defaultdict(list)
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
                    positive[args[0][0].text].append(line)
                if shape(args[0])[:1] == ("!",) and args[0][1:]:
                    expression = shape(args[0][1:])
                    if expression[0] in recorders and expression[1:] == (".", "is_empty", "("):
                        positive[expression[0]].append(line)
                receiver, method, _ = method_call(args[0])
                if method == "any":
                    base, iterator, params = method_call(receiver)
                    if (iterator == "iter" and not params and len(base) == 1
                            and base[0].text in recorders):
                        positive[base[0].text].append(line)
                expression = shape(args[0])
                if expression and expression[0] in recorders and expression[1:] == (".", "is_empty", "("):
                    negative.setdefault(expression[0], []).append(line)
            else:
                continue
            if self.self_comparison(op, left, right, line, test_name, line_comments):
                findings.append((line, "self-compare"))
            if op == "==" and self.coupled(left, right, line, pins, strings, result_pins):
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
                    positive[subject].append(line)
        controlled = {producer for name, lines in positive.items()
                      for producer, call_line in self.recorder_inputs[name]
                      if name not in self.writes
                      and all(key == producer for key, _ in self.recorder_inputs[name])
                      and any(call_line <= line for line in lines)}
        unguarded = {line for line, _, _, guarded in self.checks if not guarded}
        for name, lines in positive.items():
            for line in lines:
                if line in unguarded and (origin := self.recorder_origin(name, line, evidence, scope)):
                    recorder_witnesses.add(origin)
        checked_sources = set(self.checked_iteration_sources())
        for name in recorders - positive.keys():
            if name in negative:
                sites = self.collector_writes[name]
                failure_collector = sites and not self.recorder_inputs[name] and all(
                    mismatch and checked_sources.issuperset(path) for path, mismatch in sites)
                if not failure_collector:
                    for line in negative[name]:
                        if any(key in controlled and call_line <= line
                               for key, call_line in self.recorder_inputs[name]):
                            continue
                        origin = self.recorder_origin(name, line, evidence, scope)
                        if origin:
                            recorder_absences.append((test, line, origin))
                        else:
                            findings.append((line, "absence-no-control"))
            elif name in self.writes and name not in self.reads:
                findings.append((self.writes[name], "absence-no-control"))
        for name, (line, kind) in self.callback_writes.items():
            if initializers.get(name) == kind and name not in self.reads:
                findings.append((line, "absence-no-control"))
        return findings, measured


def setup_only(tokens, evidence, scope, assertion_count):
    checked = set()
    direct_assertions = 0
    unchecked = False
    for statement in split(tokens, ";"):
        if not statement:
            continue
        if macro_at(statement, 0, ASSERTIONS):
            direct_assertions += 1
            if unchecked:
                return False
            name = statement[0].text
            args = list(split(statement[2].children))
            for operand in (args[:2] if name.endswith(("_eq", "_ne")) else args[:1]):
                if not operand:
                    return False
                operand = operand[1:] if operand[0].text == "!" else operand
                receiver, method, params = method_call(operand)
                if method in {"is_empty", "len", "exists", "is_dir", "is_file"} and not params:
                    operand = receiver
                if path := binding_path(operand):
                    checked.add(path[0])
                elif literal(operand, signed_and_float=True) is UNKNOWN:
                    return False
            continue
        if not checked:
            continue
        if evidence.checks(statement, scope):
            return False
        if statement[0].text in {"let", "const"}:
            declaration = list(split(statement[1:], "="))
            if len(declaration) != 2 or shape(declaration[0]) != ("_",):
                continue
            statement = declaration[1]
        if statement[0].text in {"if", "for", "while", "match", "loop", "return"}:
            continue
        if macro_at(statement, 0, {"println", "print", "eprintln", "eprint", "trace", "debug", "info", "warn", "error"}):
            unchecked |= contains_call(statement[2].children)
        elif contains_call(statement):
            unchecked |= any(token.text in checked for token in check_arguments(statement))
    return unchecked and direct_assertions == assertion_count


def success_check(name, args):
    if not args:
        return False
    operator, left, right = assertion_comparison(name, args)
    if not operator and name in {"assert", "debug_assert", "prop_assert"}:
        _, method, parameters = method_call(args[0])
        return (method in {"is_ok", "success"} and not parameters
                and args[0][0].text != "!" and not any(token.text in {"&&", "||"} for token in args[0]))
    if operator != "==":
        return False
    for status, expected in ((left, right), (right, left)):
        _, method, parameters = method_call(status)
        if method == "code" and not parameters:
            if (len(expected) == 2 and shape(expected[:1]) == ("Some",)
                    and expected[1].text == "(" and integer_literal(expected[1].children) == 0):
                return True
    return False


def has_outcome_evidence(test, inspector, body, evidence):
    scope = tuple(test.identifier.split("::"))
    tokens = body.children
    if not evidence.checks(tokens, scope):
        return False
    assertions = [(name, args) for _, name, args, _ in inspector.checks if name in ASSERTIONS]
    if (OUTCOME_CLAIM.search(test.name) and assertions
            and all(name in {"assert", "debug_assert", "prop_assert"} and args
                    and method_call(args[0])[1] == "is_ok" and success_check(name, args)
                    for name, args in assertions)
            and all(name in ASSERTIONS or name == "expect" for _, name, _, _ in inspector.checks)):
        return False
    if claims_effect(test.name) and not evidence.checks(tokens, scope, ignore_status=True):
        return False
    return not setup_only(tokens, evidence, scope, len(assertions))


def witnessed_by_helpers(inspected, evidence, file_iterables, parameters, missing):
    visited = set()
    for test, inspector, _, ignored, _ in inspected:
        if ignored:
            continue
        scope = tuple(test.identifier.split("::"))
        callees = set(inspector.calls)
        callees.update((name,) for _, name, _, _ in inspector.checks)
        for callee in callees:
            name = tuple(part for part in callee if part != "::")
            if not name or "." in name or name[0] in inspector.oracle_locals:
                continue
            path = evidence.resolve(scope, name, False)
            if path in visited or (False, path) not in evidence.known or path in evidence.methods:
                continue
            visited.add(path)
            tokens = evidence.functions[path]
            if not any(token.text in missing for token in walk_tokens(tokens)):
                continue
            namespace = evidence.module_scope(path)
            helper = Inspect(file_iterables.aliases.get(namespace, {}), frozenset(), file_iterables,
                             namespace, set(mutated_names(tokens)), parameters, {})
            for local in declared_bindings(tokens):
                helper.oracle_locals.setdefault(local.text, local.line)
            helper.visit(tokens)
            yield from (producer for producer, present, _ in helper.collection_checks() if present)


def parse(source, path, *, scan_root=None, collection_controls=None, inventory=False):
    tokens, comments = tokenize(source)
    line_comments = {comment.line: comment.text for comment in comments}
    tests = []
    grouped = group(tokens)
    found = list(test_functions(grouped))
    if not found:
        return tests, comments
    test_bodies = {id(row[4]) for row in found}
    inspected = []
    helpers = {}
    parameters = dict(file_parameters(grouped))
    file_iterables = FileIterables(grouped, path, parameters)
    evidence = CheckEvidence(file_iterables, found)
    macro_tests = set(macro_template_tests(grouped))
    for token, name, identifier, start, body, panic, ignored, aliases, scope, header, column in found:
        if scope is not None and id(scope) not in helpers:
            helpers[id(scope)] = checking_helpers(scope, test_bodies)
        previous = {c.line for c in comments if c.standalone and c.column == token.column}
        while start - 1 in previous:
            start -= 1
            column = token.column
        test = Test(name, identifier, token.line, start, body.end, token.column)
        namespace = tuple(identifier.split("::")[:-1])
        inspector = Inspect(aliases, helpers.get(id(scope), frozenset()), file_iterables,
                            namespace, set(mutated_names(body.children)), parameters,
                            dict(parameter_names(body.children)))
        for local in declared_bindings(body.children):
            inspector.oracle_locals.setdefault(local.text, local.line)
        inspector.strategies = {argument[0].text: argument[2:] for argument in split(header, type_arguments=True)
                                if len(argument) >= 3 and argument[0].kind == "word" and argument[1].text == "in"}
        inspector.parity_mutations = growth_receivers(body.children)
        inspector.visit(body.children)
        if inventory:
            test.qualified = identifier
            test.span_columns = column, body.end_column
            test.body_hash = hashlib.sha256(repr(shape(body.children)).encode()).hexdigest()
            test.checks = len(inspector.checks)
        inspected.append((test, inspector, panic, ignored, body))
    pins = {pin for _, inspector, _, _, _ in inspected for pin in inspector.literal_pins()}
    observations = [list(inspector.collection_observations()) if not ignored else []
                    for _, inspector, _, ignored, _ in inspected]
    collections = [list(inspector.collection_checks(checks))
                   for (_, inspector, _, _, _), checks in zip(inspected, observations)]
    witnessed = {path for checks in collections for path, present, _ in checks if present}
    missing = {path[-1] for checks in collections for path, present, _ in checks
               if not present and path not in witnessed}
    if missing:
        witnessed.update(witnessed_by_helpers(inspected, evidence, file_iterables, parameters, missing))
    fingerprints = {}
    if collection_controls is not None:
        for producer in witnessed | {producer for checks in collections for producer, _, _ in checks}:
            fingerprints[producer] = producer[-1], shape(file_iterables.function_sources[producer])
        for producer in witnessed:
            collection_controls.record(path, scan_root, fingerprints[producer])
    recorder_witnesses = set()
    recorder_absences = []
    for (test, inspector, panic, ignored, body), collection_checks, checks in zip(inspected, collections, observations):
        strings = {value for token in walk_tokens(body.children)
                   if token.kind in {"quoted", "raw"} and isinstance(value := literal((token,)), str)}
        test.issues, test.measured = inspector.issues(
            test, pins, strings, line_comments, evidence,
            set() if ignored else recorder_witnesses, recorder_absences,
        )
        reported = set()
        for producer, present, line in collection_checks:
            if not present and producer not in witnessed and producer not in reported:
                if collection_controls is None:
                    test.issues.append((line, "absence-no-control"))
                else:
                    collection_controls.record(path, scan_root, fingerprints[producer], test, line)
                reported.add(producer)
        test.issues.extend((line, "absence-no-control") for line in inspector.runtime_sweep_checks(checks))
        if not (panic or ignored or id(body) in macro_tests or has_outcome_evidence(test, inspector, body, evidence)):
            test.issues.append((test.line, "no-assert"))
        ignored_tool = (scan_root is not None and ignored
                        and all(name == "expect" for _, name, _, _ in inspector.checks))
        test.guarded = bool(inspector.checks) and not (panic or ignored_tool) and all(
            guarded for _, _, _, guarded in inspector.checks)
        if test.guarded and scan_root is not None:
            from . import rust_guard

            mutated = inspector.mutated | set(rust_guard.possibly_drained(body.children))
            guard_inspector = Inspect(inspector.aliases, inspector.helpers, file_iterables,
                                      inspector.scope, mutated, parameters, inspector.local_parameters)
            guard_inspector.strategies = inspector.strategies
            guard_inspector.guard_iterables = rust_guard.GuardIterables(file_iterables, scan_root)
            strategy_locals = {name: rust_guard.strategy_facts(strategy)
                               for name, strategy in inspector.strategies.items()
                               if name not in inspector.mutated}
            guard_inspector.visit(body.children, inherited=strategy_locals or None)
            test.guarded = all(guarded for _, _, _, guarded in guard_inspector.checks)
        test.conditional = inspector.assumes
        tests.append(test)
    for test, line, origin in recorder_absences:
        if origin not in recorder_witnesses:
            test.issues.append((line, "absence-no-control"))
    return tests, comments
