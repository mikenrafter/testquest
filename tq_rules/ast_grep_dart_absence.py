"""Match Dart absence checks to witnesses for the same recorder or detector."""

from collections import defaultdict

from .ast_grep import line, offset, query
from .ast_grep_dart import constant, expression_operator, negate, returned_expression
from .ast_grep_dart_syntax import between, contents, final_call, kind, names, named, signature, tokens, unwrap


MATCHES = {"allMatches", "firstMatch", "hasMatch", "stringMatch"}
READERS = {"readAsString", "readAsStringSync", "readAsLines", "readAsLinesSync"}


def initializer(file, nodes):
    nodes = unwrap(nodes)
    while len(names(nodes)) == 1:
        binding = file.binding(nodes)
        if binding is None or not binding.final or not binding.initializer:
            break
        nodes = unwrap(binding.initializer)
    return nodes


def regex(file, nodes):
    call = final_call(initializer(file, nodes))
    if call is not None and call.name == ("RegExp",):
        return ("regex", tuple(signature(arg) for arg in call.args),
                tuple(sorted((name, signature(arg)) for name, arg in call.keywords.items())))
    return None


def invokes_detector(file, nodes, *, local_helpers=True):
    nodes = initializer(file, negate(nodes) or nodes)
    path = names(nodes)
    if path[-1:] in {("isEmpty",), ("isNotEmpty",), ("length",)}:
        nodes = initializer(file, nodes[:-1])
    call = final_call(nodes)
    if call is None:
        return None
    if call.method in MATCHES and (key := regex(file, call.receiver)) is not None:
        return key
    if local_helpers and not call.receiver and call.name:
        callback = file.resolve_name((call.node,), {})
        if callback is not None and callback.file is file:
            return "detector", offset(callback.body)
    return None


def loop_input(node):
    parts = next((child for child in contents(node) if child.kind() == "for_loop_parts"), node)
    return named(between(between(tokens(parts), "(", ")"), "in"))


def condition(node):
    within = between(tokens(node), "(", ")")
    return named(within[:next((index for index, token in enumerate(within) if token.kind() == "case"), len(within))])


def collector_dependencies(file, site):
    loop_keys, filter_keys, text_keys = set(), set(), set()
    reads_source = False
    for owner in site.ancestors():
        if owner.kind() in {"function_expression_body", "function_body"}:
            break
        if owner.kind() == "for_statement":
            if not loop_keys and (key := invokes_detector(file, loop_input(owner))) is not None:
                loop_keys.add(key)
            reads_source |= any(any(child.text() in READERS for child in query(part, "identifier"))
                                for part in loop_input(owner))
        if owner.kind() != "if_statement":
            continue
        predicate = condition(owner)
        if (key := invokes_detector(file, predicate)) is not None:
            filter_keys.add(key)
        operator, left, right = expression_operator(predicate)
        for operand in (predicate, *((left, right) if operator in {"&&", "||"} else ())):
            call = final_call(operand)
            if call is None:
                continue
            if (key := invokes_detector(file, operand, local_helpers=False)) is not None:
                filter_keys.add(key)
            if call.method == "contains" and call.args and type(constant(call.args[0])) is str:
                text_keys.add(("text", constant(call.args[0])))
                receiver = initializer(file, call.receiver)
                reads_source |= any(part.text() in READERS or any(
                    child.text() in READERS for child in query(part, "identifier")) for part in receiver)
    if loop_keys:
        return loop_keys | {key for key in filter_keys if key[0] == "regex"}, {
            key for key in filter_keys if key[0] == "detector"
        }
    return filter_keys | (text_keys if reads_source else set()), set()


def positive(check):
    matcher = check.matcher
    if check.truth is True and not negate(check.predicate):
        return check.predicate
    if matcher in {"isNotEmpty", "isNotNull"}:
        return check.actual
    if check.matcher_args:
        value = constant(check.matcher_args[0])
        if type(value) in {int, float} and (matcher == "greaterThan" and value >= 0
                or matcher in {"greaterThanOrEqualTo", "hasLength", "equals"} and value > 0):
            return check.actual
    if check.operator == "==" and type(value := constant(check.expected)) in {int, float} and value > 0:
        return check.actual
    return ()


