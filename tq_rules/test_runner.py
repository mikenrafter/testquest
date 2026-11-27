"""Expand a single-test command and classify evidence from the runner's output."""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import shlex
import sys

from . import catches, repo
from .ast_grep_languages import ECMASCRIPT_LANGUAGES, JVM_LANGUAGES


DEFAULT = shlex.join([sys.executable, "-m", "unittest", "{test}"])
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ASSERTION = r"(?:[\w.]*Assertion(?:Error|FailedError)|ComparisonFailure|ERR_ASSERTION)"
RUNNERS = {
    "unittest": (
        r"^ERROR:|^FAILED \([^\n]*\berrors=|^Ran 0 tests?\b",
        r"^FAIL:\s|^FAILED \(failures=[1-9]\d*\)",
        r"^OK[ \t]*$",
    ),
    "pytest": (
        r"^_+.*(?:ERROR collecting|ERROR at (?:setup|teardown))|\b[1-9]\d* errors?\b|^ERROR ",
        rf"^E\s+{ASSERTION}\b|^FAILED .+ - {ASSERTION}\b",
        r"\b[1-9]\d* passed\b",
    ),
    "rust-libtest": (
        r"^error(?:\[E\d+\])?: (?!test failed)|^error: could not compile",
        r"^assertion (?:failed:|.+ failed(?::|$))",
        r"^test result: ok\. [1-9]\d* passed\b",
    ),
    "javascript": (
        r"^(?:Test suite failed to run|Error: Cannot find (?:module|package))"
        r"|\b(?:ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND|error TS\d{4})\b|^\s*SyntaxError:",
        rf"\b{ASSERTION}\b|^\s*expect\(.+\)\..+",
        r"^(?:#|ℹ) pass [1-9]\d*\b|^\s*Tests:\s+[1-9]\d* passed|^\s*Tests\s+[1-9]\d* passed"
        r"|^\s*Tests:\s+.*\b[1-9]\d* passed\b",
    ),
    "dart-test": (
        r"Failed to load|(?:Compilation|Compiler) failed|^.*:\d+:\d+: Error:",
        r"^\s*Expected:.*\n\s*Actual:",
        r"\+[1-9]\d*:\s+All tests passed!",
    ),
    "junit": (
        r"(?i:compilation (?:error|failure)|initializationError|No tests found|"
        r"ClassNotFoundException|NoClassDefFoundError)|Tests run:.*\bErrors: [1-9]",
        rf"\b{ASSERTION}\b",
        r"Tests run: [1-9]\d*, Failures: 0, Errors: 0|\[\s*[1-9]\d* tests successful\s*\]"
        r"|\b[1-9]\d* tests? completed\b",
    ),
}


@dataclass(frozen=True)
class Run:
    classifier: str
    outcome: str
    detail: str

    def row(self, test):
        return dict(test=test, **asdict(self))


class Runner:
    def __init__(self, command):
        self.template = shlex.split(command)
        if not any("{test}" in part for part in self.template):
            raise ValueError("--cmd requires a {test} placeholder to run one named test")
        self.is_pytest = any(Path(part).name in {"pytest", "py.test"} for part in self.template)

    def classifier(self, path):
        suffix = path.suffix
        if suffix in ECMASCRIPT_LANGUAGES:
            return "javascript"
        if suffix in JVM_LANGUAGES:
            return "junit"
        return {".py": "pytest" if self.is_pytest else "unittest",
                ".rs": "rust-libtest", ".dart": "dart-test"}[suffix]

    def selector(self, path, test):
        if path.suffix == ".py" and self.is_pytest:
            return f"{path}::{test.qualified}"
        if path.suffix in ECMASCRIPT_LANGUAGES:
            return test.qualified.replace("::", " ")
        return test.identifier

    def run(self, directory, path, selector):
        classifier = self.classifier(path)
        command = [part.replace("{test}", selector) for part in self.template]
        environment = repo.git_environment()
        environment["GIT_CEILING_DIRECTORIES"] = str(directory.parent)
        environment["NO_COLOR"] = "1"
        result, error = catches.invoke(command, directory, environment)
        if result is None:
            return Run(classifier, "error", error)
        output = ANSI.sub("", result.stdout + "\n" + result.stderr).replace("\r", "\n")
        detail = output.strip()
        invalid, assertion, passed = RUNNERS[classifier]
        if (result.returncode == 0 and not re.search(invalid, output, re.MULTILINE)
                and re.search(passed, output, re.MULTILINE)):
            return Run(classifier, "pass", "")
        if (result.returncode in {1, 101} and not re.search(invalid, output, re.MULTILINE)
                and re.search(assertion, output, re.MULTILINE)):
            return Run(classifier, "assertion", detail)
        return Run(classifier, "error", f"Exit {result.returncode}. {detail}".strip())
