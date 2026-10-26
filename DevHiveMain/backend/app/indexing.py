"""Branch-head file snapshots for evidence-backed repository questions."""

import json
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from .chunking import chunk_source
from .config import Settings
from .file_graph import dependencies
from .models import BranchGraphState, BranchSnapshot, FileChunk, FileDependency, now
from .rag import embed_many


TEXT_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".txt", ".json",
                 ".yaml", ".yml", ".toml", ".sql", ".css", ".html", ".sh", ".go",
                 ".rs", ".java", ".kt", ".cs", ".rb", ".php",
                 ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
                 ".m", ".mm", ".swift", ".cmake", ".mk", ".gradle")
TEXT_NAMES = ("cmakelists.txt", "makefile", "dockerfile")
IGNORED_PARTS = {"node_modules", ".git", ".next", "dist", "build", "vendor", "__pycache__",
                 "pods", "third_party", "3rdparty", "thirdparty", "external"}
IGNORED_SUFFIXES = (".framework", ".xcodeproj", ".xcworkspace", ".xcassets", ".bundle", ".lproj")
MAX_FILES = 60
MAX_FILE_CHARS = 200_000
MAX_CHUNKS_PER_FILE = 400


def _safe_path(path: str) -> bool:
    return not any(part in path.lower() for part in (".env", "secret", "credential", "private_key"))


def _vendored(path: str) -> bool:
    for part in path.split("/"):
        lowered = part.lower()
        if lowered in IGNORED_PARTS or lowered.endswith(IGNORED_SUFFIXES):
            return True
    return False


def _is_text_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(TEXT_SUFFIXES) or lowered.rsplit("/", 1)[-1] in TEXT_NAMES


def _selection_tier(path: str) -> int:
    parts = path.lower().split("/")
    if any(part in {"tests", "test", "fixtures", "__tests__"} for part in parts) or parts[-1].startswith("test_"):
        return 2
    if parts[0] in {"docs", ".github", "data"} or path.lower().endswith(
            (".md", ".txt", ".json", ".yaml", ".yml", ".toml")):
        return 1
    return 0


def _quotas(max_files: int) -> tuple[int, int, int]:
    if max_files == MAX_FILES:
        return 45, 10, 5
    if max_files == 30:
        return 22, 5, 3
    return int(max_files * 0.75), int(max_files * 0.17), int(max_files * 0.08)


def _select_files(eligible: list[dict], max_files: int = MAX_FILES) -> list[dict]:
    """Reserve coverage for implementation, documents and tests before filling the cap."""
    buckets = [[item for item in eligible if _selection_tier(item["path"]) == tier]
               for tier in range(3)]
    for bucket in buckets:
        bucket.sort(key=lambda item: item["path"])
    quotas = _quotas(max_files)
    selected = buckets[0][:quotas[0]] + buckets[1][:quotas[1]] + buckets[2][:quotas[2]]
    picked = {item["path"] for item in selected}
    for bucket in buckets:
        for item in bucket:
            if len(selected) >= max_files:
                break
            if item["path"] not in picked:
                selected.append(item)
                picked.add(item["path"])
    return selected


def _read_files(github, repo, head_sha: str, selected: list[dict]) -> dict[str, str]:
    """Prefer one tarball download; fall back to a /contents request per file."""
    wanted = {item["path"] for item in selected}
    archive = getattr(github, "archive", None)
    if archive is not None:
        try:
            files = archive(repo.github_owner, repo.github_name, head_sha)
            return {path: text for path, text in files.items() if path in wanted}
        except HTTPException:
            pass
    contents: dict[str, str] = {}
    for item in selected:
        try:
            contents[item["path"]] = github.file(repo.github_owner, repo.github_name,
                                                 head_sha, item["path"])["content"]
        except (KeyError, ValueError):
            continue
        except HTTPException as exc:
            if exc.status_code in {413, 422}:
                continue
            raise
    return contents


