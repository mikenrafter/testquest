"""Resolve exported ECMAScript helpers from source inside the scanned repository."""

from collections import defaultdict
from functools import lru_cache
import json
import os

from .ast_grep import Syntax, children
from .ast_grep_languages import ECMASCRIPT_LANGUAGES
from .ast_grep_ts import ASSERT_MODULES, FRAMEWORKS, File, chain, expression, name, text_literal
from .ast_grep_ts_recorders import object_member


def imported_helper(file, fn):
    base, members = chain(fn)
    if (file.scan_root is None or name(base) not in file.source_imports
            or file.binding(base) is not None or file.parameter_scope(base) is not None):
        return None
    root = file.scan_root.resolve()
    importer = file.path.resolve()
    for parent in importer.parents:
        if not parent.is_relative_to(root):
            break
        if (parent / ".git").exists():
            root = parent
            break
    source, names = file.source_imports[name(base)]
    return resolve_export(root, importer, source, (*names, *members))


@lru_cache(maxsize=1024)
def resolve_export(root, importer, source, names, active=frozenset()):
    if not names:
        return None
    if source in ASSERT_MODULES | FRAMEWORKS and names[0] == "assert":
        return None, None
    target = module_path(root, importer, source)
    if target is None or target in active:
        return None
    module = read_module(root, target)
    if module is None:
        return None
    active = active | {target}
    key, *members = names
    for statement in children(module.syntax.root):
        if statement.kind() != "export_statement" or any(part.kind() == "type" for part in statement.children()):
            continue
        is_default = any(child.kind() == "default" for child in statement.children())
        declaration = statement.field("declaration")
        if key == "default" and is_default and declaration is None:
            return module, select_member(statement.field("value"), members)
        if declaration is not None:
            if declaration.kind() in {"lexical_declaration", "variable_declaration"}:
                declaration = next((entry for entry in children(declaration)
                                    if name(entry.field("name")) == key), None)
            if declaration is not None:
                declared = declaration.field("name")
                if (key == "default" if is_default else declared is not None and declared.text() == key):
                    if declaration.kind() == "variable_declarator":
                        declaration = declaration.field("value")
                    return module, select_member(declaration, members)
        namespace = next((entry for entry in children(statement) if entry.kind() == "namespace_export"), None)
        reexport = text_literal(statement.field("source"))
        if namespace is not None and reexport and children(namespace)[0].text() == key:
            return resolve_export(root, target, reexport, tuple(members), active)
        clause = next((entry for entry in children(statement) if entry.kind() == "export_clause"), None)
        if clause is not None:
            for entry in children(clause):
                original = entry.field("name")
                if (name(entry.field("alias")) or name(original)) == key:
                    if reexport:
                        return resolve_export(root, target, reexport, (name(original), *members), active)
                    binding = module.binding(original)
                    if binding is not None:
                        value = binding.field("value") if binding.kind() == "variable_declarator" else binding
                        return module, select_member(value, members)
        elif reexport and namespace is None and any(entry.kind() == "*" for entry in statement.children()):
            result = resolve_export(root, target, reexport, names, active)
            if result is not None:
                return result
    return None


def select_member(node, members):
    node = expression(node)
    for member in members:
        if node is None:
            return None
        if node.kind() == "class_declaration":
            node = next((entry.field("value") or entry for entry in children(node.field("body"))
                         if name(entry.field("name")) == member and any(part.kind() == "static"
                                                                        for part in entry.children())), None)
        elif node.kind() == "object":
            node = object_member(node, member)
        else:
            return None
        node = expression(node)
    return node


def module_path(root, importer, source):
    if source.startswith("."):
        return source_path(root, importer.parent / source)
    parts = source.split("/")
    package_parts = 2 if source.startswith("@") else 1
    package = "/".join(parts[:package_parts])
    exported = "./" + "/".join(parts[package_parts:]) if len(parts) > package_parts else "."
    candidates = packages(root).get(package, ())
    if not candidates:
        return None
    ranked = sorted((len(os.path.commonpath((folder, importer.parent)).split(os.sep)), folder, data)
                    for folder, data in candidates)
    best, folder, data = ranked[-1]
    if len(ranked) > 1 and ranked[-2][0] == best:
        return None
    exports = data.get("exports")
    target = export_path(exports, exported) if exports is not None else (
        data.get("module", data.get("main", "index")) if exported == "." else exported)
    return source_path(root, folder / target) if isinstance(target, str) else None


def export_path(exports, key):
    if isinstance(exports, str):
        return exports if key == "." else None
    if not isinstance(exports, dict):
        return None
    if key in exports:
        value = exports[key]
    else:
        for pattern in sorted(exports, key=len, reverse=True):
            if pattern.count("*") != 1:
                continue
            prefix, suffix = pattern.split("*")
            if key.startswith(prefix) and key.endswith(suffix) and len(key) >= len(prefix) + len(suffix):
                value = exports[pattern]
                part = key[len(prefix):len(key) - len(suffix)]
                return value.replace("*", part) if isinstance(value, str) else None
        value = exports if key == "." and not any(item.startswith(".") for item in exports) else None
    if isinstance(value, dict):
        value = value.get("import", value.get("default"))
    return value


@lru_cache(maxsize=16)
def packages(root):
    entries = defaultdict(list)
    for folder, directories, files in os.walk(root):
        directories[:] = [item for item in directories if item not in {
            ".git", "node_modules", "dist", "build", "coverage", ".venv", "target",
            ".cache", ".next", "__pycache__",
        }]
        if "package.json" not in files:
            continue
        path = root / folder / "package.json"
        try:
            if not path.resolve().is_relative_to(root):
                continue
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            entries[manifest["name"]].append((path.parent, manifest))
    return entries


def source_path(root, target):
    candidates = [target]
    if target.suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        candidates.extend(target.with_suffix(suffix) for suffix in (".ts", ".tsx", ".mts", ".cts"))
    if not target.suffix:
        candidates.extend(target.with_suffix(suffix) for suffix in ECMASCRIPT_LANGUAGES)
        candidates.extend(target / ("index" + suffix) for suffix in ECMASCRIPT_LANGUAGES)
    for candidate in candidates:
        if candidate.suffix in ECMASCRIPT_LANGUAGES:
            try:
                resolved = candidate.resolve()
                if resolved.is_relative_to(root) and "node_modules" not in resolved.relative_to(root).parts and resolved.is_file():
                    return resolved
            except (OSError, RuntimeError):
                continue
    return None


@lru_cache(maxsize=128)
def read_module(root, path):
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return File(Syntax.parse(source, ECMASCRIPT_LANGUAGES[path.suffix]), path, root)
