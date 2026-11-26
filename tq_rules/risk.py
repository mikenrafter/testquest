import ast
from collections import Counter, defaultdict
import html
import json
import os
from pathlib import Path
import re

from . import repo, rust
from .python import test_functions


def histories(repository):
    counts, fixes = Counter(), Counter()
    log = repo.git(repository, "log", "--format=%x00%s", "--name-only", "--no-renames", "-z", "HEAD")
    for record in log.split(b"\0\0"):
        subject, _, paths = record.lstrip(b"\0").partition(b"\0")
        changed = set(paths.removeprefix(b"\n").rstrip(b"\0").split(b"\0")) - {b""}
        decoded = [Path(os.fsdecode(path)) for path in changed]
        counts.update(decoded)
        if b"fix" in subject.lower():
            fixes.update(decoded)
    return counts, fixes


def python_context(path):
    parts = path.with_suffix("").parts
    module = parts[:-1] if parts[-1] == "__init__" else parts
    return module, parts[:-1]


def python_imports(nodes, package):
    for node in nodes:
        if isinstance(node, ast.Import):
            yield from (tuple(alias.name.split(".")) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parent = package[:len(package) - node.level + 1] if node.level else ()
            prefix = parent + tuple(node.module.split(".") if node.module else ())
            if prefix:
                yield prefix
            yield from ((*prefix, alias.name) for alias in node.names)


def rust_context(path):
    parts = path.with_suffix("").parts
    source = max((i + 1 for i, part in enumerate(parts[:-1]) if part == "src"), default=0)
    crate = parts[:source]
    module = parts[source:]
    if module[-1] in {"lib", "main", "mod"}:
        module = module[:-1]
    return crate, module


def rust_use(tokens, prefix=()):
    path = prefix
    for token in tokens:
        if token.text == "{":
            for alternative in rust.split(token.children):
                yield from rust_use(alternative, path)
            return
        if token.text == "as":
            break
        if token.kind == "word" and token.text != "self":
            path = (*path, token.text)
    if path:
        yield path


def rust_imports(tokens, module):
    for index, token in enumerate(tokens):
        if token.text == "use":
            clause = next(iter(rust.split(tokens[index + 1:], ";")), ())
            for path in rust_use(clause):
                if path[0] == "crate":
                    yield path[1:]
        if token.text == "mod" and index + 2 < len(tokens):
            location, target = tokens[index + 1:index + 3]
            if target.text == ";":
                yield (*module, location.text)
            if target.text == "{":
                yield from rust_imports(target.children, (*module, location.text))


def is_test_file(path):
    return (any(part in {"tests", "test", "fixtures"} for part in path.parts[:-1])
            or path.stem.startswith("test_") or path.stem.endswith(("_test", "_tests")))


def inventory(repository):
    sources, references = {}, []
    for path in sorted(set(repo.tracked(repository))):
        full = repository / path
        if full.is_symlink() or not full.is_file() or not full.resolve().is_relative_to(repository):
            continue
        if path.suffix not in {".py", ".rs", ""}:
            continue
        if not path.suffix:
            with full.open("rb") as stream:
                if not re.match(rb"#![^\n]*\bpython[23]?(?:\s|$)", stream.readline(512)):
                    continue
        source = full.read_text(encoding="utf-8")
        in_tests = is_test_file(path)
        if path.suffix == ".rs":
            tokens, _ = rust.tokenize(source)
            grouped = rust.group(tokens)
            crate, module = rust_context(path)
            importer = ("rust", crate, module[:1])
            imports = set(rust_imports(grouped, module))
            if in_tests or any(rust.test_functions(grouped)):
                references.append((path, {tok.text for tok in tokens if tok.kind == "word"}))
            if not in_tests:
                sources[path] = ("rust", crate, module, importer, imports)
        else:
            tree = ast.parse(source, filename=str(path))
            module, package = python_context(path)
            importer = ("python", (), module[:1])
            nodes = list(ast.walk(tree))
            imports = set(python_imports(nodes, package))
            if in_tests or any(test_functions(tree.body)):
                docstrings = {id(node.body[0].value) for node in nodes
                              if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                              and node.body and isinstance(node.body[0], ast.Expr)
                              and isinstance(node.body[0].value, ast.Constant)}
                names = {node.id for node in nodes if isinstance(node, ast.Name)}
                names.update(node.attr for node in nodes if isinstance(node, ast.Attribute))
                names.update(part for imported in imports for part in imported)
                for node in nodes:
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docstrings
                            and re.fullmatch(r"[\w./-]+", node.value)):
                        names.add(Path(node.value).stem)
                references.append((path, names))
            if not in_tests:
                sources[path] = ("python", (), module, importer, imports)
    return sources, references


