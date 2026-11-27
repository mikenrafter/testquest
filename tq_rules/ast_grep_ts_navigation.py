"""Find location assertions that can race a native history traversal."""

from .ast_grep import children, offset, query
from .ast_grep_ts import FUNCTIONS, arguments, chain, expression, literal, name, text_literal
from .ast_grep_ts_imports import imported_helper, module_path, read_module
from .ast_grep_ts_ui import QUERY, TESTING_LIBRARY, imported_reference, owned_query, reaching_initializer


def location_read(file, node):
    root, fields = chain(node)
    family = name(root)
    if family == "window" and fields:
        family, *fields = fields
    return (file.binding(root) is None and file.parameter_scope(root) is None
            and (family == "location" and bool(fields) or family == "history" and tuple(fields) == ("state",)))


def history_traversal(file, node):
    root, fields = chain(node.field("function"))
    if file.binding(root) is not None or file.parameter_scope(root) is not None:
        return False
    return ((name(root) == "history" and fields in {("back",), ("go",)})
            or (name(root) == "window" and fields in {("history", "back"), ("history", "go")}))


def call_target(file, callee):
    if file.parameter_scope(callee) is not None:
        return None
    if (target := file.helper(callee)) is not None:
        return file, target
    imported = imported_helper(file, callee)
    if imported is not None and imported[0] is not None and imported[1] is not None and imported[1].kind() in FUNCTIONS:
        return imported
    return None


def event_target(file, call, seen=frozenset()):
    fn = expression(call.field("function"))
    root, members = chain(fn)
    source, imported = imported_reference(file, fn)
    if (source.startswith(TESTING_LIBRARY) or source == "vite-plus/test/browser") and (
            imported[:1] in {("fireEvent",), ("userEvent",)} or
            source == "@testing-library/user-event" and imported[:1] == ("default",)):
        return file, call
    if name(root) in {"fireEvent", "userEvent"} and file.binding(root) is None and file.parameter_scope(root) is None:
        return file, call
    if members:
        receiver = expression(fn.field("object"))
        initializer, field = reaching_initializer(file, receiver)
        setup = expression(initializer) if initializer is not None and not field else receiver
        if setup is not None and setup.kind() == "call_expression":
            source, imported = imported_reference(file, setup.field("function"))
            if (source.startswith(TESTING_LIBRARY) and imported == ("userEvent", "setup")
                    or source == "@testing-library/user-event" and imported == ("default", "setup")):
                return file, call
    if (target := call_target(file, fn)) is not None:
        owner, callback = target
        key = owner.path, offset(callback)
        body = callback.field("body")
        if body is not None and key not in seen and len(seen) < 5:
            for child in owned_query(body, "call_expression"):
                if (event := event_target(owner, child, seen | {key})) is not None:
                    return event
    return None


def event_label(file, event):
    args = arguments(event)
    if not args:
        return None
    target = expression(args[0])
    if target is None or target.kind() != "call_expression":
        return None
    fn = target.field("function")
    if (helper := call_target(file, fn)) is not None:
        owner, callback = helper
        body = expression(callback.field("body"))
        if body is not None and body.kind() == "call_expression":
            target = body
    _, members = chain(target.field("function"))
    query_name = members[-1] if members else name(target.field("function"))
    if not QUERY.fullmatch(query_name):
        return None
    args = arguments(target)
    if query_name.endswith(("ByText", "ByLabelText")) and args:
        return literal(args[0])
    if query_name.endswith("ByRole") and len(args) >= 2 and args[1].kind() == "object":
        for entry in children(args[1]):
            if entry.kind() == "pair" and name(entry.field("key")) == "name":
                return literal(entry.field("value"))
    return None


def named_handler(file, component, label):
    owners = []
    for element in query(component, "jsx_element", "jsx_self_closing_element"):
        opening = element.field("open_tag") or element
        tag = name(opening.field("name"))
        if tag not in {"a", "button", "input"}:
            continue
        attributes = [entry for entry in children(opening) if entry.kind() == "jsx_attribute"]
        entries = {children(entry)[0].text(): children(entry)[1] for entry in attributes if len(children(entry)) == 2}
        if text_literal(entries.get("aria-label")) != label and not any(
                child.kind() == "jsx_text" and child.text().strip() == label for child in children(element)):
            continue
        handler = entries.get("onClick")
        if handler is not None:
            values = children(handler)
            if len(values) == 1:
                owners.append(values[0])
    if len(owners) != 1:
        return None
    handler = owners[0]
    if handler.kind() in FUNCTIONS:
        return file, handler
    return call_target(file, handler)


