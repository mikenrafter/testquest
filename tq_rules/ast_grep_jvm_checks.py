"""JVM assertion operands, literal proofs and recorder observations."""

from dataclasses import dataclass, field
import re

from . import lint
from .ast_grep import children, line, offset, query, shape, unwrap
from .ast_grep_jvm import call_of, child, member, name, string_title
from .ast_grep_jvm_signatures import accepts, imported_method, signature, soft_recorder
from .ast_grep_lint import COMPARISONS, literal as stage_literal, range_size


UNKNOWN = lint.UNKNOWN
EQUAL = {"assertEquals", "assertArrayEquals", "assertIterableEquals", "assertContentEquals", "isEqualTo", "shouldBe", "equalTo"}
UNEQUAL = {"assertNotEquals", "isNotEqualTo", "shouldNotBe", "notEqualTo"}
IDENTITY = {"assertSame", "isSameAs", "assertNotSame", "isNotSameAs"}
BOOLEANS = {"assertTrue", "assertFalse", "isTrue", "isFalse", "shouldBeTrue", "shouldBeFalse"}
FLUENT_ROOTS = {"assertThat", "assertThatThrownBy", "assertThatCode", "assertThatExceptionOfType",
                "assertThatNullPointerException", "assertThatIllegalArgumentException",
                "assertThatIllegalStateException", "assertThatIOException", "then"}
FLUENT_OPTIONS = {"as", "describedAs", "named", "overridingErrorMessage", "withFailMessage",
                  "because", "usingComparator", "usingDefaultComparator", "usingRecursiveComparison",
                  "withComparator", "withMessage", "that", "comparingElementsUsing", "ignoringFields"}
FLUENT_TRANSFORMS = {"extracting", "flatExtracting", "filteredOn", "map", "flatMap", "first", "last",
                     "element", "get", "actual", "asInstanceOf"}
FLUENT_TERMINALS = ("is", "has", "have", "contains", "doesNot", "startsWith", "endsWith",
                    "matches", "satisfies", "allSatisfy", "anySatisfy", "noneSatisfy", "fails",
                    "succeeded", "was", "starts", "ends", "completes", "lies", "exists",
                    "anyMatch", "allMatch", "noneMatch", "canRead", "canWrite")
KOTEST_EXACT = {"should", "shouldNot", "shouldBe", "shouldNotBe", "shouldHave", "shouldNotHave"}
KOTEST_PREFIXES = ("shouldBe", "shouldNotBe", "shouldHave", "shouldNotHave", "shouldThrow",
                   "shouldNotThrow", "shouldContain", "shouldNotContain", "shouldStart", "shouldEnd",
                   "shouldMatch", "shouldNotMatch", "shouldEqual", "shouldNotEqual", "shouldFail")
VERIFICATIONS = {"verify", "verifyBlocking", "coVerify", "verifySequence", "coVerifySequence",
                 "verifyOrder", "coVerifyOrder", "verifyAll", "coVerifyAll"}
NO_INTERACTIONS = {"verifyZeroInteractions", "verifyNoInteractions", "verifyNoMoreInteractions",
                   "confirmVerified"}
BINARY_KINDS = {"binary_expression", "equality_expression", "comparison_expression",
                "conjunction_expression", "disjunction_expression", "elvis_expression"}
INTEGER_KINDS = {"integer_literal", "hex_literal", "bin_literal", "decimal_integer_literal",
                 "hex_integer_literal", "binary_integer_literal", "octal_integer_literal"}
SYNCHRONOUS = {"assertAll", "assertSoftly", "assertTimeout", "assertTimeoutPreemptively", "runTest",
               "runBlocking", "run", "apply", "also", "let", "with", "use", "synchronized",
               "assumingThat", "assertDoesNotThrow", "assertNotNull", "assertIs", "assertIsNot",
               "eventually", "continually", "assertEach", "runOnIdle", "runOnUiThread", "runOnMainSync",
               "onActivity", "runComposeUiTest", "runAndroidComposeUiTest", "dynamicTest",
               "coroutineScope", "supervisorScope", "withContext", "withTimeout"}
