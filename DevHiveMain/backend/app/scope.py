"""Folder/file scope for questions: validation, SQL filtering and grouping by folder."""

from sqlalchemy import or_
from .models import FileChunk


MAX_SCOPE = 50


def valid_path(path: str) -> bool:
    return 0 < len(path) <= 500 and "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/"))


def covers(path: str, scope: list[str]) -> bool:
    """True when the scope is empty (whole repository) or names the path or one of its parents."""
    return not scope or any(path == entry or path.startswith(entry + "/") for entry in scope)


def normalize_scope(raw: list[str]) -> list[str]:
    """Strip slashes, validate, dedupe and drop entries already covered by a selected parent.

    Raises ValueError for an invalid path or more than MAX_SCOPE entries.
    """
    cleaned = list(dict.fromkeys(entry.strip().strip("/") for entry in raw if entry and entry.strip().strip("/")))
    if len(cleaned) > MAX_SCOPE:
        raise ValueError("Too many scope entries")
    for entry in cleaned:
        if not valid_path(entry):
            raise ValueError("Invalid scope path")
    return [entry for entry in cleaned
            if not any(other != entry and entry.startswith(other + "/") for other in cleaned)]


def _escape_like(value: str) -> str:
    # valid_path rejects backslashes, so \ is a safe escape character. "_" matters: Chapter1_X must
    # not match Chapter1aX.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def scope_clause(scope: list[str]):
    """SQL filter on FileChunk.path for a normalized scope; None means no restriction."""
    if not scope:
        return None
    return or_(*[or_(FileChunk.path == entry, FileChunk.path.like(_escape_like(entry) + "/%", escape="\\"))
                 for entry in scope])


def group_key(path: str, scope: list[str]) -> str:
    """Folder a file is reported under: the first segment below the longest matching scope entry.

    With no scope this is the top-level folder. A file directly inside the scope folder is grouped
    under that folder, and a repository-root file is grouped as "(root)".
    """
    best = max((entry for entry in scope if path == entry or path.startswith(entry + "/")), key=len, default="")
    if best and path == best:
        return best.rsplit("/", 1)[0] if "/" in best else "(root)"
    rest = path[len(best) + 1:] if best else path
    first, _, tail = rest.partition("/")
    if not tail:  # a file directly inside the base folder
        return best or "(root)"
    return f"{best}/{first}" if best else first