def handler_traversal(file, callback, seen=frozenset()):
    key = file.path, offset(callback)
    body = callback.field("body")
    if body is None or key in seen or len(seen) >= 5:
        return None
    unknown = False
    for call in owned_query(body, "call_expression"):
        if history_traversal(file, call):
            return True
        target = call_target(file, call.field("function"))
        result = handler_traversal(*target, seen | {key}) if target is not None else None
        if result is True:
            return True
        unknown |= result is None
    return None if unknown else False


def rendered_components(file, statements):
    components = []
    for statement in statements:
        for rendered in owned_query(statement, "call_expression"):
            source, imported = imported_reference(file, rendered.field("function"))
            if imported != ("render",) or not source.startswith(TESTING_LIBRARY):
                continue
            for arg in arguments(rendered):
                if arg.kind() not in {"jsx_element", "jsx_self_closing_element"}:
                    continue
                opening = arg.field("open_tag") or arg
                component = opening.field("name")
                if (resolved := call_target(file, component)) is not None:
                    components.append(resolved)
    return components


def component_traversal(file, component):
    if any(history_traversal(file, call) for call in query(component, "call_expression")):
        return True
    if file.scan_root is None:
        return False
    sources = {source for source, _ in file.source_imports.values() if source.startswith(".")}
    for source in sources:
        imported = module_path(file.scan_root.resolve(), file.path.resolve(), source)
        if imported is not None and (module := read_module(file.scan_root.resolve(), imported)) is not None:
            if any(history_traversal(module, call) for call in query(module.syntax.root, "call_expression")):
                return True
    return False


def writes_history(file, call):
    callee = call.field("function")
    root, methods = chain(callee)
    if (name(root) == "history" and methods in {("replaceState",), ("pushState",)}
            or name(root) == "window" and methods in {("history", "replaceState"), ("history", "pushState")}):
        return file.binding(root) is None and file.parameter_scope(root) is None
    if (target := file.helper(callee)) is not None:
        body = expression(target.field("body"))
        if body is not None and body.kind() == "call_expression":
            root, methods = chain(body.field("function"))
            return (name(root), methods[:-1]) in {("window", ("history",)), ("history", ())} and (
                methods[-1:] in {("pushState",), ("replaceState",)}
                and file.binding(root) is None and file.parameter_scope(root) is None)
    return False


def after_unsettled_click(file, node):
    """Find a preceding event in this block with no synchronization before the check."""
    for parent in node.ancestors():
        if parent.kind() == "await_expression" or parent.kind() == "call_expression" and (
                name(parent.field("function")) in {"waitFor", "waitForElementToBeRemoved"}
                or name(parent.field("function").field("property")) in {"waitFor", "poll"}):
            return None, []
        if parent.kind() in FUNCTIONS and (parent.parent() is None or parent.parent().kind() != "arguments"):
            break
    statement = next((parent for parent in node.ancestors() if parent.parent() is not None
                      and parent.parent().kind() == "statement_block"), None)
    if statement is None:
        return None, []
    previous = [child for child in children(statement.parent()) if offset(child) < offset(statement)]
    for entry in reversed(previous):
        awaited = list(owned_query(entry, "await_expression"))
        if awaited:
            return None, previous
        for call in owned_query(entry, "call_expression"):
            if (event := event_target(file, call)) is not None:
                return event, previous
            if writes_history(file, call):
                return None, previous
        if entry.kind() in {"if_statement", "try_statement", "switch_statement", "for_statement",
                           "for_in_statement", "while_statement", "do_statement"}:
            break
    return None, previous


def navigation_issues(file, checks):
    for check in checks:
        if not location_read(file, check.actual):
            continue
        event, statements = after_unsettled_click(file, check.node)
        if event is None:
            continue
        label = event_label(*event)
        components = rendered_components(file, statements)
        handlers = [target for owner, component in components
                    if isinstance(label, str) and (target := named_handler(owner, component, label)) is not None]
        proved = handler_traversal(*handlers[0]) if len(handlers) == 1 else None
        if proved is None:
            proved = (any(history_traversal(file, node) for node in query(file.syntax.root, "call_expression"))
                      or any(component_traversal(*component) for component in components))
        if proved:
            yield check.node, "unawaited-navigation"