ITERATING = {"forEach", "forEachIndexed", "map", "mapIndexed", "onEach", "all", "any", "none",
             "filter", "filterNot", "repeat"}


def split(node):
    node = unwrap(node)
    if node is not None and node.kind() in BINARY_KINDS:
        elements = [entry for entry in node.children() if entry.kind() not in
                    {"comment", "line_comment", "block_comment", "multiline_comment"}]
        if len(elements) == 3:
            left, operator, right = elements
            return operator.text(), left, right
    return "", None, None


def negate(node):
    node = unwrap(node)
    if node is not None and node.kind() in {"prefix_expression", "unary_expression"}:
        entries = node.children()
        if len(entries) == 2 and entries[0].kind() == "!":
            return entries[1]
    return None


def literal(syntax, node):
    node = unwrap(node)
    if node is None:
        return UNKNOWN
    kind = node.kind()
    if kind == "string_literal":
        title = string_title(node)
        return title if title is not None and "\\" not in title else UNKNOWN
    if kind in {"null", "null_literal"}:
        return None
    if kind in INTEGER_KINDS:
        word = node.text().replace("_", "").lower().removesuffix("l")
        if word.endswith("u"):
            return UNKNOWN
        base = 8 if kind == "octal_integer_literal" else 0 if word.startswith(("0x", "0b")) else 10
        try:
            value = int(word, base)
        except ValueError:
            return UNKNOWN
        if value < 2 ** 63 and (base == 10 or value < 2 ** 31):
            return value
        return UNKNOWN
    if kind in {"real_literal", "decimal_floating_point_literal"}:
        if node.text()[-1:].lower() == "f":
            return UNKNOWN
        try:
            return float(node.text().replace("_", "").lower().removesuffix("d"))
        except ValueError:
            return UNKNOWN
    if kind in {"prefix_expression", "unary_expression"} and node.children()[0].kind() in {"-", "+"}:
        operand = literal(syntax, children(node)[-1])
        if type(operand) in {float, int}:
            return -operand if node.children()[0].kind() == "-" else operand
    return stage_literal(syntax, node)


def compare(operator, first, second):
    if UNKNOWN in (first, second) or type(first) is not type(second):
        return UNKNOWN
    if operator in {"==", "!="}:
        return COMPARISONS[operator](first, second)
    if type(first) in {int, float} and operator in COMPARISONS:
        return COMPARISONS[operator](first, second)
    return UNKNOWN


def truth(syntax, node):
    value = literal(syntax, node)
    if type(value) is bool:
        return value
    if (inner := negate(node)) is not None:
        value = truth(syntax, inner)
        return not value if value is not UNKNOWN else UNKNOWN
    operator, left, right = split(node)
    if operator in {"&&", "||"}:
        values = (truth(syntax, left), truth(syntax, right))
        decisive = operator == "||"
        if any(value is decisive for value in values):
            return decisive
        if UNKNOWN not in values:
            return not decisive
    return compare(operator, literal(syntax, left), literal(syntax, right))


def iterable_size(syntax, node):
    node = unwrap(node)
    if node is None:
        return None
    if node.kind() == "array_initializer":
        return len(children(node))
    if node.kind() == "array_creation_expression":
        if (initializer := child(node, "array_initializer")) is not None:
            return len(children(initializer))
        size = literal(syntax, child(child(node, "dimensions_expr"), *INTEGER_KINDS))
        return size if type(size) is int and size >= 0 else None
    if node.kind() in {"range_expression", "infix_expression"}:
        entries = children(node)
        if len(entries) in {2, 3} and entries:
            operator = (name(entries[1]) if len(entries) == 3 else
                        next((token.kind() for token in node.children() if token.kind() in {"..", "..<"}), ""))
            first, last = (literal(syntax, target) for target in (entries[0], entries[-1]))
            if type(first) is type(last) is int:
                if operator in {"..", "..<", "until"}:
                    return range_size(first, last + int(operator == ".."))
                if operator == "downTo":
                    return range_size(first, last - 1, -1)
    if (call := call_of(node)) is not None:
        factories = {"listOf", "mutableListOf", "arrayOf", "booleanArrayOf", "byteArrayOf", "intArrayOf",
                     "longArrayOf", "doubleArrayOf", "floatArrayOf", "charArrayOf", "shortArrayOf",
                     "setOf", "mutableSetOf", "sequenceOf", "mapOf", "mutableMapOf"}
        if call.receiver is None and call.method in factories:
            return sum(arg.kind() != "spread_expression" for arg in call.args)
        if call.receiver is None and call.method in {"emptyList", "emptyArray", "emptySet", "emptySequence", "emptyMap"}:
            return 0
        if call.receiver is not None:
            _, owner = member(call.receiver)
            if (owner, call.method) in {("List", "of"), ("Set", "of"), ("Collections", "singletonList"),
                                       ("Collections", "singleton")}:
                return len(call.args)
    value = literal(syntax, node)
    return len(value) if type(value) is str and syntax.language == "kotlin" else None


