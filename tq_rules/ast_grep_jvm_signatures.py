"""Resolve framework assertion owners from JVM source bindings and imports."""

from functools import lru_cache
from pathlib import Path

from .ast_grep import children, unwrap
from .ast_grep_jvm import LAMBDAS, call_of, child, name, node_key
from .source import project_root


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
    "org.assertj.core.api.BDDSoftAssertions": "soft-assertj",
    "org.assertj.core.api.AutoCloseableBDDSoftAssertions": "soft-assertj",
    "com.google.common.truth.Truth": "truth", "com.google.common.truth.Truth8": "truth",
    "com.google.common.truth.Expect": "truth", "com.google.common.truth.StandardSubjectBuilder": "truth",
    "org.skyscreamer.jsonassert.JSONAssert": "jsonassert",
    "org.mockito.Mockito": "mockito", "org.mockito.BDDMockito": "mockito",
    "org.mockito.InOrder": "mockito",
    "io.mockk": "mockk",
    "assertk": "assertk", "assertk.assertions": "assertk",
    "reactor.test.StepVerifier": "stepverifier",
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
API_SOURCE_MARKERS = {
    "assertj": ("org/assertj/core/api/Assertions.java", "org/assertj/core/api/AbstractAssert.java"),
    "soft-assertj": ("org/assertj/core/api/SoftAssertions.java", "org/assertj/core/api/Assertions.java"),
    "jupiter": ("org/junit/jupiter/api/Assertions.java",),
    "junit4": ("org/junit/Assert.java", "junit/framework/Assert.java"),
    "truth": ("com/google/common/truth/Truth.java",),
}


def family(owner):
    if owner == "assertk" or owner.startswith("assertk."):
        return "assertk"
    if owner.startswith(("io.kotest.matchers", "io.kotest.assertions")):
        return "kotest"
    if owner in OWNERS:
        return OWNERS[owner]
    if owner.startswith("org.assertj."):
        if owner.endswith(("SoftAssertions", "BDDSoftAssertions")):
            return "soft-assertj"
        if owner.startswith("org.assertj.core.internal.") or owner.endswith(
                ("Assertions", "BDDAssertions", "AssertionsForClassTypes", "AssertionsForInterfaceTypes")):
            return "assertj"
    if owner.startswith("com.google.common.truth.") and owner.endswith(("Truth", "Truth8")):
        return "truth"
    return "foreign"


@lru_cache(maxsize=64)
def _repo_defines_api(repo_root, owner):
    markers = API_SOURCE_MARKERS.get(owner)
    if not markers or repo_root is None:
        return False
    roots = [repo_root]
    try:
        for entry in repo_root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                roots.append(entry)
    except OSError:
        pass
    for root in roots:
        main_java = root / "src" / "main" / "java"
        if any((main_java / marker).is_file() for marker in markers):
            return True
    return False


def defines_assertion_api(file, owner):
    """Return True when the scanned repository's main source defines the given assertion API."""
    if owner not in API_SOURCE_MARKERS:
        return False
    if file.module is not None and _repo_defines_api(file.module, owner):
        return True
    repo_root = project_root(file.path, None, {".git"})
    return repo_root != file.module and _repo_defines_api(repo_root, owner)


def accepts(owner, method):
    if owner in ASSERTION_OWNERS:
        return method in COMMON_ASSERTIONS | JUNIT_METHODS[owner]
    if owner == "hamcrest":
        return method == "assertThat"
    if owner in {"mockk", "mockito"}:
        return method.startswith(("verify", "coVerify")) or method == "confirmVerified"
    if owner in {"assertj", "soft-assertj", "truth"}:
        return (method.startswith("assertThat")
                or method == "then"
                or method in {"assertAny", "assertWithMessage", "assert_", "assertSoftly", "assertAll", "fail"}
                or method.startswith(("thenExceptionOfType", "thenThrownBy", "thenCode", "thenNoException"))
                or (method.startswith("then") and method.endswith("Exception")))
    if owner == "stepverifier":
        return method in {"create", "withVirtualTime"} or method.startswith(("expect", "verify", "consume", "assert"))
    if owner == "jsonassert":
        return method in {"assertEquals", "assertNotEquals"}
    if owner == "kotest":
        return method.startswith(("assert", "should", "fail"))
    if owner == "assertk":
        return method.startswith(("assertThat", "assertFailure", "assertSuccess")) or method in {"assertAll", "fail"}
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


