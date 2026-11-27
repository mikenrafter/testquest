"""Find a terminal assertion whose failure is absorbed by a local handler."""

from .ast_grep import children, line, offset, query


TRY_KINDS = {
    "python": ("try_statement",),
    "java": ("try_statement", "try_with_resources_statement"),
    "kotlin": ("try_expression",),
    "typescript": ("try_statement",),
    "tsx": ("try_statement",),
    "javascript": ("try_statement",),
}
FAILURE_TYPES = {
    "python": {"AssertionError", "Exception"},
    "java": {"AssertionError", "Error", "Throwable"},
    "kotlin": {"AssertionError", "Error", "Throwable"},
}
HANDLERS = {"except_clause", "catch_clause", "catch_block"}
NESTED_SCOPES = {
    "function_definition", "lambda", "lambda_expression", "lambda_literal", "arrow_function",
    "function_expression", "function_declaration", "method_declaration", "class_declaration",
}
THROWS = {"throw_statement", "throw_expression", "raise_statement"}


def catches_assertion(handler, language):
    contents = children(handler)
    if language == "python":
        if len(contents) == 1:
            return True
        types = contents[0]
        if types.kind() == "as_pattern":
            types = children(types)[0]
        names = children(types) if types.kind() == "tuple" else [types]
        return any(node.text().removeprefix("builtins.") in FAILURE_TYPES[language] for node in names)
    if language == "java":
        parameter = next((node for node in contents if node.kind() == "catch_formal_parameter"), None)
        if parameter is None:
            return False
        types = next((node for node in children(parameter) if node.kind() == "catch_type"), None)
        return types is not None and any(
            node.text().removeprefix("java.lang.") in FAILURE_TYPES[language] for node in children(types)
        )
    if language == "kotlin":
        return any(
            node.text().removeprefix("kotlin.").removeprefix("java.lang.") in FAILURE_TYPES[language]
            for node in contents if node.kind() == "user_type"
        )
    return handler.field("parameter") is None


def terminal(block):
    if block is None:
        return None
    statements = children(block)
    if not statements:
        return None
    last = statements[-1]
    if last.kind() == "expression_statement":
        expressions = children(last)
        return expressions[0] if len(expressions) == 1 else None
    return last


def failure_call(node):
    if node.kind() not in {"call", "call_expression", "method_invocation"}:
        return False
    callee = node.field("function") or node.field("name")
    if callee is None:
        callee = next(iter(children(node)), None)
    if callee is None:
        return False
    name = callee.text().rsplit(".", 1)[-1]
    return name == "fail"


def can_fail(node, assertions):
    if node.kind() in NESTED_SCOPES:
        return False
    return (
        node.kind() in THROWS | {"assert_statement"}
        or offset(node) in assertions
        or failure_call(node)
        or any(can_fail(child, assertions) for child in children(node))
    )


def swallowed_assertions(body, language, checks):
    kinds = TRY_KINDS.get(language)
    if kinds is None:
        return
    assertions = {offset(node) for node in checks}
    for attempt in query(body, *kinds):
        parent = attempt.parent()
        while parent is not None and parent != body and parent.kind() not in NESTED_SCOPES:
            parent = parent.parent()
        if parent != body:
            continue
        contents = children(attempt)
        block = attempt.field("body") or next(iter(contents), None)
        last = terminal(block)
        if last is None or not (
            offset(last) in assertions or last.kind() == "assert_statement" or failure_call(last)
        ):
            continue
        handler = next((node for node in contents if node.kind() in HANDLERS
                        and catches_assertion(node, language)), None)
        if handler is None or can_fail(handler, assertions):
            continue
        if any(can_fail(node, assertions) for node in contents if node.kind() in {"finally_clause", "finally_block"}):
            continue
        yield line(last)