@dataclass
class Check:
    node: object
    predicate: object = None
    operator: str = ""
    left: object = None
    right: object = None
    negated: bool = False
    observations: tuple = ()
    fragment_subject: object = None
    snapshot_subject: object = None
    fragment_needle: object = None
    fragment_negative: bool = False
    no_throw: bool = False
    message: str = ""
    controls: dict = field(default_factory=dict)
    disabled: frozenset = frozenset()
    sizes: tuple = ()
    actual: object = None
    expected: object = None
    soft_receiver: object = None

    def constant(self, syntax):
        if self.predicate is not None:
            return truth(syntax, self.predicate) is (not self.negated)
        return compare(self.operator, literal(syntax, self.left), literal(syntax, self.right)) is True


def equality(node, method, first, second, *, expected_first=False):
    if method in EQUAL | UNEQUAL | IDENTITY and first is not None and second is not None:
        operator = "!=" if method in UNEQUAL else "==" if method in EQUAL else (
            "is not" if method in {"assertNotSame", "isNotSameAs"} else "is")
        expected, actual = (first, second) if expected_first else (second, first)
        return Check(node, operator=operator, left=first, right=second, actual=actual, expected=expected)
    return Check(node)


def predicate(node, subject, negated):
    operator, left, right = split(subject)
    if operator in {"===", "!=="}:
        operator = "is" if operator == "===" else "is not"
    if negated:
        operator = {"==": "!=", "!=": "==", "is": "is not", "is not": "is"}.get(operator, "")
    return Check(node, subject, operator, left, right, negated)


def compares_itself(syntax, check, title, notes, checks):
    if check.operator not in {"==", "!=", "is", "is not"} or check.left is None or check.right is None:
        return False
    lhs = shape(check.left)
    if lhs != shape(check.right):
        return False
    if check.operator in {"!=", "is not"} or not query(check.left, "call_expression" if syntax.language == "kotlin" else "method_invocation"):
        return True
    site = line(check.node)
    comment = " ".join(notes.get(at, "") for at in (site - 1, site))
    if any(lint.describes_distinct_inputs(text) for text in (title, comment, check.message)):
        return True
    if any(lint.suggests_repeatability(text) for text in (title, comment, check.message)):
        return False
    if check.operator == "is" and re.search(
            r"\b(?:second|subsequent|repeated)\b.*\b(?:cache|cached|from memory)\b",
            check.message, flags=re.I):
        return False
    for other in checks:
        if other is check:
            continue
        for operand, expected in ((other.left, other.right), (other.right, other.left)):
            if shape(operand) == lhs and (
                    other.operator == "!=" and shape(expected) != lhs
                    or other.operator == "==" and (literal(syntax, expected) is not UNKNOWN
                                                    or iterable_size(syntax, expected) is not None)):
                return False
    return True


def kotest_matcher(method):
    return method in KOTEST_EXACT or method.startswith(KOTEST_PREFIXES)


