"""Find location assertions that can race a native history traversal."""

from .ast_grep import children, offset, split_operands
from .ast_grep_ts import FUNCTIONS, arguments, chain, expression, name, plain_array, text_literal
from .ast_grep_ts_navigation_refs import Reference, branch_possible, object_reference, resolve, static_value
from .ast_grep_ts_ui import QUERY, TESTING_LIBRARY, imported_reference, owned_query, return_expression


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


def event_target(ref, seen=frozenset()):
    event = ref.node
    callee = ref.at(event.field("function"))
    root, members = chain(callee.node)
    source, imported = imported_reference(ref.file, callee.node)
    if members[-1:] == ("click",):
        if (source.startswith(TESTING_LIBRARY) or source == "vite-plus/test/browser") and (
                imported[:1] in {("fireEvent",), ("userEvent",)}
                or source == "@testing-library/user-event" and imported[:1] == ("default",)):
            return ref
        if (name(root) in {"fireEvent", "userEvent"} and ref.file.binding(root) is None
                and ref.file.parameter_scope(root) is None):
            return ref
        receiver = resolve(ref.at(callee.node.field("object")))
        if receiver is not None and receiver.node.kind() == "call_expression":
            source, imported = imported_reference(receiver.file, receiver.node.field("function"))
            if (source.startswith(TESTING_LIBRARY) and imported == ("userEvent", "setup")
                    or source == "@testing-library/user-event" and imported == ("default", "setup")):
                return ref
    callback = resolve(callee)
    if callback is None or callback.node.kind() not in FUNCTIONS or callback.key() in seen or len(seen) >= 5:
        return None
    invoked = callback.bind([ref.at(arg) for arg in arguments(event)])
    body = invoked.node.field("body")
    if body is not None:
        for child in reversed(list(owned_query(body, "call_expression"))):
            found = event_target(invoked.at(child), seen | {callback.key()})
            if found is not None:
                return found
    return None


def event_selector(ref, parent=False, seen=frozenset()):
    if ref.node is None or ref.key() in seen or len(seen) >= 8:
        return None
    seen = seen | {ref.key()}
    if ref.node.kind() == "member_expression" and name(ref.node.field("property")) == "parentElement":
        return event_selector(ref.at(ref.node.field("object")), True, seen) if not parent else None
    target = resolve(ref)
    if target is None or target.node is None:
        return None
    if target.key() != ref.key():
        return event_selector(target, parent, seen)
    if target.node.kind() != "call_expression":
        return None
    callee = target.at(target.node.field("function"))
    _, members = chain(callee.node)
    query_name = members[-1] if members else name(callee.node)
    args = arguments(target.node)
    if QUERY.fullmatch(query_name) and args:
        role = None
        label = target.at(args[0])
        if query_name.endswith("ByRole"):
            role = static_value(label)
            options = target.at(args[1]) if len(args) >= 2 else None
            label = object_reference(options, "name") if options is not None and options.node.kind() == "object" else None
        elif not query_name.endswith(("ByText", "ByLabelText")):
            return None
        if label is not None and isinstance(value := static_value(label), str):
            return role, value, parent
        return None
    callback = resolve(callee)
    if callback is not None and callback.node.kind() in FUNCTIONS:
        invoked = callback.bind([target.at(arg) for arg in args])
        return event_selector(invoked.at(return_expression(callback.node)), parent, seen)
    return None


def jsx_props(ref):
    props = {}
    for attribute in children(ref.node.field("open_tag") or ref.node):
        parts = children(attribute)
        if attribute.kind() == "jsx_attribute" and len(parts) == 2:
            key, value = parts
            if value.kind() == "jsx_expression":
                value = next(iter(children(value)), None)
            props[key.text()] = ref.at(value)
        elif attribute.kind() == "jsx_expression" and parts and parts[0].kind() == "spread_element":
            spread = ref.at(children(parts[0])[0])
            branches = (spread.node.field("consequence"), spread.node.field("alternative")
                        ) if spread.node.kind() == "ternary_expression" else (spread.node,)
            for branch in branches:
                if branch.kind() == "object":
                    for entry in children(branch):
                        key = name(entry.field("key") or entry)
                        if key:
                            value = object_reference(spread.at(branch), key)
                            props[key] = value if key not in props else None
    return props


def returned_values(callback):
    body = callback.at(callback.node.field("body"))
    if body.node is None:
        return
    if body.node.kind() != "statement_block":
        yield body
        return
    for statement in owned_query(body.node, "return_statement"):
        if reachable(body.at(statement), body.node):
            yield body.at(next(iter(children(statement)), None))


