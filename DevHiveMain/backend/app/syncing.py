"""Shared manual and scheduled branch ingestion."""

import json
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import Settings
from .github import GitHubClient
from .indexing import index_branch_files
from .models import BranchEmbeddingState, CommitRecord, MemoryChunk, Project, Repository, now
from .permissions import log
from .rag import embed_many, embedding_profile, invalidate_turbovec, store_chunk


# Anonymous GitHub reads allow 60 requests/hour and each imported commit costs one.
ANON_COMMIT_LIMIT = 5


def sync_branch(db: Session, settings: Settings, github: GitHubClient,
                repo: Repository, project: Project, branch: str,
                limit: int = 10, actor_id: str | None = None,
                audit_action: str | None = None) -> dict:
    profile = embedding_profile(settings)
    state = db.get(BranchEmbeddingState, (repo.id, branch))
    rebuild = state is None or state.profile != profile
    if not github.token:
        limit = min(limit, ANON_COMMIT_LIMIT)
    summaries = github.commits(repo.github_owner, repo.github_name, branch, limit)
    head_detail = github.commit(repo.github_owner, repo.github_name, summaries[0]["sha"]) if summaries else None
    imported = 0
    known = set(db.execute(select(CommitRecord.sha).where(
        CommitRecord.repository_id == repo.id, CommitRecord.branch == branch)).scalars())
    fresh = [s["sha"] for s in summaries if s["sha"] not in known]
    details: dict[str, dict] = {}
    if head_detail and summaries[0]["sha"] in fresh:
        details[summaries[0]["sha"]] = head_detail
    todo = [sha for sha in fresh if sha not in details]
    if todo:
        # Network-bound and independent; the database is only touched from this thread.
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            for sha, detail in zip(todo, pool.map(
                    lambda s: github.commit(repo.github_owner, repo.github_name, s), todo)):
                details[sha] = detail
    if rebuild:
        # Re-embed history stored under an older model before adding new commits, in one batch.
        old = db.execute(select(MemoryChunk).where(
            MemoryChunk.repository_id == repo.id, MemoryChunk.branch == branch)).scalars().all()
        for chunk, vector in zip(old, embed_many([c.body for c in old], settings)):
            chunk.embedding_json = json.dumps(vector)
            if settings.rag_backend in {"supabase", "turbovec"} and db.bind.dialect.name == "postgresql":
                db.execute(text("""INSERT INTO memory_vectors(chunk_id, embedding)
                    VALUES (:id, CAST(:embedding AS extensions.vector))
                    ON CONFLICT (chunk_id) DO UPDATE SET embedding=EXCLUDED.embedding"""),
                    {"id": chunk.id, "embedding": chunk.embedding_json})
    created: list[tuple[CommitRecord, str]] = []
    for sha in fresh:
        detail = details[sha]
        data = detail.get("commit") or {}
        author = data.get("author") or {}
        login = (detail.get("author") or {}).get("login")
        name = author.get("name") or login or "unknown"
        files = [{"name": f.get("filename", ""), "status": f.get("status", ""),
                  "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
                 for f in detail.get("files", [])[:100]]
        diff = "\n".join((f.get("patch") or "")[:3000] for f in detail.get("files", [])[:20])[:12000]
        parents = detail.get("parents") or []
        commit = CommitRecord(repository_id=repo.id, branch=branch, sha=sha,
            parent_sha=parents[0].get("sha") if parents else None,
            author=f"{name} (@{login})" if login and login.lower() not in name.lower() else name,
            committed_at=author.get("date"),
            message=data.get("message") or "", files_json=json.dumps(files), diff_summary=diff)
        db.add(commit)
        db.flush()
        changed = ", ".join(f"{f['name']} (+{f['additions']}/-{f['deletions']})" for f in files)
        body = (f"Commit {sha[:12]} on {branch} by {commit.author} on {commit.committed_at or 'unknown date'}.\n"
                f"Message: {commit.message}\nChanged files: {changed}\nDiff context: {diff}")
        created.append((commit, body))
        imported += 1
    # One batched model call for every new commit instead of one call per commit.
    for (commit, body), vector in zip(created, embed_many([b for _, b in created], settings)):
        store_chunk(db, settings, repo_id=repo.id, branch=branch, commit_id=commit.id,
                    kind="commit", body=body, vector=vector)
    snapshot = {"indexed_files": 0, "file_chunks": 0, "snapshot_sha": None, "partial": False}
    if head_detail:
        tree_sha = ((head_detail.get("commit") or {}).get("tree") or {}).get("sha")
        if tree_sha:
            snapshot = index_branch_files(db, settings, github, repo, branch,
                                          summaries[0]["sha"], tree_sha, force=rebuild)
    if state is None:
        db.add(BranchEmbeddingState(repository_id=repo.id, branch=branch, profile=profile))
    else:
        state.profile = profile
        state.updated_at = now()
    log(db, project.id, actor_id, audit_action or ("repository.sync" if actor_id else "repository.sync.scheduled"),
        branch, True, f"imported={imported}", repository_id=repo.id)
    db.commit()
    invalidate_turbovec(repo.id)
    return {"branch": branch, "imported": imported, "examined": len(summaries),
            "embedding_profile": profile, "rebuilt_embeddings": rebuild, **snapshot}
