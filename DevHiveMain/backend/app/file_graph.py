"""Resolve local source imports against files actually indexed on a branch."""

import ast
import posixpath
import re


SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".css")
C_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".m", ".mm")
INCLUDE = re.compile(r'^\s*#\s*(?:include|import)\s+"([^"\n]+)"', re.M)


def _resolve_include(source: str, specifier: str, paths: set[str]) -> str | None:
    """Quoted includes search the including file's folder, then the whole tree by path suffix."""
    local = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if local in paths and local != source:
        return local
    suffix = "/" + posixpath.normpath(specifier).lstrip("./")
    matches = [p for p in paths if (p.endswith(suffix) or p == suffix[1:]) and p != source]
    if len(matches) == 1:
        return matches[0]
    # Several candidates: prefer the one sharing the longest leading folder with the includer.
    if matches:
        def shared(path: str) -> int:
            return len(posixpath.commonpath([posixpath.dirname(source) or ".", posixpath.dirname(path) or "."]))
        return max(matches, key=shared)
    return None


def _resolve(source: str, specifier: str, paths: set[str]) -> str | None:
    if source.endswith(".py") and specifier.startswith("."):
        level = len(specifier) - len(specifier.lstrip("."))
        parent = posixpath.dirname(source)
        for _ in range(level - 1):
            parent = posixpath.dirname(parent)
        base = posixpath.normpath(posixpath.join(parent, specifier[level:].replace(".", "/")))
    elif specifier.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    else:
        base = specifier.replace(".", "/") if source.endswith(".py") else specifier
    if base.startswith("../") or base in {"", ".", ".."}:
        return None
    options = [base, *(base + suffix for suffix in SOURCE_SUFFIXES),
               *(f"{base}/index{suffix}" for suffix in SOURCE_SUFFIXES), f"{base}/__init__.py"]
    return next((path for path in options if path in paths and path != source), None)


def dependencies(source: str, content: str, paths: set[str]) -> set[str]:
    specs: set[str] = set()
    if source.lower().endswith(C_SUFFIXES):
        return {target for spec in set(INCLUDE.findall(content))
                if (target := _resolve_include(source, spec, paths))}
    if source.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                specs.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = "." * node.level
                    specs.add(prefix + (node.module or "").replace(".", "/"))
                    specs.update(prefix + ((node.module + "/") if node.module else "") + alias.name
                                 for alias in node.names)
                elif node.module:
                    specs.add(node.module)
    else:
        specs.update(re.findall(r"(?:\bfrom\s*|\bimport\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)[\"']([^\"']+)[\"']", content))
    return {target for specifier in specs if (target := _resolve(source, specifier, paths))}
