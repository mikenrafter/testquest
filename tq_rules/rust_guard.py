"""Static iteration facts used only by guarded-assert."""

from dataclasses import dataclass
from functools import lru_cache
import operator
import re
import tomllib

from .rust import (FileIterables, absolute_path, comparison, directly_checks, group,
                   integer_literal, literal, literal_size, method_call, qualified_name,
                   shape, split, tokenize, unborrow, walk_tokens)
from .source import project_root


@dataclass(frozen=True)
class Iterable:
    minimum: int
    length: int | None = None
    rows: tuple = ()


ARITHMETIC = {"+": operator.add, "-": operator.sub, "*": operator.mul,
              "/": operator.floordiv}
COMPARISONS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
               "<=": operator.le, ">": operator.gt, ">=": operator.ge}
EXITS = {"break", "continue", "return", "?"}


def integer(tokens, lookup):
    value = integer_literal(tokens)
    if value is not None:
        return value
    if len(tokens) == 1 and tokens[0].text == "(":
        return integer(tokens[0].children, lookup)
    for operators in ({"+", "-"}, {"*", "/"}):
        for index in range(len(tokens) - 2, 0, -1):
            op = tokens[index].text
            if op in operators:
                left = integer(tokens[:index], lookup)
                right = integer(tokens[index + 1:], lookup)
                if None not in (left, right) and (op != "/" or left >= 0 and right > 0):
                    return ARITHMETIC[op](left, right)
                return None
    receiver, method, args = method_call(tokens)
    if method == "len" and not args:
        facts = iterable(receiver, lookup)
        return facts.length if facts else None
    return lookup(tokens, "number")


def range_parts(tokens):
    if len(tokens) == 1 and tokens[0].text == "(":
        return range_parts(tokens[0].children)
    for index in range(len(tokens) - 1):
        if shape(tokens[index:index + 2]) == (".", "."):
            upper = tokens[index + 2:]
            inclusive = bool(upper) and upper[0].text == "="
            return tokens[:index], upper[inclusive:], inclusive
    return None


def range_bounds(tokens, lookup):
    if parts := range_parts(tokens):
        lower, upper, inclusive = parts
        start, end = integer(lower, lookup), integer(upper, lookup)
        if None not in (start, end):
            return start, end + inclusive
    return None


def elements(tokens):
    tokens = unborrow(tokens)
    if len(tokens) == 1 and tokens[0].text == "[":
        return tokens[0].children
    if len(tokens) == 3 and shape(tokens[:2]) == ("vec", "!") and tokens[2].text in {"[", "(", "{"}:
        return tokens[2].children
    return ()


def iterable(tokens, lookup, *, vector=False):
    tokens = unborrow(tokens)
    size = literal_size(tokens, lambda count: integer(count, lookup))
    if size is not None:
        entries = elements(tokens)
        rows = ()
        if entries and len(list(split(entries, ";"))) == 1:
            rows = tuple(split(entries))
        return Iterable(size, size, rows)
    if len(tokens) == 1:
        token = tokens[0]
        if token.text == "(":
            return iterable(token.children, lookup)
        if token.kind == "raw" and token.text.startswith("br"):
            size = len(literal(tokens))
            return Iterable(size, size)
        if token.kind == "quoted" and token.text.startswith('b"'):
            content = token.text[2:-1]
            if re.fullmatch(r"(?:\\\r?\n[ \t\r\n]*)*", content):
                return Iterable(0, 0)
            return Iterable(1)
    if len(tokens) >= 2 and tokens[-1].text == "[":
        base = iterable(tokens[:-1], lookup)
        parts = range_parts(tokens[-1].children)
        if parts and base and not parts[1] and not parts[2]:
            if not parts[0]:
                return base
            start = integer(parts[0], lookup)
            if start is not None and 0 <= start <= base.minimum:
                length = base.length - start if base.length is not None else None
                return Iterable(base.minimum - start, length)
    if shape(tokens) == ("Vec", "::", "new", "("):
        return Iterable(0, 0)
    if tokens and tokens[0].text == "if":
        blocks = [part for part in tokens if part.text == "{"]
        if len(blocks) == 2 and tokens[-2].text == "else" and tokens[-1] is blocks[-1]:
            alternatives = [iterable(block.children, lookup) for block in blocks]
            if all(item is not None for item in alternatives):
                return Iterable(min(item.minimum for item in alternatives))
    if bounds := range_bounds(tokens, lookup):
        size = max(0, bounds[1] - bounds[0])
        return Iterable(size, size)
    if parts := range_parts(tokens):
        lower, upper, inclusive = parts
        start = integer(lower, lookup)
        end = lookup(upper, "lower")
        if None not in (start, end):
            return Iterable(max(0, end - start + inclusive))
    receiver, method, args = method_call(tokens)
    if method:
        return adapted(receiver, method, args, lookup, vector=vector)
    if tokens and tokens[-1].text == "(":
        return lookup(tokens[:-1], "literal_helper")
    return lookup(tokens, "iterable")


