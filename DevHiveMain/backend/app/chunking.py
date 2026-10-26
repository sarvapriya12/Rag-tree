"""Split source files into line-addressed chunks that follow definition boundaries."""

import re
from dataclasses import dataclass


TARGET_LINES = 25
MAX_CHARS = 900
OVERLAP = 3
MIN_DEFINITION_LINES = 6  # a new definition starts a chunk once the current one has this many lines
# Hard limits when no definition or blank line offers a better break. Kept near the target so the
# embedding (about 256 tokens) still sees most of the chunk.
MAX_LINES = TARGET_LINES * 3 // 2
HARD_CHARS = MAX_CHARS * 3 // 2

# A definition start at column 0 (or an access-free indented method head for classes
# in languages that nest them). The captured group is the symbol name.
_C_FUNC = re.compile(r"^(?:[A-Za-z_][\w:<>,\s\*&~]*?[\s\*&])?((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\([^;{]*$")
_C_TYPE = re.compile(r"^\s*(?:template\s*<.*>\s*)?(?:class|struct|namespace|enum|union)\s+([A-Za-z_]\w*)")
_OBJC = re.compile(r"^\s*(?:@implementation|@interface|@protocol)\s+([A-Za-z_]\w*)|^[-+]\s*\([^)]*\)\s*([A-Za-z_]\w*)")
_PY = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")
_JS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|class|interface|type|const|enum)\s+([A-Za-z_$][\w$]*)")
_JAVA = re.compile(r"^\s{0,4}(?:public|private|protected|static|final|abstract|synchronized|\s)*"
                   r"(?:class|interface|enum)\s+([A-Za-z_]\w*)|^\s{1,4}(?:public|private|protected|static|final|synchronized|\s)+"
                   r"[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\([^;]*$")
_NOT_DEF = {"if", "for", "while", "switch", "return", "else", "catch", "sizeof", "do", "case", "defined"}

C_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
OBJC_SUFFIXES = (".m", ".mm")


@dataclass(frozen=True)
class Chunk:
    start_line: int
    end_line: int
    symbol: str | None
    text: str


def _symbol(path: str, line: str) -> str | None:
    lower = path.lower()
    if not line.strip() or line.lstrip().startswith(("//", "/*", "*", "#", "--")) and not lower.endswith(".py"):
        return None
    if lower.endswith(".py"):
        match = _PY.match(line)
        return match.group(1) if match else None
    if lower.endswith(OBJC_SUFFIXES):
        match = _OBJC.match(line)
        if match:
            return match.group(1) or match.group(2)
    if lower.endswith(C_SUFFIXES + OBJC_SUFFIXES):
        match = _C_TYPE.match(line)
        if match:
            return match.group(1)
        # Function heads start in column 0 and are not statements.
        if line[:1].isspace() or line.rstrip().endswith(";"):
            return None
        match = _C_FUNC.match(line)
        if match and match.group(1).split("::")[-1] not in _NOT_DEF:
            return match.group(1)
        return None
    if lower.endswith((".java", ".kt", ".cs")):
        match = _JAVA.match(line)
        if match:
            return match.group(1) or match.group(2)
        return None
    if lower.endswith((".ts", ".tsx", ".js", ".jsx")):
        match = _JS.match(line)
        return match.group(1) if match else None
    return None


def chunk_source(path: str, text: str) -> list[Chunk]:
    """Return chunks whose text is exactly lines start_line..end_line of the file.

    Chunks prefer to start at a definition, stay near TARGET_LINES lines and
    MAX_CHARS characters, and repeat OVERLAP lines of context between neighbours.
    """
    lines = text.splitlines()
    if not lines:
        return []
    symbols = [_symbol(path, line) for line in lines]
    chunks: list[Chunk] = []
    start = 0
    total = len(lines)
    while start < total:
        end = start
        size = 0
        # Grow until the target is reached, then stop at the next definition or blank line.
        while end < total:
            size += len(lines[end]) + 1
            end += 1
            if end >= total:
                break
            long_enough = end - start >= TARGET_LINES or size >= MAX_CHARS
            if end - start >= MIN_DEFINITION_LINES and symbols[end] is not None and (size >= MAX_CHARS // 3 or long_enough):
                break
            if long_enough and not lines[end].strip():
                break
            if end - start >= MAX_LINES or size >= HARD_CHARS:
                break
        body = "\n".join(lines[start:end])
        # Name the chunk by its first definition, else the nearest one above it.
        name = next((s for s in symbols[start:end] if s), None)
        if name is None:
            name = next((s for s in reversed(symbols[:start]) if s), None)
        chunks.append(Chunk(start + 1, end, name, body))
        if end >= total:
            break
        start = max(end - OVERLAP, start + 1)
    return chunks
