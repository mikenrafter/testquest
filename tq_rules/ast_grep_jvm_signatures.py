"""Resolve framework assertion owners from JVM source bindings and imports."""

from .ast_grep import children, unwrap
from .ast_grep_jvm import LAMBDAS, call_of, child, name, node_key


OWNERS = {
    "org.junit.Assert": "junit4", "junit.framework.Assert": "junit4",
    "junit.framework.TestCase": "junit4", "org.junit.jupiter.api.Assertions": "jupiter",
    "org.junit.jupiter.api": "jupiter", "kotlin.test": "kotlin",
    "org.hamcrest.MatcherAssert": "hamcrest", "org.hamcrest.Matchers": "hamcrest-matcher",
    "org.hamcrest.CoreMatchers": "hamcrest-matcher", "org.assertj.core.api.Assertions": "assertj",
    "org.assertj.core.api.AssertionsForClassTypes": "assertj",
    "org.assertj.core.api.AssertionsForInterfaceTypes": "assertj",
    "org.assertj.core.api.BDDAssertions": "assertj", "org.assertj.core.api.SoftAssertions": "soft-assertj",
    "org.assertj.core.api.AutoCloseableSoftAssertions": "soft-assertj",
    "com.google.common.truth.Truth": "truth", "com.google.common.truth.Truth8": "truth",
    "org.mockito.Mockito": "mockito", "org.mockito.BDDMockito": "mockito",
    "io.mockk": "mockk",
}
ASSERTION_OWNERS = {"junit4", "jupiter", "kotlin"}
COMMON_ASSERTIONS = {"assertTrue", "assertFalse", "assertEquals", "assertNotEquals", "assertSame",
                     "assertNotSame", "assertNull", "assertNotNull", "fail"}
JUNIT_METHODS = {
    "junit4": {"assertArrayEquals", "assertThat", "assertThrows"},
    "jupiter": {"assertArrayEquals", "assertIterableEquals", "assertLinesMatch", "assertInstanceOf",
                "assertThrows", "assertThrowsExactly", "assertDoesNotThrow", "assertTimeout",
                "assertTimeoutPreemptively", "assertAll"},
    "kotlin": {"assertFails", "assertFailsWith", "assertIs", "assertIsNot", "assertContains",
               "assertContentEquals", "assertNotContains"},
}


def family(owner):
    if owner.startswith(("io.kotest.matchers", "io.kotest.assertions")):
        return "kotest"
    return OWNERS.get(owner, "foreign")


def accepts(owner, method):
    if owner in ASSERTION_OWNERS:
        return method in COMMON_ASSERTIONS | JUNIT_METHODS[owner]
    if owner == "hamcrest":
        return method == "assertThat"
    if owner in {"mockk", "mockito"}:
        return method.startswith(("verify", "coVerify")) or method == "confirmVerified"
    if owner in {"assertj", "soft-assertj", "truth"}:
        return method.startswith("assertThat") or method in {
            "then", "assertWithMessage", "assert_", "assertSoftly", "assertAll", "fail",
        }
    if owner == "kotest":
        return method.startswith(("assert", "should", "fail"))
    return False


def imported_method(file, identifier):
    if (qualified := file.imports.get(identifier)) is not None:
        owner, _, method = qualified.rpartition(".")
        return family(owner), method
    possible = {family(owner) for owner in file.wildcard_imports if accepts(family(owner), identifier)}
    return (next(iter(possible)) if len(possible) == 1 else "", identifier)


def soft_callback(file, receiver):
    identifier = name(receiver)
    if not identifier:
        return False
    for callback in receiver.ancestors():
        if callback.kind() not in LAMBDAS:
            continue
        parameters = callback.field("parameters") or child(callback, "lambda_parameters")
        names = ([name(parameters)] if name(parameters) else
                 [name(part.field("name") or child(part, "simple_identifier") or part)
                  for part in children(parameters)] if parameters is not None else ["it"])
        if names != [identifier]:
            continue
        key = node_key(callback)
        for parent in callback.ancestors():
            if parent.kind() in LAMBDAS:
                break
            call = call_of(parent)
            if call is not None and any(node_key(arg) == key for arg in call.callbacks()):
                return signature(file, call) == ("soft-assertj", "assertSoftly")
    return False


def signature(file, call):
    receiver = unwrap(call.receiver)
    if receiver is None:
        return imported_method(file, call.method)
    static = receiver.text()
    root = static.split(".", 1)[0]
    if name(receiver) and file.visible_binding(receiver, root)[1] is not None:
        _, binding = file.visible_binding(receiver, root)
        value = file.initializer(binding)
        if value is not None:
            constructor = value.field("type") if value.kind() == "object_creation_expression" else None
            called = call_of(value)
            declared = constructor.text() if constructor is not None else called.method if called is not None else ""
            if family(file.imports.get(declared, declared)) == "soft-assertj":
                return "soft-assertj", call.method
        return ("soft-assertj" if soft_callback(file, receiver) else "foreign"), call.method
    if root in file.imports:
        qualified = file.imports[root] + static[len(root):]
        return family(qualified), call.method
    if static.startswith(("org.", "io.", "kotlin.", "com.", "junit.")):
        return family(static), call.method
    if soft_callback(file, receiver):
        return "soft-assertj", call.method
    if root in file.types or static == "this":
        return "foreign", call.method
    return {"Assert": "junit4", "Assertions": "jupiter", "MatcherAssert": "hamcrest",
            "SoftAssertions": "soft-assertj", "Truth": "truth", "Mockito": "mockito"}.get(
                static, ""), call.method


def aggregate(file, call):
    owner, method = signature(file, call)
    if method == "assertAll" and owner in {"jupiter", ""}:
        return "executables"
    if method == "assertSoftly" and owner in {"soft-assertj", "kotest", ""}:
        return "soft"
    return ""


def soft_recorder(file, receiver):
    if soft_callback(file, receiver):
        return None
    return file.recorder(receiver)


def executable_callbacks(file, args):
    for arg in args:
        value = unwrap(arg)
        if value is None:
            continue
        if value.kind() in LAMBDAS:
            yield value
            continue
        initial = file.immutable_value(value)
        if initial is not None and node_key(initial) != node_key(value) and initial.kind() in LAMBDAS:
            yield initial
            continue
        call = call_of(value)
        if call is None:
            continue
        receiver = call.receiver.text() if call.receiver is not None else ""
        resolved = file.imports.get(receiver, receiver)
        if (resolved, call.method) in {
                ("java.util.List", "of"), ("java.util.Arrays", "asList"), ("java.util.stream.Stream", "of"),
                ("", "listOf"), ("", "arrayOf")}:
            yield from executable_callbacks(file, call.args)