def adapted(receiver, method, args, lookup, *, vector):
    if method in {"iter", "iter_mut", "into_iter", "rev", "copied", "cloned"} and not args:
        return iterable(receiver, lookup)
    if method == "step_by" and (step := integer_literal(args)) is not None and step > 0:
        facts = iterable(receiver, lookup)
        if facts:
            length = (facts.length + step - 1) // step if facts.length is not None else None
            return Iterable((facts.minimum + step - 1) // step, length, facts.rows[::step])
    if method == "enumerate" and not args:
        facts = iterable(receiver, lookup)
        if facts:
            return Iterable(facts.minimum, facts.length,
                            tuple((None, row) for row in facts.rows))
    if method == "collect" and not args or method == "map" and args:
        facts = iterable(receiver, lookup)
        if facts:
            if method == "collect" and not vector:
                return Iterable(min(facts.minimum, 1))
            return Iterable(facts.minimum, facts.length)
    if method == "clone" and not args:
        return iterable(receiver, lookup)
    if method == "concat" and not args and (facts := iterable(receiver, lookup)) and facts.rows:
        parts = [iterable(row, lookup) for row in facts.rows]
        if all(part is not None for part in parts):
            length = sum(part.length for part in parts) if all(part.length is not None for part in parts) else None
            return Iterable(sum(part.minimum for part in parts), length)
    if method in {"bytes", "chars"} and not args:
        value = literal(receiver)
        if isinstance(value, str):
            return Iterable(int(bool(value)))
        return lookup(receiver, "text")
    if method == "zip":
        left, right = iterable(receiver, lookup), iterable(args, lookup)
        if left and right:
            length = min(left.length, right.length) if None not in (left.length, right.length) else None
            return Iterable(min(left.minimum, right.minimum), length)
    if method == "take" and (limit := integer(args, lookup)) is not None and limit >= 0:
        base, adapter, adapter_args = method_call(receiver)
        if adapter == "cycle" and not adapter_args:
            facts = iterable(base, lookup)
            if facts and facts.minimum:
                return Iterable(limit, limit)
        facts = iterable(receiver, lookup)
        if facts:
            return Iterable(min(limit, facts.minimum),
                            min(limit, facts.length) if facts.length is not None else None)
    return None



def extend_local(locals_here, name, added, kind="iterable"):
    facts = locals_here.get(name, {}).get(kind)
    if facts is not None and added is not None:
        length = (facts.length + added.length
                  if None not in (facts.length, added.length) else None)
        locals_here[name] = {kind: Iterable(facts.minimum + added.minimum, length)}


def direct_insertions(tokens):
    for index, token in enumerate(tokens[:-3]):
        if index and tokens[index - 1].text not in {";", "{"}:
            continue
        dot, method = shape(tokens[index + 1:index + 3])
        if token.kind == "word" and dot == "." and method in {"push", "spawn"}:
            args = tokens[index + 3]
            if args.text == "(" and len(list(split(args.children))) == 1:
                yield token.text, "iterable" if method == "push" else "join_set"


def join_set_receiver(tokens):
    if len(tokens) == 10 and shape(tokens[:2]) == ("let", "Some") and tokens[2].text == "(":
        name = tokens[4].text
        if shape(tokens[3:]) == ("=", name, ".", "join_next", "(", ".", "await"):
            return name
    return ""


def possibly_drained(tokens, joins=None):
    if joins is None:
        joins = set()
    for index, token in enumerate(tokens):
        if token.kind == "group":
            yield from possibly_drained(token.children, joins)
        if token.kind == "word" and index + 3 < len(tokens):
            dot, method = shape(tokens[index + 1:index + 3])
            if dot != "." or tokens[index + 3].text != "(":
                continue
            if method in {"join_all", "shutdown", "detach_all", "try_join_next", "join_next_with_id"}:
                yield token.text
            elif method == "join_next":
                if token.text in joins:
                    yield token.text
                joins.add(token.text)

def literal_tail(tokens):
    statements = list(split(tokens, ";"))
    if not statements or tokens[-1].text == ";":
        return ()
    if any(not statement or statement[0].text != "let" for statement in statements[:-1]):
        return ()
    if any(token.text in EXITS for token in walk_tokens(tokens)):
        return ()
    tail = statements[-1]
    return tail


def strategy_facts(tokens):
    if bounds := range_bounds(tokens, lambda *_: None):
        return {"lower": bounds[0]} if bounds[0] < bounds[1] else {}
    if tokens and tokens[-1].text == "(" and qualified_name(tokens[:-1]) in {
            ("prop", "collection", "vec"), ("proptest", "collection", "vec")}:
        args = list(split(tokens[-1].children))
        if len(args) == 2 and (bounds := range_bounds(args[1], lambda *_: None)):
            if 0 <= bounds[0] < bounds[1]:
                return {"iterable": Iterable(bounds[0])}
    return {}


def arm_checks(tokens, reach):
    seen = set()
    for arm in split(tokens):
        pieces = list(split(arm, "=>"))
        if len(pieces) != 2:
            return False
        pattern, body = pieces
        value = integer_literal(pattern)
        wildcard = shape(pattern) == ("_",)
        if value is None and not wildcard:
            return False
        if wildcard and reach.stop - reach.start > len(seen) or value in reach and value not in seen:
            if directly_checks(body[0].children if len(body) == 1 and body[0].text == "{" else body):
                return True
        if wildcard:
            return False
        if value in reach:
            seen.add(value)
    return False


def pattern_facts(pattern, rows, lookup):
    if not rows or not all(row is not None for row in rows):
        return {}
    pattern = unborrow(pattern)
    name = qualified_name(pattern)
    if len(name) == 1 and name != ("_",):
        if any(not row or not hasattr(row[0], "text") for row in rows):
            return {}
        values = [iterable(row, lookup) for row in rows]
        result = {}
        if all(value is not None for value in values):
            lengths = {value.length for value in values}
            result["iterable"] = Iterable(min(value.minimum for value in values),
                                          lengths.pop() if len(lengths) == 1 else None)
        if all((isinstance(value := literal(row), str) and value)
               or ((text := lookup(row, "text")) and text.minimum) for row in rows):
            result["text"] = Iterable(1)
        return {name[0]: result}
    if len(pattern) != 1 or pattern[0].text != "(":
        return {}
    parts = list(split(pattern[0].children))
    columns = []
    for row in rows:
        if row and (row[0] is None or isinstance(row[0], tuple)):
            values = row
        elif len(row) == 1 and row[0].text == "(":
            values = list(split(row[0].children))
        else:
            return {}
        if len(values) != len(parts):
            return {}
        columns.append(values)
    return {name: facts for child, column in zip(parts, zip(*columns))
            for name, facts in pattern_facts(child, column, lookup).items()}


def branch_witness(tokens, counters, lookup):
    op, left, right = comparison(tokens)
    if not op:
        return None
    name = qualified_name(left)
    reach = counters.get(name)
    if reach is None:
        name = qualified_name(right)
        reach = counters.get(name)
        left, right = right, left
        op = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(op, op)
    if reach is not None and (bound := integer(right, lookup)) is not None:
        for candidate in (reach.start, reach.stop - 1, bound - 1, bound, bound + 1):
            if candidate in reach and COMPARISONS[op](candidate, bound):
                return name, range(candidate, candidate + 1)
    return None


@lru_cache(maxsize=32)
def module_items(path):
    try:
        tokens, _ = tokenize(path.read_text(encoding="utf-8"))
        return FileIterables(group(tokens), path, {})
    except (OSError, ValueError, RecursionError, MemoryError):
        return None


class GuardIterables:
    def __init__(self, current, scan_root):
        self.current = current
        self.path = current.path.resolve()
        self.root = project_root(self.path, scan_root, (".git", "Cargo.toml"))
        self.source_root = next((parent for parent in self.path.parents
                                if parent.is_relative_to(self.root)
                                and ((parent / "lib.rs").is_file() or (parent / "main.rs").is_file())),
                                self.root / "src")
        self.package = ""
        try:
            config = tomllib.loads((self.root / "Cargo.toml").read_text(encoding="utf-8"))
            name = config.get("lib", {}).get("name", config.get("package", {}).get("name"))
            if isinstance(name, str):
                self.package = name.replace("-", "_")
        except (OSError, ValueError):
            pass
        self.module = ()
        self.in_source = self.path.is_relative_to(self.source_root)
        if self.in_source:
            parts = self.path.relative_to(self.source_root).with_suffix("").parts
            self.module = parts[:-1] if parts[-1] in {"lib", "main", "mod"} else parts
        self.cache = {}

    def definition(self, scope, name, *, local_imports=None):
        if not name:
            return "", (), None, ()
        return self.resolve(self.current, self.module, scope, name, local_imports or {}, set())

    def resolve(self, file, module, scope, name, local_imports, visited):
        key = file.path.resolve(), scope, name
        if key in visited:
            return "", (), None, ()
        visited = visited | {key}
        if name[0] not in {"crate", "self", "super"}:
            direct = (*scope, *name)
            if direct in file.definitions:
                return *file.definitions[direct], file, direct
            alias = local_imports.get(name[0], file.aliases.get(scope, {}).get(name[0]))
            if alias:
                absolute = (*alias, *name[1:])
                if absolute[0] in {"crate", "self", "super"}:
                    if file is self.current and not self.in_source:
                        local = absolute_path(scope, absolute)
                        return *file.definitions.get(local, ("", ())), file, local
                    absolute = absolute_path((*module, *scope), absolute)
                elif absolute[0] == self.package:
                    absolute = absolute[1:]
                else:
                    return "", (), None, ()
                return self.absolute(absolute, visited)
            glob_scopes = [absolute_path((*module, *scope), glob)
                           for glob in file.relative_globs.get(scope, ())]
            if file is self.current and not self.in_source:
                matches = [self.resolve(file, module, glob, name, {}, visited) for glob in glob_scopes]
            else:
                matches = [self.absolute((*glob, *name), visited) for glob in glob_scopes]
            matches = [match for match in matches if match[0]]
            if len(matches) == 1:
                return matches[0]
            return "", (), None, ()
        if file is self.current and not self.in_source:
            local = absolute_path(scope, name)
            return *file.definitions.get(local, ("", ())), file, local
        return self.absolute(absolute_path((*module, *scope), name), visited)

    def absolute(self, name, visited):
        if (self.in_source and self.module == name[:len(self.module)]
                and name[len(self.module):] in self.current.definitions):
            local = name[len(self.module):]
            return *self.current.definitions[local], self.current, local
        for depth in range(len(name) - 1, -1, -1):
            module, rest = name[:depth], name[depth:]
            if module == self.module and self.in_source:
                file = self.current
            else:
                file = self.module_file(module)
            if file is not None:
                if rest in file.definitions:
                    return *file.definitions[rest], file, rest
                return self.resolve(file, module, (), rest, {}, visited)
        return "", (), None, ()

    def module_file(self, module):
        if module not in self.cache:
            stem = self.source_root.joinpath(*module)
            candidates = (stem.with_suffix(".rs"), stem / "mod.rs") if module else (
                stem / "lib.rs", stem / "main.rs")
            paths = [path.resolve() for path in candidates
                     if path.is_file() and path.resolve().is_relative_to(self.root)]
            self.cache[module] = module_items(paths[0]) if len(paths) == 1 else None
        return self.cache[module]

    def lookup(self, scope, tokens, kind, local_imports):
        name = qualified_name(tokens)
        source, expression, file, path = self.definition(scope, name, local_imports=local_imports)
        if source == "constant":
            if kind == "number":
                return integer_literal(expression)
            if kind == "iterable":
                # Cross-file facts are based on the declaration's literal initializer.
                return iterable(expression, lambda tokens, kind: file.lookup(
                    path[:-1], qualified_name(tokens), kind)) if elements(expression) else None
            if kind == "text":
                value = literal(expression)
                if isinstance(value, str):
                    return Iterable(int(bool(value)))
        elif kind == "literal_helper" and source == "helper":
            if tail := literal_tail(expression):
                return iterable(tail, lambda *_: None)
        return None
