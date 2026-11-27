"""Source suffixes recognized by the optional ast-grep engine."""

ECMASCRIPT_LANGUAGES = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}
ECMASCRIPT_OUTPUT = {"node_modules", "dist", "build", "coverage"}

CALLBACK_SUFFIXES = frozenset(ECMASCRIPT_LANGUAGES) | {".dart"}