def counted_scans(file, callback):
    counts = defaultdict(set)
    if callback is None or callback.file is not file:
        return counts
    for node in query(callback.body, "postfix_expression", "unary_expression"):
        if not any(token.text() == "++" for token in tokens(node)):
            continue
        target = next((child for child in contents(node) if child.kind() == "assignable_expression"), None)
        counter = file.binding(contents(target))
        if counter is None:
            continue
        for owner in node.ancestors():
            if owner.kind() in {"function_body", "function_expression_body"}:
                break
            if owner.kind() == "for_statement" and (key := invokes_detector(file, loop_input(owner))) is not None:
                counts[offset(counter.node)].add(key)
                break
    return counts


def detector_witnesses(file, flow):
    witnessed = set()
    counters = counted_scans(file, flow.callback)
    for check in flow.checks:
        if check.file is not file or check.guarded or not (subject := positive(check)):
            continue
        if (key := invokes_detector(file, subject)) is not None:
            witnessed.add(key)
        if len(names(subject)) == 1 and (binding := file.binding(subject)) is not None:
            witnessed.update(counters[offset(binding.node)])
    return witnessed


def indexed_recorder(file, nodes):
    if len(nodes) != 2:
        return None
    bracket = next((child for child in contents(nodes[1]) if child.kind() == "unconditional_assignable_selector"),
                   nodes[1])
    indices = named(between(tokens(bracket), "[", "]"))
    if (key := file.recorder(nodes[:1])) is not None and type(index := constant(indices)) in {int, str}:
        return key, index
    return None


def success_slot(file, key, site):
    call = final_call(contents(site.parent()))
    if call is None or call.method != "then" or not call.args:
        return None
    handler = unwrap(call.keywords.get("onError", ()))
    if names(handler)[-1:] != ("add",) or file.recorder(handler[:-1]) != key:
        return None
    callback = file.callback(call.args[0])
    if callback is None or callback.file is not file:
        return None
    result = returned_expression(callback.body)
    if kind(result) != "assignment_expression":
        return None
    assignment = tokens(result[0])
    if len(assignment) < 3 or assignment[1].kind() != "=":
        return None
    return indexed_recorder(file, contents(assignment[0]))


def required_indices(file, nodes):
    nodes = unwrap(nodes)
    if not nodes:
        return
    if nodes[-1].text() == "!" and (key := indexed_recorder(file, nodes[:-1])) is not None:
        yield key
    call = final_call(nodes)
    if call is not None and not call.optional:
        for argument in (*call.args, *call.keywords.values()):
            yield from required_indices(file, argument)


def checked_result_slots(file, flow):
    checked = set()
    for check in flow.checks:
        if check.file is not file or check.guarded:
            continue
        checked.update(required_indices(file, check.actual))
    return checked


def covers_success_paths(file, key, writes, checked, callback):
    slots = {success_slot(file, key, site) for site, _ in writes}
    if len(slots) != len(writes) or not slots <= checked or callback.file is not file:
        return False
    for assignment in query(callback.body, "assignment_expression"):
        target = contents(assignment)[0]
        if (slot := indexed_recorder(file, contents(target))) in slots:
            slots.remove(slot)
        elif slot in checked:
            return False
    return not slots


class Absence:
    def __init__(self, file):
        self.file = file
        self.sibling_witnesses = set()
        self.detector_witnesses = set()
        self.pending = []

    def collect(self, test, flow):
        local_detectors = detector_witnesses(self.file, flow)
        self.detector_witnesses.update(local_detectors)
        writes = defaultdict(list)
        for file, key, site, sink in flow.writes:
            if file is self.file:
                writes[key].append((site, sink))

        witnesses, negatives = set(), []
        for check in flow.checks:
            if check.file is not self.file:
                continue
            key, present = self.file.observation(check)
            if key not in writes:
                continue
            if present is True and not check.guarded:
                witnesses.add(key)
                self.sibling_witnesses.update(sink for _, sink in writes[key] if sink)
            elif present is False:
                negatives.append((key, line(check.node)))

        checked = checked_result_slots(self.file, flow) if negatives else set()
        cb_witnessed = {key for group in flow.unconditional_cb_writes if group & witnesses for key in group}
        for key, site in negatives:
            if (key in witnesses or key in cb_witnessed
                    or checked and covers_success_paths(self.file, key, writes[key], checked, flow.callback)):
                continue
            sinks = {sink for _, sink in writes[key] if sink is not None}
            detectors = [collector_dependencies(self.file, write) for write, sink in writes[key] if sink is None]
            self.pending.append((test, site, sinks, detectors))

    def finish(self):
        for test, site, sinks, detectors in self.pending:
            if sinks - self.sibling_witnesses or any(
                    required - self.detector_witnesses and not alternatives & self.detector_witnesses
                    for required, alternatives in detectors):
                test.issues.append((site, "absence-no-control"))