def fluent_origin(file, node, *, preserves_size=False):
    transformed = False
    while node is not None:
        if (call := call_of(node)) is None:
            initializer = file.stable_initializer(node, *file.visible_binding(node, name(node)))
            if initializer is None or offset(initializer) >= offset(node):
                break
            node = initializer
            continue
        owner, method = signature(file, call)
        if method in FLUENT_ROOTS:
            if owner not in {"", "assertj", "soft-assertj", "truth"}:
                return False, None, None
            if method == "then" and (not call.args or call.callbacks()):
                return False, None, None
            subject = call.args[0] if method in {"assertThat", "then"} and call.args else None
            buffered = soft_recorder(file, call.receiver) if owner == "soft-assertj" else None
            return True, None if transformed else subject, buffered
        if method == "that" and call.args:
            sender = call_of(call.receiver)
            if sender is not None:
                owner, builder = signature(file, sender)
                if owner in {"", "truth"} and builder in {"assertWithMessage", "withMessage", "assert_"}:
                    return True, None if transformed else call.args[0], None
        transformed |= method in FLUENT_TRANSFORMS and not (preserves_size and method in {"extracting", "map"})
        if method not in FLUENT_OPTIONS | FLUENT_TRANSFORMS and not method.startswith(FLUENT_TERMINALS):
            break
        node = call.receiver
    return False, None, None


def junit_args(file, call, owner, method):
    args = [arg for arg in call.args if not any(arg is named for named in call.named.values())]
    if method in BOOLEANS | {"assertNotNull"}:
        if "actual" in call.named:
            return [call.named["actual"]]
        slots = 1
    else:
        if "actual" in call.named or "expected" in call.named:
            positional = iter(args)
            return [call.named[label] if label in call.named else next(positional, None)
                    for label in ("expected", "actual")]
        slots = 2
    if len(args) > slots:
        initial_call = call_of(args[0])
        message = (args[0].kind() == "string_literal" or initial_call is not None and
                   initial_call.method in {"buildString", "getMessage", "format"})
        numeric_delta = (method in {"assertEquals", "assertNotEquals", "assertArrayEquals"}
                         and len(args) == 3 and not message and
                         type(literal(file.syntax, args[-1])) in {int, float})
        if (owner == "junit4" and not numeric_delta or owner == "" and
                file.syntax.language == "java" and args[0].kind() == "string_literal"):
            return args[1:]
    return args