def _is_local_soft_assertions(file, declared_type):
    local_type = file.types.get(declared_type)
    if local_type is None:
        return False
    text = local_type.text().split("{", 1)[0]
    return any(marker in text for marker in (
        "AbstractSoftAssertions", "SoftAssertionsProvider", "StandardSoftAssertionsProvider",
        "BDDSoftAssertions", "SoftAssertions"))


def signature(file, call):
    receiver = unwrap(call.receiver)
    if receiver is None:
        return imported_method(file, call.method)
    static = receiver.text()
    root = static.split(".", 1)[0]
    if name(receiver) and file.visible_binding(receiver, root)[1] is not None:
        _, binding = file.visible_binding(receiver, root)
        declared_node = binding.field("type") or (
            binding.parent().field("type") if binding.parent() is not None else None)
        declared_type = declared_node.text().split("<", 1)[0] if declared_node is not None else ""
        if declared_type:
            owner = family(file.imports.get(declared_type, declared_type))
            if owner in {"assertj", "soft-assertj", "truth", "mockito", "stepverifier"}:
                return owner, call.method
            if declared_type in {"BDDSoftAssertions", "AutoCloseableBDDSoftAssertions", "SoftAssertions",
                                 "AutoCloseableSoftAssertions"} or _is_local_soft_assertions(file, declared_type):
                return "soft-assertj", call.method
            if declared_type in {"WithAssertions", "WithBDDAssertions", "BDDAssertions"}:
                return "assertj", call.method
            if declared_type in {"Expect", "StandardSubjectBuilder"}:
                return "truth", call.method
        value = file.initializer(binding)
        if value is not None:
            constructor = value.field("type") if value.kind() == "object_creation_expression" else None
            called = call_of(value)
            declared = constructor.text().split("<", 1)[0] if constructor is not None else called.method if called is not None else ""
            owner = family(file.imports.get(declared, declared))
            if owner == "soft-assertj" or declared in {"SoftAssertions", "AutoCloseableSoftAssertions",
                                                      "BDDSoftAssertions", "AutoCloseableBDDSoftAssertions"} or _is_local_soft_assertions(file, declared):
                return "soft-assertj", call.method
            if declared in {"WithAssertions", "WithBDDAssertions", "BDDAssertions"}:
                return "assertj", call.method
            if declared in {"Expect", "StandardSubjectBuilder"} or (
                    called is not None and called.method == "create" and name(called.receiver) == "Expect"):
                return "truth", call.method
            if declared == "inOrder" or owner == "mockito":
                return "mockito", call.method
        if declared_type == "var" and name(receiver) in {"order", "inOrder"} and call.method.startswith("verify"):
            return "mockito", call.method
        return ("soft-assertj" if soft_callback(file, receiver) else "foreign"), call.method
    if root in file.imports:
        qualified = file.imports[root] + static[len(root):]
        return family(qualified), call.method
    if static.startswith(("org.", "io.", "kotlin.", "com.", "junit.", "reactor.")):
        return family(static), call.method
    if soft_callback(file, receiver):
        return "soft-assertj", call.method
    if root in file.types or static == "this":
        return "foreign", call.method
    return {"Assert": "junit4", "Assertions": "jupiter", "MatcherAssert": "hamcrest",
            "SoftAssertions": "soft-assertj", "AutoCloseableSoftAssertions": "soft-assertj",
            "BDDSoftAssertions": "soft-assertj", "AutoCloseableBDDSoftAssertions": "soft-assertj",
            "BDDAssertions": "assertj", "Truth": "truth", "ProtoTruth": "truth",
            "Mockito": "mockito", "StepVerifier": "stepverifier"}.get(
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
        elif call.method == "map" and call.receiver is not None and len(call.callbacks()) == 1:
            from .ast_grep_jvm import body_of

            body = unwrap(body_of(call.callbacks()[0]))
            if body is not None and body.kind() == "block":
                steps = children(body)
                body = unwrap(next(iter(children(steps[0])), None)) if len(steps) == 1 and steps[0].kind() == "return_statement" else None
            if body is not None and body.kind() in LAMBDAS:
                yield body
