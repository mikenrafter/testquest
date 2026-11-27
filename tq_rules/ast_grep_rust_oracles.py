"""Rust equality checks with coupled calls or equivalent empty inputs."""

from dataclasses import dataclass, replace
import re

from . import lint
from .ast_grep import CALLS, children, namespace, parts, query, shape, unwrap
from .ast_grep_lint import direct_statements, literal
from .ast_grep_origins import name_of


IMPORT_FREE = re.compile(r"\s*function\s+\{\}\(\)\s*\{\{\s*(?:\{\}\(\);\s*)?\}\}\s*")
IDENTIFIER_STRATEGY = re.compile(
    r"\[(?:a-z|A-Z|_)+\]\[(?:a-z|A-Z|0-9|_)+\]\{\d+(?:,\d+)?\}"
)
IMPORT_FIELDS = {"frameworks_detected"}
OBSERVERS = {"len", "to_string", "as_str", "as_slice", "clone", "as_ref", "unwrap", "expect"}
ITERATORS = {"iter", "into_iter", "collect"}


@dataclass(frozen=True)
class Result:
    callee: tuple
    args: tuple
    projections: tuple = ()

    def project(self, name):
        return replace(self, projections=(*self.projections, name))


@dataclass(frozen=True)
class Input:
    kind: str
    key: object