def check_call(file, node, call):
    owner, method = signature(file, call)
    first = call.args[0] if call.args else None
    if owner and not accepts(owner, method) and call.receiver is None:
        return None
    if method in {"assertAll", "assertSoftly"}:
        return None
    if method in {"assertDoesNotThrow", "shouldNotThrow", "shouldNotThrowAny"} and first is not None:
        return Check(node, no_throw=True)
    if method == "assertThat":
        matcher = call_of(call.args[-1]) if len(call.args) >= 2 else None
        if owner in {"hamcrest", "junit4"} or not owner and matcher is not None:
            if len(call.args) >= 2:
                subject = call.args[-2]
                return hamcrest_check(file, node, subject, matcher)
        return None
    if method in FLUENT_ROOTS | {"assertWithMessage", "assert_"} | VERIFICATIONS:
        if method == "assertThatThrownBy" and owner in {"", "assertj", "soft-assertj"}:
            return Check(node)
        if method in VERIFICATIONS and owner in {"", "mockk"} and call.callbacks():
            from .ast_grep_jvm_controls import disabled_verifications

            check = mockk_observations(file, Check(node), call)
            if check.controls:
                check.disabled = frozenset(disabled_verifications(file, call))
            return check
        return None
    if method in {"assertGolden", "assertSnapshot", "assertMatchesGolden", "assertMatchesSnapshot",
                  "verifySnapshot"} and first is not None:
        return Check(node, snapshot_subject=call.named.get("actual", first))
    if method in NO_INTERACTIONS and owner in {"", "mockk", "mockito"}:
        from .ast_grep_jvm_controls import verified_call

        check = Check(node)
        for arg in call.args:
            record_observation(check, (key for key in verified_call(file, arg, "")
                                       if key[0] in {"calls", "mock-type", "installed-mock"}), False)
        return check
    if call.receiver is not None:
        if ((verified := call_of(call.receiver)) is not None
                and signature(file, verified) in {("", "verify"), ("mockito", "verify")}):
            return mockito_observations(file, Check(node), verified, call.method)
        found, subject, buffered = fluent_origin(file, call.receiver)
        if found and method not in FLUENT_OPTIONS | FLUENT_TRANSFORMS and method.startswith(FLUENT_TERMINALS):
            check = (predicate(node, subject, method in {"isFalse", "shouldBeFalse"})
                     if method in BOOLEANS else equality(node, method, subject, first))
            if subject is None and method in {"hasSize", "containsExactly", "containsOnly", "contains", "isNotEmpty"}:
                _, subject, _ = fluent_origin(file, call.receiver, preserves_size=True)
            check.soft_receiver = buffered
            return collect_observations(file, check, method, subject, first)
        if method == "check" and first is not None and call_of(first) is not None and (
                call_of(first).method in {"matches", "doesNotExist", "selectedDescendantsMatch"}):
            return Check(node)
    if kotest_matcher(method) and owner in {"", "foreign", "kotest"} and (
            owner != "foreign" or imported_method(file, call.method)[0] in {"", "kotest"}):
        if method in BOOLEANS:
            return collect_observations(file, predicate(node, call.receiver, method == "shouldBeFalse"))
        return collect_observations(file, equality(node, method, call.receiver, first),
                                    method, call.receiver, first)
    if owner and not accepts(owner, method):
        return None
    if method in BOOLEANS:
        operands = junit_args(file, call, owner, method)
        if operands and operands[0] is not None:
            return collect_observations(file, predicate(node, operands[0], method == "assertFalse"))
        return None
    if method in EQUAL | UNEQUAL | IDENTITY:
        operands = junit_args(file, call, owner, method)
        if len(operands) >= 2:
            check = equality(node, method, *operands[:2], expected_first=True)
            if method == "assertSame" and len(call.args) == 3:
                message = next((arg for arg in call.args if all(arg is not operand for operand in operands[:2])), None)
                value = literal(file.syntax, message)
                if isinstance(value, str):
                    check.message = value
            return collect_observations(file, check)
        return None
    if method == "assertNotNull":
        operands = junit_args(file, call, owner, method)
        return collect_observations(file, Check(node), method, operands[0] if operands else None)
    if lint.assertion(method) or method == "fail" or file.imports.get(call.method) and lint.helper(method):
        return Check(node)
    return None


def hamcrest_check(file, node, subject, matcher):
    if matcher is None:
        return Check(node)
    inverse = False
    while matcher.method in {"is", "not"} and matcher.args:
        inverse ^= matcher.method == "not"
        inner = call_of(matcher.args[0])
        if inner is None:
            check = equality(node, "assertNotEquals" if inverse else "assertEquals", subject, matcher.args[0])
            return collect_observations(file, check)
        matcher = inner
    expected = next(iter(matcher.args), None)
    method = matcher.method
    if inverse and method in {"equalTo", "isEmpty", "empty", "emptyIterable"}:
        method = "notEqualTo" if method == "equalTo" else "isNotEmpty"
    check = equality(node, method, subject, expected)
    if inverse and method == "contains":
        check.fragment_negative = True
    return collect_observations(file, check, method, subject, expected)


def infix_check(file, node):
    bindings = children(node)
    if len(bindings) == 3:
        subject, identifier, expected = bindings
        owner, method = imported_method(file, name(identifier))
        if owner in {"", "kotest"} and kotest_matcher(method):
            return collect_observations(file, equality(node, method, subject, expected), method, subject, expected)
    return None


def mockk_observations(file, check, call):
    exact, least, most = (literal(file.syntax, call.named.get(key)) for key in ("exactly", "atLeast", "atMost"))
    if exact == 0 or most == 0:
        present = False
    elif (type(exact) is int and exact > 0 or type(least) is int and least > 0
          or all(key not in call.named for key in ("exactly", "atLeast", "atMost"))):
        present = True
    else:
        return check
    from .ast_grep_jvm_controls import verified_call

    for callback in call.callbacks():
        statements = child(callback, "statements")
        if statements is None:
            continue
        for entry in children(statements):
            recorded = call_of(entry)
            if recorded is None:
                continue
            keys = verified_call(file, recorded.receiver, recorded.method)
            record_observation(check, (key for key in keys if not present or key[0] != "captured-mock"), present)
    return check


