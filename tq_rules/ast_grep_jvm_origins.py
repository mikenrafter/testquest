"""Operand origins for expected-first JVM assertions."""

from .ast_grep import children, line, offset
from .ast_grep_jvm import body_of, call_of, declaration_name, member
from .ast_grep_jvm_checks import literal
from .ast_grep_origins import ReachingFacts, direct_return


ASSERTIONS = {"assertEquals", "assertArrayEquals", "assertIterableEquals"}
FRAMEWORKS = {"org.junit.Assert", "junit.framework.Assert", "org.junit.jupiter.api.Assertions",
              "kotlin.test", "kotlin.test.AssertionsKt"}


def jvm_call(node):
    call = call_of(node)
    if call is None:
        return None
    _, receiver = member(call.receiver)
    name = (receiver, call.method) if receiver else (call.method,)
    return name, call.receiver, call.args


def expected_first(file, node, call):
    method = file.resolve_name(call.method)
    if method not in ASSERTIONS or file.functions.get(call.method) and file.helper(node, call) is not None:
        return False
    _, receiver = member(call.receiver)
    if receiver:
        qualified = {file.imports.get(receiver, call.receiver.text())}
    elif call.method in file.imports:
        qualified = {file.imports[call.method].rpartition(".")[0]}
    else:
        qualified = FRAMEWORKS & file.wildcard_imports
    if len(qualified) != 1 or not qualified <= FRAMEWORKS:
        return False
    junit4 = {"org.junit.Assert", "junit.framework.Assert"}
    if qualified & junit4:
        # Three arguments can be a leading message or a trailing numeric delta.
        return len(call.args) == 2
    return True


def local_helper(file):
    def resolve(node, name):
        call = call_of(node)
        if call is None or call.receiver is not None or call.named or not file.functions.get(call.method):
            return None
        target = file.helper(node, call)
        if target is None or target[0] is not file:
            return None
        function = target[1]
        parameters = function.field("parameters") or next(
            (node for node in children(function) if node.kind() == "function_value_parameters"), None
        )
        if parameters is None:
            return None
        names = [declaration_name(parameter) or None for parameter in children(parameters)]
        return function, names, direct_return(body_of(function))
    return resolve


def misordered_expected(file, body, checks):
    candidates = [
        check for check in checks
        if check.operator == "==" and (call := call_of(check.node)) is not None and expected_first(file, check.node, call)
    ]
    if not candidates:
        return
    origins = ReachingFacts(file.syntax, body, literal, call=jvm_call, helper=local_helper(file),
                            captures=(offset(check.node) for check in candidates))
    for check in candidates:
        site = offset(check.node)
        expected, actual = (origins.eventual_origin(node, site) for node in (check.left, check.right))
        if expected is not None and actual is not None and expected.kind == "product" and actual.kind == "literal":
            yield line(check.node)