def index_branch_files(db: Session, settings: Settings, github, repo, branch: str,
                       head_sha: str, tree_sha: str, force: bool = False) -> dict:
    snapshot = db.get(BranchSnapshot, (repo.id, branch))
    graph_state = db.get(BranchGraphState, (repo.id, branch))
    if not force and snapshot and snapshot.head_sha == head_sha and graph_state and graph_state.head_sha == head_sha:
        return {"indexed_files": snapshot.indexed_files, "file_chunks": snapshot.file_chunks,
                "snapshot_sha": head_sha, "partial": snapshot.partial, "cached": True}
    tree = github.tree(repo.github_owner, repo.github_name, tree_sha)
    eligible = [item for item in tree.get("tree", [])
                if item.get("type") == "blob" and (item.get("size") or 0) <= MAX_FILE_CHARS
                and _is_text_path(item.get("path", ""))
                and not _vendored(item.get("path", ""))
                and _safe_path(item.get("path", ""))]
    # A tarball costs one request, so only the per-file fallback needs the small anonymous cap.
    has_archive = getattr(github, "archive", None) is not None
    max_files = settings.max_index_files if (has_archive or getattr(github, "token", None)) else 30
    selected = _select_files(eligible, max_files)
    contents = _read_files(github, repo, head_sha, selected)
    pending: list[tuple[str, int, int, int, str | None, str]] = []
    source_text: dict[str, str] = {}
    indexed = 0
    for item in selected:
        content = contents.get(item["path"])
        # Never embed binary-like content.
        if content is None or "\x00" in content:
            continue
        indexed += 1
        source_text[item["path"]] = content
        for segment, chunk in enumerate(chunk_source(item["path"], content)[:MAX_CHUNKS_PER_FILE]):
            label = f" :: {chunk.symbol}" if chunk.symbol else ""
            # One header line (path and symbol) so the embedding keeps room for code. No SHA, line
            # numbers or branch: unchanged code keeps its body, so its embedding is reused.
            body = f"{item['path']}{label}\n{chunk.text}"
            pending.append((item["path"], segment, chunk.start_line, chunk.end_line, chunk.symbol, body))
    reusable: dict[str, str] = {}
    if not force:
        reusable = dict(db.execute(select(FileChunk.body, FileChunk.embedding_json).where(
            FileChunk.repository_id == repo.id, FileChunk.branch == branch)).all())
    fresh_rows = [row for row in pending if row[-1] not in reusable]
    fresh_vectors = iter(embed_many([row[-1] for row in fresh_rows], settings))
    vectors = [json.loads(reusable[row[-1]]) if row[-1] in reusable else next(fresh_vectors)
               for row in pending]
    chunks = [FileChunk(repository_id=repo.id, branch=branch, head_sha=head_sha, path=path,
                        segment=segment, start_line=start, end_line=end,
                        symbol=symbol[:200] if symbol else None,
                        body=body, embedding_json=json.dumps(vector))
              for (path, segment, start, end, symbol, body), vector in zip(pending, vectors)]
    db.execute(delete(FileChunk).where(FileChunk.repository_id == repo.id, FileChunk.branch == branch))
    db.execute(delete(FileDependency).where(FileDependency.repository_id == repo.id, FileDependency.branch == branch))
    db.add_all(chunks)
    # Resolve includes/imports against the whole eligible tree, even when only the selected
    # files receive embeddings. The linked file can be read on demand.
    paths = {item["path"] for item in eligible}
    db.add_all(FileDependency(repository_id=repo.id, branch=branch, source_path=path, target_path=target)
               for path, content in source_text.items() for target in dependencies(path, content, paths))
    db.flush()
    partial = bool(tree.get("truncated")) or len(eligible) > max_files or any(
        item["path"] not in contents for item in selected)
    if snapshot is None:
        snapshot = BranchSnapshot(repository_id=repo.id, branch=branch, head_sha=head_sha,
            indexed_files=indexed, file_chunks=len(pending), partial=partial)
        db.add(snapshot)
    else:
        snapshot.head_sha = head_sha
        snapshot.indexed_files = indexed
        snapshot.file_chunks = len(pending)
        snapshot.partial = partial
        snapshot.indexed_at = now()
    if graph_state is None:
        db.add(BranchGraphState(repository_id=repo.id, branch=branch, head_sha=head_sha))
    else:
        graph_state.head_sha = head_sha
        graph_state.built_at = now()
    db.flush()
    return {"indexed_files": indexed, "file_chunks": len(pending), "snapshot_sha": head_sha,
            "partial": partial, "cached": False}


def indexed_file(db: Session, repo, branch: str, path: str) -> dict | None:
    """Rebuild a file from its indexed chunks (exact line ranges), or None if it is not fully covered."""
    rows = db.execute(select(FileChunk).where(
        FileChunk.repository_id == repo.id, FileChunk.branch == branch, FileChunk.path == path
    ).order_by(FileChunk.start_line)).scalars().all()
    if not rows or any(row.start_line is None or row.end_line is None for row in rows):
        return None
    lines: dict[int, str] = {}
    for row in rows:
        text = row.body.split("\n", 1)[-1]  # the first line is the embedded "File ..." header
        for offset, line in enumerate(text.split("\n")):
            lines[row.start_line + offset] = line
    last = max(row.end_line for row in rows)
    if len(lines) < last:  # chunks were capped; a partial file would mislead
        return None
    head = rows[0].head_sha
    return {"path": path, "branch": branch, "content": "\n".join(lines[n] for n in range(1, last + 1)),
            "sha": head, "github_url": f"{repo.html_url.rstrip('/')}/blob/{head}/{path}",
            "indexed_snapshot": True}


def indexed_tree(db: Session, repo, branch: str) -> list[dict]:
    """File list from the last sync, used when GitHub cannot be reached."""
    rows = db.execute(select(FileChunk.path, FileChunk.end_line).where(
        FileChunk.repository_id == repo.id, FileChunk.branch == branch)).all()
    sizes: dict[str, int] = {}
    for path, end in rows:
        sizes[path] = max(sizes.get(path, 0), end or 0)
    return [{"path": path, "size": size} for path, size in sorted(sizes.items())]