class Oracles:
    def __init__(self, file, flow):
        self.file = file
        self.flow = flow
        self.facts = flow.reaching_facts
        self.scope = flow.scope
        self.alias_cache = {}

    def owner(self, name, scope):
        key = name, scope
        if key not in self.alias_cache:
            target = self.file.resolve_function(name, scope)
            if target is None and name:
                for count in range(len(scope), -1, -1):
                    if imported := self.file.imports.get((*scope[:count], name[0])):
                        name = (*imported, *name[1:])
                        break
            self.alias_cache[key] = target or name
        return self.alias_cache[key]

    def source(self, node, site, env, scope, seen):
        node = unwrap(node)
        if node is None:
            return None
        kind = node.kind()
        if kind in {"reference_expression", "try_expression"}:
            return self.source(node.field("value") or children(node)[-1], site, env, scope, seen)
        if name := name_of(node):
            if name in env:
                return env[name]
            if (site, name) in seen:
                return None
            origin = self.facts.eventual_origin(node, site)
            if origin is None:
                return None
            if origin.kind == "literal":
                return Input("empty" if origin.value in {(list, ()), (tuple, ()), (str, "")} else "literal",
                             origin.value)
            if origin.kind in {"product", "tuple-member"}:
                source = origin.source
                if source is None:
                    return None
                at = self.flow.syntax.absolute(source)
                value = self.source(source, at if at in self.facts.incoming else site, env, scope,
                                    seen | {(site, name)})
                if origin.kind == "tuple-member":
                    if not isinstance(value, tuple) or origin.value >= len(value):
                        return None
                    return value[origin.value]
                return value
            return Input("parameter", name)
        value = literal(self.file.syntax, node)
        if value is not lint.UNKNOWN:
            return Input("empty" if value in ([], (), "") else "literal", lint.literal_key(value))
        if kind == "field_expression":
            base = self.source(node.field("value"), site, env, scope, seen)
            field = node.field("field")
            return base.project(field.text()) if isinstance(base, Result) and field is not None else None
        if kind == "tuple_expression":
            return tuple(self.source(child, site, env, scope, seen) for child in children(node))
        if kind == "array_expression":
            values = [self.source(child, site, env, scope, seen) for child in children(node)]
            if len(values) == 1 and isinstance(values[0], Input) and values[0].kind == "import-free":
                return values[0]
            return None
        if kind not in CALLS:
            name = parts(node)
            return Input("path", name) if name else None
        call = self.flow.syntax.call(node)
        if not call.name:
            return None
        if call.macro:
            if call.name == ("format",):
                return self.source_format(call.arguments)
            if call.name == ("vec",) and len(call.arguments) == 1:
                element = self.source(call.arguments[0], site, env, scope, seen)
                if isinstance(element, Input) and element.kind == "import-free":
                    return element
            return None
        method = call.method
        if call.receiver is not None:
            value = self.source(call.receiver, site, env, scope, seen)
            if method in {"unwrap", "expect", "clone", "as_ref", "as_slice"}:
                return value
            if method == "map" and isinstance(value, Input) and value.kind in {"empty", "import-free"}:
                return value
            if method in ITERATORS:
                return value if isinstance(value, Input) else value.project(method) if isinstance(value, Result) else None
            if method in OBSERVERS and isinstance(value, Result):
                return value.project(method)
        args = tuple(self.source(arg, site, env, scope, seen) for arg in call.arguments)
        if method == "parse_file" and len(args) == 2 and isinstance(args[1], Input) and args[1].kind == "import-free":
            return args[1]
        callee = self.owner(call.name, scope)
        if callee in self.file.functions and callee not in seen and len(seen) < 16:
            adapted = self.returned(callee, args, site, seen)
            if isinstance(adapted, tuple) or isinstance(adapted, Result) and adapted.callee in self.file.functions:
                return adapted
        return Result(callee, args)

    def returned(self, callee, args, site, seen):
        fn = self.file.functions[callee]
        parameters = children(fn.field("parameters"))
        names = [name_of(parameter.field("pattern")) for parameter in parameters]
        if len(names) != len(args) or None in names:
            return None
        values = dict(zip(names, args))
        scope = (*namespace(fn), callee[-1])
        seen = seen | {callee}
        statements = list(direct_statements(fn.field("body")))
        if not statements or len(statements) > 8:
            return None
        tail = statements[-1]
        if tail.kind() == "return_expression":
            tail = next(iter(children(tail)), None)
        if tail is None or tail.kind() not in {"tuple_expression", "call_expression"}:
            return None
        for statement in statements[:-1]:
            if statement.kind() == "let_declaration" and (name := name_of(statement.field("pattern"))):
                values[name] = self.source(statement.field("value"), site, values, scope, seen)
            else:
                for write in query(statement, "assignment_expression", "compound_assignment_expr"):
                    if name := name_of(write.field("left")):
                        values[name] = None
        if tail.kind() == "call_expression":
            call = self.file.syntax.call(tail)
            if call.name == ("Ok",) and len(call.arguments) == 1:
                name = name_of(call.arguments[0])
                if name in names:
                    return values[name]
        return self.source(tail, site, values, scope, seen)

    def source_format(self, args):
        if len(args) not in {2, 3}:
            return None
        template = literal(self.file.syntax, args[0])
        if template is lint.UNKNOWN and args[0].kind() == "string_literal":
            template = args[0].text()[1:-1].replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
        if not isinstance(template, str) or not IMPORT_FREE.fullmatch(template):
            return None
        for argument in args[1:]:
            strategy = self.flow.strategies.get(name_of(argument))
            regex = self.strategy_regex(strategy)
            if not isinstance(regex, str) or not IDENTIFIER_STRATEGY.fullmatch(regex):
                return None
        return Input("import-free", template)

    def strategy_regex(self, strategy):
        if strategy is None or strategy.kind() != "call_expression":
            return literal(self.flow.syntax, strategy)
        call = self.flow.syntax.call(strategy)
        name = self.owner(call.name, self.scope)
        if name not in self.file.functions or call.arguments:
            return None
        body = list(direct_statements(self.file.functions[name].field("body")))
        if len(body) != 1 or body[0].kind() != "call_expression":
            return None
        modify = self.file.syntax.call(body[0])
        if modify.method == "prop_map" and len(modify.arguments) == 1:
            identity = modify.arguments[0]
            params = identity.field("parameters")
            if params is not None and len(children(params)) == 1 and (
                name_of(children(params)[0]) == name_of(identity.field("body"))
            ):
                return literal(self.file.syntax, modify.receiver)
        return None

    def observations(self, check):
        return tuple(self.source(node, check.location, {}, self.scope, frozenset())
                     for node in (check.left, check.right))

    def duplicate_call(self, check):
        if check.left is None or check.right is None:
            return False
        if check.left.kind() != "call_expression" or check.right.kind() != "call_expression":
            return False
        call = self.flow.syntax.call(check.left)
        return (self.owner(call.name, self.scope) in self.file.functions
                and bool(call.arguments) and shape(check.left) == shape(check.right))

    def coupled(self, check, first, second, pinned):
        if not isinstance(first, Result) or not isinstance(second, Result):
            return False
        if first in pinned or second in pinned or first.projections != second.projections:
            return False
        if first == second and first.args and None not in first.args:
            # A delegated call or returned argument can supply the same expected value.
            origins = [self.facts.eventual_origin(node, check.location) for node in (check.left, check.right)]
            if all(origin is not None and origin.source is not None for origin in origins):
                if all(origin.kind == "tuple-member" for origin in origins):
                    return origins[0].site == origins[1].site and origins[0].value != origins[1].value
                names = [self.flow.syntax.call(origin.source).name for origin in origins]
                return all(names) and names[0] != names[1]
            return False
        if first.callee == second.callee or not first.args or not second.args:
            return False
        if first.args[1:] != second.args[1:] or None in first.args[1:]:
            return False
        if len(first.callee) > 1 and first.callee[:-1] == second.callee[:-1] and first.args == second.args:
            return None not in first.args and not lint.inverse_codecs(first.callee, second.callee)
        supplied = first.args[0]
        return isinstance(supplied, Input) and supplied == second.args[0] and (
            supplied.kind == "empty" or supplied.kind == "import-free" and IMPORT_FIELDS.intersection(first.projections)
        )

    def issues(self):
        comparisons = [(check, *self.observations(check)) for check in self.flow.checks if check.operator == "=="]
        pins = {result for check, first, second in comparisons if not check.guarded
                for result, pin in ((first, second), (second, first))
                if isinstance(result, Result) and isinstance(pin, Input) and pin.kind in {"literal", "empty"}}
        for check, first, second in comparisons:
            if self.duplicate_call(check) or self.coupled(check, first, second, pins):
                yield check.location
