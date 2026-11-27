"""Read pinned mutants and apply their swaps inside a scratch tree."""

from dataclasses import dataclass
import io
import json
from pathlib import Path
import re
import subprocess
import tokenize

from . import repo
from .ast_grep_languages import EXTENDED_LANGUAGES


LANGUAGES = {".py": "python", ".rs": "rust"} | EXTENDED_LANGUAGES
COMMENT_KINDS = {
    "python": ("comment",), "rust": ("line_comment",),
    "typescript": ("comment",), "tsx": ("comment",), "javascript": ("comment",),
    "dart": ("comment",), "kotlin": ("line_comment",),
    "java": ("line_comment",),
}
MUTANT_ID = r"M-\d+"
HEADER = re.compile(rf"[ \t]*(?P<comment>\#|//)[ \t]*tq-mutant[ \t]+"
                    rf"(?P<id>{MUTANT_ID})[ \t]+(?P<flip>[\w-]+)[ \t]+(?P<metadata>.+)")
COMMENT = re.compile(r"[ \t]*(?:#|//)[ \t]*tq-mutant[ \t]+")
SWAP_LINE = re.compile(r"[ \t]*(?:#|//)[ \t]*\|")
CONFIG = re.compile(r"""mutants\s*=\s*(?:"(inline|sidecar)"|'(inline|sidecar)'|(inline|sidecar))""")


@dataclass(frozen=True)
class Mutant:
    id: str
    file: Path
    definition: Path
    assumes: str
    flip: str
    tests: tuple[str, ...]
    swap: tuple[str, ...]
    anchor: tuple[str, ...] = ()
    block: tuple[str, ...] = ()
    start: int = 0
    replaces: int = 0


def safe_path(directory, path):
    if (path.is_absolute() or ".." in path.parts or ".git" in path.parts
            or not path.parts):
        raise ValueError(f"Expected a repository-relative source path: {path}")
    result = directory / path
    if result.resolve() != result or result.is_symlink():
        raise ValueError(f"Source path passes through a symlink: {path}")
    return result


def encoding(path, data):
    if path.suffix == ".py":
        return tokenize.detect_encoding(io.BytesIO(data).readline)[0]
    return "utf-8"


def read_source(path):
    data = path.read_bytes()
    codec = encoding(path, data)
    return data.decode(codec), codec


def syntax_tree(source, path):
    try:
        from .ast_grep import Syntax
    except ModuleNotFoundError as error:
        if error.name != "ast_grep_py":
            raise
        raise ValueError("Pinned mutant and slice checks require ast-grep-py") from error
    language = LANGUAGES[path.suffix]
    if language == "dart":
        from .ast_grep_dart_syntax import register_grammar

        register_grammar()
    return Syntax.parse(source, language)


def parse_error(source, path):
    tree = syntax_tree(source, path)
    from .ast_grep import syntax_errors

    lines = syntax_errors(tree.root, missing=True)
    if lines:
        return f"{path}:{lines[0]}: syntax error"
    if path.suffix == ".py":
        try:
            compile(source, str(path), "exec", dont_inherit=True)
        except SyntaxError as error:
            return f"{path}:{error.lineno}: {error.msg}"
    return ""


def parse_inline(path, source):
    # Only parse files containing a candidate. Match comment nodes to exclude string literals.
    if "tq-mutant" not in source:
        return
    lines = source.splitlines()
    candidates = {index for index, line in enumerate(lines) if COMMENT.match(line)}
    if not candidates:
        return
    tree = syntax_tree(source, path)
    from .ast_grep import query

    comments = {node.range().start.line for node in query(
        tree.root, *COMMENT_KINDS[tree.language])
        if node.text().startswith(("#", "//"))}
    for index in sorted(candidates & comments):
        match = HEADER.fullmatch(lines[index])
        if match is None:
            raise ValueError(f"{path}:{index + 1}: invalid tq-mutant header")
        name, flip, metadata = match.group("id", "flip", "metadata")
        fields = {}
        # A final killed-by value can contain a runner's display name, including spaces.
        parts = re.split(r"\s+(?=(?:assumes|killed-by|replaces)=)", metadata)
        for part in parts:
            key, _, value = part.partition("=")
            if key in fields or key not in {"assumes", "killed-by", "replaces"} or not value:
                raise ValueError(f"{path}:{index + 1}: invalid mutant metadata: {part}")
            fields[key] = value
        assumes = fields.get("assumes", "")
        tests = tuple(name.strip() for name in fields.get("killed-by", "").split(","))
        if not re.fullmatch(r"A-\d+", assumes) or not all(tests):
            raise ValueError(f"{path}:{index + 1}: mutant needs assumes=A-NN and killed-by=TEST")
        prefix = re.compile(r"[ \t]*" + re.escape(match["comment"]) + r"[ \t]*\|(?: |$)")
        end = index + 1
        swap = []
        while end in comments and (line := prefix.match(lines[end])):
            swap.append(lines[end][line.end():])
            end += 1
        count = fields.get("replaces", str(len(swap)))
        if not re.fullmatch(r"[1-9][0-9]*", count):
            raise ValueError(f"{path}:{index + 1}: replaces must be a positive line count")
        if not swap:
            raise ValueError(f"{path}:{index + 1}: mutant needs a swap line")
        yield Mutant(name, path, path, assumes, flip, tests, tuple(swap),
                     block=tuple(lines[index:end]), start=end, replaces=int(count))