def jsx_elements(ref, seen=frozenset(), component_depth=0):
    node = ref.node
    if node is None or ref.key() in seen or len(seen) >= 48:
        return
    seen = seen | {ref.key()}

    def visit(child, depth=component_depth):
        yield from jsx_elements(child, seen, depth)

    kind = node.kind()
    if kind in {"jsx_element", "jsx_self_closing_element"}:
        tag = (node.field("open_tag") or node).field("name")
        tag_name = name(tag)
        if tag_name and tag_name[0].islower():
            yield ref
        elif tag is not None and component_depth < 8:
            component = resolve(ref.at(tag))
            if component is not None and component.node.kind() in FUNCTIONS:
                for returned in returned_values(component.bind(props=jsx_props(ref))):
                    yield from visit(returned, component_depth + 1)
        for child in children(node):
            if child.kind() not in {"jsx_opening_element", "jsx_closing_element"}:
                yield from visit(ref.at(child))
    elif kind == "jsx_expression":
        for child in children(node):
            yield from visit(ref.at(child))
    elif kind == "ternary_expression":
        condition = ref.at(node.field("condition"))
        for branch, field in ((True, "consequence"), (False, "alternative")):
            if branch_possible(condition, branch):
                yield from visit(ref.at(node.field(field)))
    elif kind == "binary_expression":
        operator, left, right = split_operands(node)
        if operator in {"&&", "||"} and branch_possible(ref.at(left), operator == "&&"):
            yield from visit(ref.at(right))
    elif kind == "call_expression":
        fn = node.field("function")
        args = arguments(node)
        if name(fn.field("property")) != "map" or not args:
            return
        iterator = expression(args[0])
        array = resolve(ref.at(fn.field("object")))
        if (iterator.kind() in FUNCTIONS and array is not None and array.node.kind() == "array"
                and plain_array(array.node) and len(children(array.node)) <= 20):
            for item in children(array.node):
                mapped = ref.at(iterator).bind([array.at(item)])
                for returned in returned_values(mapped):
                    yield from visit(returned)
    elif name(node):
        resolved = resolve(ref)
        if resolved is not None and resolved.key() != ref.key():
            yield from visit(resolved)


def rendered_components(file, statements):
    for statement in statements:
        for rendered in owned_query(statement, "call_expression"):
            source, imported = imported_reference(file, rendered.field("function"))
            if imported == ("render",) and source.startswith(TESTING_LIBRARY):
                args = arguments(rendered)
                if args:
                    yield Reference(file, expression(args[0]))


def matches_element(element, props, role, label):
    node = element.node
    tag = name((node.field("open_tag") or node).field("name"))
    declared_role = static_value(props["role"]) if props.get("role") is not None else (
        {"button": "button", "a": "link"}.get(tag))
    if role is not None and role != declared_role:
        return False
    if props.get("aria-label") is not None:
        return static_value(props["aria-label"]) == label
    parts = []
    for part in children(node):
        if part.kind() == "jsx_text":
            parts.append(part.text())
        elif part.kind() == "jsx_expression" and children(part):
            value = static_value(element.at(children(part)[0]))
            if isinstance(value, str):
                parts.append(value)
    return " ".join("".join(parts).split()) == label


def named_handlers(file, statements, selector):
    role, label, parent = selector
    for component in rendered_components(file, statements):
        for element in jsx_elements(component):
            props = jsx_props(element)
            if not matches_element(element, props, role, label):
                continue
            if parent:
                overlay = element.node.parent()
                if overlay.kind() != "jsx_element":
                    yield None
                    continue
                props = jsx_props(element.at(overlay))
            yield props.get("onClick")


def assigned_handler(event, target, statements):
    binding = event.file.binding(target.node)
    if binding is None or event.file.parameter_scope(target.node) is not None:
        return None
    for statement in reversed(statements):
        assignment = next(iter(children(statement)), None) if statement.kind() == "expression_statement" else None
        if assignment is not None and assignment.kind() == "assignment_expression":
            receiver, members = chain(assignment.field("left"))
            owner = event.file.binding(receiver)
            if members == ("onclick",) and owner is not None and offset(owner) == offset(binding):
                return event.at(assignment.field("right"))
    return None


def reachable(ref, body):
    child = ref.node
    for parent in child.ancestors():
        if offset(parent) < offset(body):
            break
        kind = parent.kind()
        if kind in {"if_statement", "ternary_expression"}:
            consequent = parent.field("consequence")
            if consequent is not None and offset(child) >= offset(consequent):
                branch = offset(child) == offset(consequent)
                if not branch_possible(ref.at(parent.field("condition")), branch):
                    return False
        elif kind == "binary_expression":
            operator, left, right = split_operands(parent)
            if operator in {"&&", "||"} and offset(child) == offset(right):
                if not branch_possible(ref.at(left), operator == "&&"):
                    return False
        elif kind == "statement_block":
            for previous in children(parent):
                if offset(previous) >= offset(child):
                    break
                if previous.kind() in {"return_statement", "throw_statement"}:
                    return False
        if parent.range() == body.range():
            break
        child = parent
    return True


def handler_traversal(ref, seen=frozenset()):
    callback = resolve(ref)
    if callback is None or callback.node.kind() not in FUNCTIONS or callback.key() in seen or len(seen) >= 8:
        return False
    body = callback.node.field("body")
    if body is None:
        return False
    for call in owned_query(body, "call_expression"):
        if not reachable(callback.at(call), body):
            continue
        if history_traversal(callback.file, call):
            return True
        invoked = resolve(callback.at(call.field("function")))
        if invoked is not None and invoked.node.kind() in FUNCTIONS:
            invoked = invoked.bind([callback.at(arg) for arg in arguments(call)])
            if handler_traversal(invoked, seen | {callback.key()}):
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
        for call in reversed(list(owned_query(entry, "call_expression"))):
            if (event := event_target(Reference(file, call))) is not None:
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
        if event is None or not arguments(event.node):
            continue
        target = event.at(arguments(event.node)[0])
        selector = event_selector(target)
        if selector is not None:
            handlers = list(named_handlers(file, statements, selector))
        elif event.file is file:
            handlers = [assigned_handler(event, target, statements)]
        else:
            continue
        if handlers and all(handler_traversal(handler) for handler in handlers):
            yield check.node, "unawaited-navigation"