def allocate(rows, budget):
    # Reserve one slot for each of the top three areas when the budget permits.
    areas = rows[:min(budget, 5)]
    if not areas:
        return
    reserved = min(3, len(areas))
    for row in areas[:reserved]:
        row["tests"] = 1
    remaining = budget - reserved
    total = sum(max(1, row["score"]) for row in areas)
    fractions = []
    for index, row in enumerate(areas):
        whole, remainder = divmod(remaining * max(1, row["score"]), total)
        row["tests"] += whole
        fractions.append((-remainder, index))
    unassigned = budget - sum(row["tests"] for row in areas)
    for _, index in sorted(fractions)[:unassigned]:
        areas[index]["tests"] += 1


def map_risk(repository, budget):
    repository = repo.root(repository)
    churn, fixes = histories(repository)
    sources, references = inventory(repository)
    callers = defaultdict(set)
    for language, crate, _, importer, imports in sources.values():
        for imported in imports:
            for length in range(1, len(imported) + 1):
                callers[language, crate, imported[:length]].add(importer)
    rows = []
    for path, (language, crate, module, importer, _) in sources.items():
        importers = len(callers[language, crate, module] - {importer})
        names = {path.stem} if not module else {module[-1]}
        mentions = sum(bool(names & names_used) for test_path, names_used in references if test_path != path)
        score = 3 * fixes[path] + churn[path] + 2 * importers
        rows.append(dict(path=str(path), fixes=fixes[path], churn=churn[path], importers=importers,
                         test_refs=mentions, score=score, gap=score >= 8 and not mentions, tests=0))
    rows.sort(key=lambda row: (-row["score"], row["path"]))
    allocate(rows, budget)
    return dict(budget=budget, files=rows)


def table_rows(rows):
    for row in rows:
        yield (
            "<tr><th scope='row'>" + html.escape(row["path"]) + "</th>"
            + "".join(f"<td>{row[key]}</td>" for key in ("score", "fixes", "churn", "importers", "test_refs", "tests"))
            + ("<td>Gap</td>" if row["gap"] else "<td></td>") + "</tr>"
        )


def write_html(snapshot, path):
    rows = snapshot["files"]
    gaps = sum(row["gap"] for row in rows)
    snapshot_json = json.dumps(snapshot).replace("<", r"\u003c")
    page = f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test risk</title>
<style>
* {{box-sizing: border-box}}
body {{margin: 0; color: var(--foreground,#18202b); background: var(--background,#f5f7fa);
  font: 1rem/1.5 system-ui, sans-serif}}
main {{max-width: 76rem; margin: auto; padding: 1.5rem 1.25rem 6rem}}
h1 {{margin: 0 0 1.25rem}}
.cards {{display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: 1.5rem}}
article {{flex: 1 1 9rem; background: var(--card,#fff); border: 1px solid var(--border,#c5ccd6);
  border-radius: .5rem; padding: .75rem 1rem}}
article strong {{display: block; font-size: 1.5rem}}
.scroll {{overflow: auto}}
table {{width: 100%; text-align: left; border-collapse: collapse; background: var(--card,#fff)}}
td, th {{border-bottom: 1px solid var(--border,#c5ccd6); padding: .4rem .65rem; vertical-align: top}}
tbody th {{font-weight: normal; overflow-wrap: anywhere; min-width: 14rem}}
tbody td {{text-align: right; font-variant-numeric: tabular-nums}}
</style>
<main>
<h1>Test risk</h1>
<section class="cards" aria-label="Overview">
<article><strong>{len(rows)}</strong>Source files</article>
<article><strong>{gaps}</strong>High-risk files with zero test references</article>
<article><strong>{sum(row['tests'] for row in rows)} / {snapshot['budget']}</strong>Test slots allocated</article>
</section>
<p>Score counts each fix three times, each change once, and each importing module twice.
A score of eight or higher with zero test references marks a gap.</p>
<div class="scroll"><table>
<thead><tr><th scope="col">File</th><th scope="col">Score</th><th scope="col">Fixes</th>
<th scope="col">Churn</th><th scope="col">Importers</th><th scope="col">Test references</th>
<th scope="col">Test slots</th><th scope="col">Coverage</th></tr></thead>
<tbody>{"".join(table_rows(rows))}</tbody>
</table></div>
</main>
<script id="tq-risk" type="application/json">{snapshot_json}</script>
</html>
"""
    Path(path).write_text(page, encoding="utf-8")
