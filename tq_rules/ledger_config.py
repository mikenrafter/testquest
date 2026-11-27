from dataclasses import dataclass
import os
from pathlib import Path
from string import Formatter
import tomllib


@dataclass(frozen=True)
class Location:
    path: Path | None
    source: str

    def describe(self):
        return {"path": str(self.path) if self.path is not None else None,
                "source": self.source}


def read_config(path, root):
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error

    for key in config:
        if key != "ledger":
            raise ValueError(f"{path}: unknown key {key}")
    table = config.get("ledger", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: ledger must be a table")

    locations = {}
    for key, value in table.items():
        source = f"{path} [ledger].{key}"
        if key not in {"path", "global"}:
            raise ValueError(f"{source}: unknown key ledger.{key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{source}: ledger.{key} must be a nonempty string")
        locations[key] = resolve_path(value, source, root or Path.cwd(), root, template=True)
    return locations


def resolve_path(value, source, base, root, *, template=False):
    if value == "none":
        return Location(None, source)
    try:
        if not value.strip() or "\0" in value:
            raise ValueError("expected a nonempty path without NUL characters")
        if template:
            fields = {"home": Path.home(), "root": root, "name": root and root.name,
                      "parent": root and root.parent}
            parts = []
            needs_worktree = False
            for literal, field, format_spec, conversion in Formatter().parse(value):
                parts.append(literal)
                if field is None:
                    continue
                if field not in fields or format_spec or conversion:
                    raise ValueError(f"unknown template field {{{field}}}")
                replacement = fields[field]
                needs_worktree |= replacement is None
                parts.append(str(replacement))
            if needs_worktree:
                return Location(None, f"{source} requires a Git worktree")
            value = "".join(parts)
        return Location(base / Path(value).expanduser(), source)
    except (RuntimeError, ValueError) as error:
        raise ValueError(f"{source}: {error}") from error


def resolve(root, ledger, global_ledger):
    """Resolve both ledger paths with provenance, or raise on invalid configuration."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    config_dir = Path.cwd() / config_home if config_home else Path.home() / ".config"
    config = read_config(config_dir / "tq" / "config.toml", root)
    if root is not None:
        config.update(read_config(root / ".tq" / "config.toml", root))

    default = (Location(root / ".tq" / "ledger.jsonl", "default") if root is not None
               else Location(None, "outside a Git worktree"))
    local = resolve_override(ledger, "--ledger", "TQ_LEDGER") or config.get("path", default)
    shared = (resolve_override(global_ledger, "--global-ledger", "TQ_GLOBAL_LEDGER")
              or config.get("global", Location(None, "not configured")))
    return local, shared


def resolve_override(flag, flag_name, variable):
    if flag is not None:
        return resolve_path(flag, flag_name, Path.cwd(), None)
    if variable in os.environ:
        return resolve_path(os.environ[variable], variable, Path.cwd(), None)
    return None
