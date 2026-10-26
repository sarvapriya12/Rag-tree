"""Refresh previously indexed branches after an embedding model change."""

import sys
from pathlib import Path

from sqlalchemy import select

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402
from app.models import BranchSnapshot, MemoryChunk, Project, Repository, User  # noqa: E402
from app.routes.repositories import client_for_repository  # noqa: E402
from app.syncing import sync_branch  # noqa: E402


with app.state.session_factory() as db:
    indexed = set(db.execute(select(MemoryChunk.repository_id, MemoryChunk.branch).distinct()).all())
    indexed.update(db.execute(select(BranchSnapshot.repository_id, BranchSnapshot.branch)).all())
    for repo_id, branch in sorted(indexed):
        repo = db.get(Repository, repo_id)
        if repo is None:
            continue
        project = db.get(Project, repo.project_id)
        owner = db.get(User, project.owner_id)
        github = client_for_repository(db, repo, project, owner, app.state.settings)
        result = sync_branch(db, app.state.settings, github, repo, project, branch,
                             audit_action="repository.sync.maintenance")
        print(f"{repo.github_owner}/{repo.github_name} [{branch}]: "
              f"{result['file_chunks']} code sections, {result['imported']} new commits, "
              f"rebuilt={result['rebuilt_embeddings']}")