def parse_sidecar(directory, path):
    def string(item, field, *, empty=False):
        value = item.get(field)
        if not isinstance(value, str) or ("\0" in value) or (not empty and not value.strip()):
            raise ValueError(f"{path}: mutant {field} must be a valid string")
        return value

    item = json.loads(safe_path(directory, path).read_text(encoding="utf-8"))
    if not isinstance(item, dict):
        raise ValueError(f"{path}: expected a mutant object")
    name, assumes = string(item, "id"), string(item, "assumes")
    source = Path(string(item, "file"))
    safe_path(directory, source)
    if source.suffix not in LANGUAGES:
        raise ValueError(f"{path}: unsupported mutant language: {source}")
    tests = item.get("killed_by")
    if (not re.fullmatch(MUTANT_ID, name) or not re.fullmatch(r"A-\d+", assumes)
            or not isinstance(tests, list) or not tests
            or not all(isinstance(test, str) and test.strip() and "\0" not in test for test in tests)):
        raise ValueError(f"{path}: expected M-NN, A-NN and a nonempty killed_by list")
    return Mutant(name, source, path, assumes, string(item, "flip"), tuple(tests),
                  tuple(string(item, "swap", empty=True).splitlines()),
                  anchor=tuple(string(item, "anchor").splitlines()))


def read_inline(directory, path):
    source = directory / path
    if source.is_symlink() or source.parent.resolve() != source.parent or not source.is_file():
        return
    data = source.read_bytes()
    if b"tq-mutant" in data:
        yield from parse_inline(path, data.decode(encoding(path, data)))


def discover(directory):
    config = safe_path(directory, Path(".tq/config"))
    mode = None
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            setting = line.partition("#")[0].strip()
            if not setting:
                continue
            match = CONFIG.fullmatch(setting)
            if match is None:
                raise ValueError(f"{config}: expected mutants = inline or mutants = sidecar")
            if mode is not None:
                raise ValueError(f"{config}: duplicate mutants setting")
            mode = next(group for group in match.groups() if group is not None)
    mode = mode or "inline"
    found = {}
    for path in sorted(set(repo.tracked(directory))):
        if mode == "inline" and path.suffix in LANGUAGES:
            candidates = read_inline(directory, path)
        elif mode == "sidecar" and path.parent == Path(".tq/mutants") and path.suffix == ".json":
            candidates = (parse_sidecar(directory, path),)
        else:
            continue
        for mutant in candidates:
            if mutant.id in found:
                raise ValueError(f"Duplicate mutant {mutant.id}: {found[mutant.id].definition} and {path}")
            found[mutant.id] = mutant
    return found


def indent(line):
    return line[:len(line) - len(line.lstrip(" \t"))]


def relative_lines(lines, prefix):
    return tuple(line.removeprefix(prefix) if line.strip() else "" for line in lines)


def swap_span(mutant, lines):
    if mutant.block:
        start, stop = mutant.start, mutant.start + mutant.replaces
        if stop > len(lines) or not lines[start].strip() or lines[start].lstrip().startswith(("#", "//")):
            return None
        return start, stop, indent(lines[start])
    anchor = relative_lines(mutant.anchor, indent(mutant.anchor[0]))
    matches = []
    for start in range(len(lines) - len(anchor) + 1):
        stop = start + len(anchor)
        prefix = indent(lines[start])
        if relative_lines(lines[start:stop], prefix) == anchor:
            matches.append((start, stop, prefix))
    return matches[0] if len(matches) == 1 else None


