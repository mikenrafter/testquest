"""Resolve Dart imports within the current package."""

from dataclasses import dataclass
from pathlib import Path
import re

from .ast_grep import Syntax, query
from .ast_grep_dart_syntax import between, contents, identifier, string, tokens


@dataclass(frozen=True)
class Import:
    uri: str
    prefix: str
    show: frozenset
    hide: frozenset

    def exposes(self, name):
        return (not self.show or name in self.show) and name not in self.hide


def imports(root):
    for entry in query(root, "import_specification"):
        uri = entry.find(kind="string_literal")
        if uri is None or (uri := string(uri)) is None:
            continue
        prefix = between(tokens(entry), "as")
        combinators = {"show": set(), "hide": set()}
        for clause in contents(entry):
            if clause.kind() == "combinator":
                operator, *targets = tokens(clause)
                combinators[operator.text()].update(identifier(target) for target in targets if identifier(target))
        yield Import(uri, identifier(prefix[0]) if prefix else "",
                     frozenset(combinators["show"]), frozenset(combinators["hide"]))


class Modules:
    def __init__(self, path):
        path = path.absolute()
        root = next((parent for parent in path.parents if (parent / "pubspec.yaml").is_file()), None)
        self.root = root or next((parent.parent for parent in path.parents if parent.name == "test"),
                                 path.parent)
        self.root = self.root.resolve()
        self.name = None
        if root is not None:
            try:
                manifest = (root / "pubspec.yaml").read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                manifest = ""
            match = re.search(r"^name:[ \t]*['\"]?([a-zA-Z0-9_]+)", manifest, re.MULTILINE)
            if match:
                self.name = match[1]
        self.loaded = {}

    def load(self, path, uri):
        if uri.startswith("package:"):
            name, _, relative = uri.removeprefix("package:").partition("/")
            if name != self.name:
                return None
            candidate = self.root / "lib" / relative
        elif ":" not in uri and not Path(uri).is_absolute():
            candidate = path.parent / uri
        else:
            return None
        if candidate.suffix != ".dart":
            return None
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.root):
            return None
        if candidate not in self.loaded:
            from .ast_grep_dart import File

            try:
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                self.loaded[candidate] = None
            else:
                self.loaded[candidate] = File(Syntax.parse(source, "dart"), candidate, self)
        return self.loaded[candidate]