def record_observation(check, keys, present):
    keys = tuple(keys)
    if not keys:
        return
    if present:
        check.observations += tuple((key, True) for key in keys)
    else:
        check.observations += ((keys[0], False),)
        check.controls[keys[0]] = check.controls.get(keys[0], frozenset()) | frozenset(keys)


def mockito_observations(file, check, call, method):
    from .ast_grep_jvm_controls import verified_call

    if not call.args:
        return check
    if len(call.args) == 1:
        present = True
    elif (mode := call_of(call.args[1])) is not None:
        if mode.method in {"never", "atLeastOnce"}:
            present = mode.method == "atLeastOnce"
        elif mode.args and mode.method in {"times", "atLeast", "atMost"}:
            count = literal(file.syntax, mode.args[0])
            if type(count) is not int or count < 0 or mode.method == ("atMost" if count else "atLeast"):
                return check
            present = count > 0
        else:
            return check
    else:
        return check
    keys = verified_call(file, call.args[0], method)
    record_observation(check, (key for key in keys if present or key[0] != "mock-type"), present)
    return check


def string_needle(node):
    node = unwrap(node)
    return node is not None and node.kind() == "string_literal" and all(
        part.kind() in {"string_content", "string_fragment", "escape_sequence"} for part in children(node)
    )