def apply(directory, mutant):
    path = safe_path(directory, mutant.file)
    if not path.is_file():
        return "anchor-missing", f"{mutant.file} is missing"
    source, codec = read_source(path)
    lines = source.splitlines(keepends=True)
    span = swap_span(mutant, [line.rstrip("\r\n") for line in lines])
    if span is None:
        return "anchor-missing", f"{mutant.file} has no unique replacement span"
    start, stop, prefix = span
    endings = lines[start][len(lines[start].rstrip("\r\n")):]
    newline = endings or "\n"
    target_ending = lines[stop - 1][len(lines[stop - 1].rstrip("\r\n")):]
    replacement = [prefix + line if line else "" for line in mutant.swap]
    swapped = newline.join(replacement) + (target_ending if replacement else "")
    updated = "".join(lines[:start]) + swapped + "".join(lines[stop:])
    if error := parse_error(updated, mutant.file):
        return "unparseable", error
    path.write_bytes(updated.encode(codec))
    return None, ""


def changes(repository, base, commit):
    return {new: (old, blob) for old, new, blob, _ in
            repo.differences(repository, base, commit, renames=True)}


def block_end(lines, block):
    for index, line in enumerate(lines):
        if line != block[0]:
            continue
        end = index + len(block)
        if tuple(lines[index:end]) == block and (end == len(lines) or not SWAP_LINE.match(lines[end])):
            return end
    return None


def target_lines(lines, start, count):
    return tuple(lines[start:start + count])


def changed_pins(repository, directory, base, commit, mutants):
    """Select pins with a changed definition or replacement span since base."""
    changed = changes(repository, base, commit)
    previous = history_blobs(repository, changed, {pin.file for pin in mutants})
    for pin in mutants:
        if pin.definition != pin.file and pin.definition in changed:
            yield pin
        elif pin.file in changed:
            old = previous.get(pin.file, ())
            current = read_source(directory / pin.file)[0].splitlines() if (directory / pin.file).is_file() else []
            if pin.block:
                start = block_end(old, pin.block)
                if (start is None or target_lines(old, start, pin.replaces)
                        != target_lines(current, pin.start, pin.replaces)):
                    yield pin
            elif (swap_span(pin, old) is None) != (swap_span(pin, current) is None):
                yield pin


def history_blobs(repository, changed, paths):
    from .history import read_blobs

    files = {path: changed[path][1] for path in paths & changed.keys() if changed[path][1]}
    blobs = read_blobs(repository, files.values())
    return {path: blobs[blob].decode(encoding(path, blobs[blob])).splitlines()
            for path, blob in files.items()}


def previous_versions(repository, commit, path):
    """Yield file versions along first-parent history, following detected renames."""
    while True:
        commits = repo.git(repository, "--literal-pathspecs", "log", "--first-parent",
                           "--full-history", "--format=%H", commit, "--", str(path)).decode().splitlines()
        for previous in commits[1:]:
            try:
                blob = repo.git(repository, "show", f"{previous}:{path.as_posix()}")
            except subprocess.CalledProcessError:
                return
            yield blob.decode(encoding(path, blob)).splitlines()
        if not commits:
            return
        try:
            commit = repo.resolve(repository, commits[-1] + "^1")
        except subprocess.CalledProcessError:
            return
        old, blob = changes(repository, commit, commits[-1]).get(path, (path, None))
        if old == path or blob is None:
            return
        content = repo.git(repository, "cat-file", "blob", blob)
        yield content.decode(encoding(old, content)).splitlines()
        path = old


def stale_mutants(repository, commit, mutants, directory):
    """Compare adjacent first-parent versions while a pinned block stays identical."""
    by_file = {}
    for mutant in mutants:
        if mutant.block:
            by_file.setdefault(mutant.file, []).append(mutant)
    stale = set()
    for path, pins in by_file.items():
        source = read_source(directory / path)[0].splitlines()
        pending = {pin: target_lines(source, pin.start, pin.replaces) for pin in pins}
        for lines in previous_versions(repository, commit, path):
            for pin, target in tuple(pending.items()):
                start = block_end(lines, pin.block)
                if start is None:
                    del pending[pin]
                elif target_lines(lines, start, pin.replaces) != target:
                    stale.add(pin.id)
                    del pending[pin]
            if not pending:
                break
    return stale
