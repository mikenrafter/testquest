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

JVM_LANGUAGES = {".kt": "kotlin", ".kts": "kotlin", ".java": "java"}
EXTENDED_LANGUAGES = ECMASCRIPT_LANGUAGES | JVM_LANGUAGES