def collect_observations(file, check, method="", subject=None, expected=None):
    if method in {"contains", "doesNotContain", "shouldContain", "shouldNotContain"} and string_needle(expected):
        check.fragment_subject = unwrap(subject)
        check.fragment_needle = expected
        check.fragment_negative |= check.negated or method in {"doesNotContain", "shouldNotContain"}

    from .ast_grep_jvm_controls import (
        captured_slot, checked_callback_collectors, collection_controls_for, collection_projection, latch_recorder,
        reflected_mock, sized_source,
    )

    def observe(receiver, present):
        record_observation(check, collection_controls_for(file, receiver, present), present)
        if present and file.syntax.language == "kotlin":
            if (source := collection_projection(file, receiver)) is not None:
                record_observation(check, collection_controls_for(file, source, True), True)
        return check

    def observed_item(node):
        node = unwrap(node)
        node = file.immutable_value(node) or node
        while (receiver := member(node)[0]) is not None:
            node = receiver
        if (call := call_of(node)) is not None and call.method in {"poll", "single", "first", "getFirst", "remove"}:
            return call.receiver
        return None

    if check.operator == "is" and file.syntax.language == "java":
        for readback, observed in ((check.left, check.right), (check.right, check.left)):
            if (recorder := reflected_mock(file, readback, observed)) is not None:
                record_observation(check, (("installed-mock", recorder),), True)

    for operand in (subject, check.left, check.right, check.predicate):
        if (slot := captured_slot(file, operand)) is not None:
            record_observation(check, (("capture-slot", slot),), True)
    tested = unwrap(check.predicate)
    if tested is not None:
        operator, left, right = split(tested)
        if operator == ("||" if check.negated else "&&"):
            for operand in (left, right):
                asserted = collect_observations(file, predicate(check.node, operand, check.negated))
                check.observations += asserted.observations
                check.controls.update(asserted.controls)
                check.sizes += asserted.sizes
        opposite = negate(tested)
        if (call := call_of(opposite or tested)) is not None and call.receiver is not None:
            if call.method == "contains" and len(call.args) == 1 and string_needle(call.args[0]):
                check.fragment_subject = unwrap(call.receiver)
                check.fragment_needle = call.args[0]
                check.fragment_negative = check.negated != (opposite is not None)
            positive = (not check.negated) != (opposite is not None)
            if file.syntax.language == "kotlin" and call_of(call.receiver) is not None:
                from .ast_grep_jvm_profiles import observed_mock_reads

                channel = "read-returned-true" if positive else "read-returned-false"
                record_observation(check, ((channel, recorder) for recorder in observed_mock_reads(
                    file, opposite or tested, call)), True)
            if (positive and call.method == "await" and len(call.args) == 2
                    and file.syntax.language == "java" and (latch := latch_recorder(file, call.receiver)) is not None):
                record_observation(check, (("awaited-latch", latch),), True)
            if call.method in {"isEmpty", "isNotEmpty"} and not call.args:
                return observe(call.receiver, positive == (call.method == "isNotEmpty"))
            if positive and call.method in {"contains", "containsKey", "containsValue", "any"}:
                return observe(call.receiver, True)
    if method == "isZero" and subject is not None and file.syntax.language == "java":
        record_observation(check, (("checked-callback-count", recorder)
                                   for recorder in checked_callback_collectors(file, subject)), True)
    if method in {"isEmpty", "isNotEmpty", "empty", "emptyIterable", "shouldBeEmpty", "shouldNotBeEmpty"}:
        return observe(subject, method in {"isNotEmpty", "shouldNotBeEmpty"})
    if method in {"assertNotNull", "isNotNull"} and (receiver := observed_item(subject)) is not None:
        return observe(receiver, True)
    if method in {"contains", "containsOnly", "containsAnyOf", "containsExactly", "containsExactlyElementsIn",
                  "containsAtLeast", "shouldContain", "hasSizeGreaterThan", "hasSizeLessThan", "hasSize",
                  "shouldHaveSize"}:
        length = literal(file.syntax, expected)
        if method in {"hasSize", "shouldHaveSize", "hasSizeGreaterThan", "hasSizeLessThan"}:
            if type(length) is int and length >= 0:
                if method == "hasSizeLessThan" and length == 1:
                    return observe(subject, False)
                if method != "hasSizeLessThan":
                    if method in {"hasSize", "shouldHaveSize"} and (key := sized_source(file, subject)) is not None:
                        check.sizes += ((key, length),)
                    return observe(subject, length > 0 or method == "hasSizeGreaterThan")
        elif method == "containsExactlyElementsIn":
            known = iterable_size(file.syntax, expected)
            if known is not None:
                return observe(subject, known > 0)
        elif expected is not None or method == "containsExactly":
            return observe(subject, expected is not None)
    if check.operator in COMPARISONS:
        reverse = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
        for operand, expected, relation in ((check.left, check.right, check.operator),
                                             (check.right, check.left, reverse.get(check.operator, check.operator))):
            if relation == "==" and (receiver := observed_item(operand)) is not None:
                value = literal(file.syntax, expected)
                item = file.immutable_value(unwrap(operand)) or unwrap(operand)
                while (base := member(item)[0]) is not None:
                    item = base
                read = call_of(item)
                if (value is not UNKNOWN and value is not None
                        or read is not None and read.method in {"single", "first", "getFirst"}
                        or (size := iterable_size(file.syntax, expected)) is not None and size > 0):
                    return observe(receiver, True)
            if relation == "==" and call_of(expected) is not None and (size := iterable_size(
                    file.syntax, expected)) is not None and size > 0:
                if file.collection_recorder(operand) is not None or (
                        file.syntax.language == "kotlin" and collection_projection(file, operand) is not None):
                    return observe(operand, True)
            if relation == "==" and type(count := literal(file.syntax, expected)) is int and count == 0:
                if file.syntax.language == "java" and operand is not None:
                    record_observation(check, (("checked-callback-count", recorder)
                                               for recorder in checked_callback_collectors(file, operand)), True)
            operand = file.immutable_value(unwrap(operand)) or unwrap(operand)
            receiver, property_name = member(operand)
            measurement = call_of(operand)
            if measurement is not None and (not measurement.args or measurement.method == "count"):
                receiver, property_name = measurement.receiver, measurement.method
            count = literal(file.syntax, expected)
            if property_name not in {"size", "length", "count"} or type(count) is not int or count < 0:
                continue
            if relation == "==" and (key := sized_source(file, receiver)) is not None:
                check.sizes += ((key, count),)
            if relation == "==" or count == 0 and relation in {"!=", ">", "<="} or count == 1 and relation == "<":
                return observe(receiver, (count > 0) if relation == "==" else relation in {"!=", ">"})
            if relation in {">", ">="} and count > 0:
                return observe(receiver, True)
    return check
