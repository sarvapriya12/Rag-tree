import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import Settings
from ..deps import current_user, get_db, get_settings
from ..github import GitHubClient
from ..models import (AuditEvent, BranchEmbeddingState, BranchSnapshot, CommitRecord, FileChunk,
                      Project, ReadOnlyRepository, Repository, User)
from ..permissions import (Permission, effective_bits, log, project_for,
                           repository_for, require_permission, valid_branch)
from ..indexing import indexed_file, indexed_tree
from ..syncing import sync_branch
from ..rag import embedding_profile
from ..schemas import BranchCreateIn, CreateGitHubRepositoryIn, PullRequestIn, RepositoryIn, RepositoryUrlIn
from ..security import decrypt_github_token
from ..scheduler import next_run


router = APIRouter(tags=["Repositories"])
TREE_LIMIT = 5000  # files returned to the explorer; vendored folders no longer crowd out later folders


def client_for(user: User, settings: Settings) -> GitHubClient:
    return GitHubClient(decrypt_github_token(settings.secret, user.github_token_ciphertext))


def client_for_repository(db: Session, repo: Repository, project: Project,
                          user: User, settings: Settings) -> GitHubClient:
    # Project membership is the read boundary. Use a registered account's
    # connection so teammates need no separate GitHub token for reading.
    link = db.get(ReadOnlyRepository, repo.id)
    if link:
        # A public URL link must never start exposing a repo that later became
        # private through a project member's token. Private links use only the
        # account that explicitly linked them.
        if link.public:
            # Read-only workspace token (if configured) lifts the anonymous rate limit.
            return GitHubClient(settings.github_token or None)
        account = db.get(User, repo.created_by)
        if account and account.github_token_ciphertext:
            return client_for(account, settings)
        raise HTTPException(503, "The linked private repository needs its creator's GitHub connection")
    for account_id in dict.fromkeys((repo.created_by, project.owner_id, user.id)):
        account = db.get(User, account_id)
        if account and account.github_token_ciphertext:
            return client_for(account, settings)
    raise HTTPException(503, "Connect the repository owner's GitHub account in Settings")


def is_read_only(db: Session, repo_id: str) -> bool:
    return db.get(ReadOnlyRepository, repo_id) is not None


def require_writable_repo(db: Session, repo: Repository, project: Project, user: User) -> None:
    if is_read_only(db, repo.id):
        log(db, project.id, user.id, "repository.write", repo.id, False,
            "URL-linked repository is view only", repository_id=repo.id)
        db.commit()
        raise HTTPException(403, "Repositories added by URL are view only")


def repo_json(repo: Repository, read_only: bool = False) -> dict:
    return {"id": repo.id, "project_id": repo.project_id, "github_owner": repo.github_owner,
            "github_name": repo.github_name, "full_name": f"{repo.github_owner}/{repo.github_name}",
            "html_url": repo.html_url, "default_branch": repo.default_branch,
            "read_only": read_only,
            "created_at": repo.created_at.isoformat()}


def register_repo(db: Session, project: Project, user: User, remote: dict,
                  read_only: bool = False) -> dict:
    owner = remote.get("owner", {}).get("login")
    name = remote.get("name")
    if not owner or not name or remote.get("full_name", "").lower() != f"{owner}/{name}".lower():
        raise HTTPException(502, "GitHub returned an invalid repository identity")
    if read_only and remote.get("private") is not False and not remote.get("permissions", {}).get("pull"):
        raise HTTPException(403, "Your connected GitHub account needs read access to this private repository")
    if not read_only and not remote.get("permissions", {}).get("push"):
        raise HTTPException(403, "Your GitHub token needs push access to link this repository")
    repo = Repository(project_id=project.id, github_owner=owner, github_name=name,
                      html_url=remote["html_url"], default_branch=remote.get("default_branch") or "main",
                      created_by=user.id)
    db.add(repo)
    try:
        db.flush()
        if read_only:
            db.add(ReadOnlyRepository(repository_id=repo.id, public=remote.get("private") is False))
        log(db, project.id, user.id, "repository.register", repo.id, True,
            f"{owner}/{name}", repository_id=repo.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Repository is already registered in this project") from exc
    return repo_json(repo, read_only)


@router.get("/projects/{project_id}/repositories")
def list_repositories(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for(db, project_id, user)
    rows = db.execute(select(Repository).where(Repository.project_id == project_id)
                      .order_by(Repository.created_at.desc())).scalars()
    return [repo_json(r, is_read_only(db, r.id)) for r in rows]


@router.post("/projects/{project_id}/repositories", status_code=201)
def link_repository(project_id: str, body: RepositoryIn,
                    db: Session = Depends(get_db), user: User = Depends(current_user),
                    settings: Settings = Depends(get_settings)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.CREATE_REPOSITORY)
    remote = client_for(user, settings).repository(body.github_owner, body.github_name)
    return register_repo(db, project, user, remote)


@router.post("/projects/{project_id}/repositories/from-url", status_code=201)
def link_repository_url(project_id: str, body: RepositoryUrlIn,
                        db: Session = Depends(get_db), user: User = Depends(current_user),
                        settings: Settings = Depends(get_settings)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.CREATE_REPOSITORY)
    try:
        parsed = urlsplit(body.url.strip())
    except ValueError as exc:
        raise HTTPException(422, "Enter a valid GitHub repository URL") from exc
    if (parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}
            or parsed.query or parsed.fragment):
        raise HTTPException(422, "Enter a GitHub repository URL such as https://github.com/owner/repo")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        raise HTTPException(422, "Enter a GitHub repository URL such as https://github.com/owner/repo")
    owner, name = parts
    name = name.removesuffix(".git")
    from ..github import validate_repo_slug
    validate_repo_slug(owner, name)
    try:
        remote = GitHubClient(settings.github_token or None).repository(owner, name)
    except HTTPException as exc:
        if exc.status_code != 404 or not user.github_token_ciphertext:
            raise
        remote = client_for(user, settings).repository(owner, name)
    return register_repo(db, project, user, remote, read_only=True)


@router.post("/projects/{project_id}/repositories/new", status_code=201)
def create_github_repository(project_id: str, body: CreateGitHubRepositoryIn,
                             db: Session = Depends(get_db), user: User = Depends(current_user),
                             settings: Settings = Depends(get_settings)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.CREATE_REPOSITORY)
    github = client_for(user, settings)
    created = github.create_repository(body.name, body.description, body.private)
    remote = github.repository(created["owner"]["login"], created["name"])
    return register_repo(db, project, user, remote)


@router.get("/repositories/{repo_id}")
def get_repository(repo_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    repo, project = repository_for(db, repo_id, user)
    read_only = is_read_only(db, repo_id)
    write_actions = {Permission.CREATE_BRANCH, Permission.PUSH, Permission.MERGE, Permission.WRITE_TCC}
    return {**repo_json(repo, read_only), "is_owner": project.owner_id == user.id,
            "permissions": [p.name.lower() for p in Permission if effective_bits(db, project, user) & p
                            and (not read_only or p not in write_actions)]}


@router.get("/repositories/{repo_id}/access")
def branch_access(repo_id: str, branch: str, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    _, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    bits = effective_bits(db, project, user, branch)
    read_only = is_read_only(db, repo_id)
    write_actions = {Permission.CREATE_BRANCH, Permission.PUSH, Permission.MERGE, Permission.WRITE_TCC}
    return {p.name.lower(): bool(bits & p) and (not read_only or p not in write_actions)
            for p in Permission}


@router.get("/repositories/{repo_id}/branches")
def branches(repo_id: str, db: Session = Depends(get_db), user: User = Depends(current_user),
             settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    try:
        rows = client_for_repository(db, repo, project, user, settings).branches(repo.github_owner, repo.github_name)
    except HTTPException as exc:
        if exc.status_code not in {502, 503}:
            raise
        # GitHub unreachable or rate limited: offer the default branch and every synced branch.
        synced = db.execute(select(BranchSnapshot.branch).where(BranchSnapshot.repository_id == repo.id)).scalars()
        rows = [{"name": name} for name in sorted({repo.default_branch, *synced})]
    return [{"name": r["name"]} for r in rows if effective_bits(db, project, user, r["name"])
            & int(Permission.VIEW_REPOSITORY)]


@router.post("/repositories/{repo_id}/branches", status_code=201)
def create_branch(repo_id: str, body: BranchCreateIn,
                  db: Session = Depends(get_db), user: User = Depends(current_user),
                  settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    require_writable_repo(db, repo, project, user)
    if not valid_branch(body.source) or not valid_branch(body.name):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.CREATE_BRANCH, body.source, repo_id)
    require_permission(db, project, user, Permission.CREATE_BRANCH, body.name, repo_id)
    github = client_for(user, settings)
    github.create_branch(repo.github_owner, repo.github_name, body.source, body.name)
    log(db, project.id, user.id, "branch.create", body.name, True,
        f"from={body.source}", repository_id=repo_id)
    db.commit()
    return {"name": body.name, "source": body.source}


@router.get("/repositories/{repo_id}/pulls")
def list_pull_requests(repo_id: str, db: Session = Depends(get_db),
                       user: User = Depends(current_user),
                       settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    rows = client_for_repository(db, repo, project, user, settings).pull_requests(
        repo.github_owner, repo.github_name)
    result = []
    for row in rows:
        head = row.get("head") or {}
        base = row.get("base") or {}
        head_branch, base_branch = head.get("ref"), base.get("ref")
        if not head_branch or not base_branch or not valid_branch(base_branch):
            continue
        result.append({"number": row.get("number"), "title": row.get("title"),
                       "head": head_branch, "base": base_branch,
                       "author": (row.get("user") or {}).get("login") or "unknown",
                       "url": row.get("html_url"), "created_at": row.get("created_at"),
                       "can_merge": not is_read_only(db, repo_id) and bool(
                           effective_bits(db, project, user, base_branch) & Permission.MERGE)})
    return result


@router.post("/repositories/{repo_id}/pulls", status_code=201)
def create_pull_request(repo_id: str, body: PullRequestIn,
                        db: Session = Depends(get_db), user: User = Depends(current_user),
                        settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    require_writable_repo(db, repo, project, user)
    if not valid_branch(body.head) or not valid_branch(body.base) or body.head == body.base:
        raise HTTPException(422, "Invalid pull request branches")
    require_permission(db, project, user, Permission.PUSH, body.head, repo_id)
    row = client_for(user, settings).create_pull_request(repo.github_owner, repo.github_name,
        body.head, body.base, body.title, body.body)
    log(db, project.id, user.id, "pull.create", str(row.get("number")), True,
        f"{body.head} -> {body.base}", repository_id=repo_id)
    db.commit()
    return {"number": row.get("number"), "url": row.get("html_url")}


@router.put("/repositories/{repo_id}/pulls/{number}/merge")
def merge_pull_request(repo_id: str, number: int,
                       db: Session = Depends(get_db), user: User = Depends(current_user),
                       settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    require_writable_repo(db, repo, project, user)
    if number < 1:
        raise HTTPException(422, "Invalid pull request number")
    github = client_for(user, settings)
    row = github.pull_request(repo.github_owner, repo.github_name, number)
    base = (row.get("base") or {}).get("ref")
    if not base or not valid_branch(base):
        raise HTTPException(422, "Invalid base branch")
    require_permission(db, project, user, Permission.MERGE, base, repo_id)
    result = github.merge_pull_request(repo.github_owner, repo.github_name, number)
    if not result.get("merged"):
        raise HTTPException(409, "GitHub did not merge this pull request")
    log(db, project.id, user.id, "pull.merge", str(number), True,
        f"base={base};sha={result.get('sha', '')}", repository_id=repo_id)
    db.commit()
    return {"merged": True, "sha": result.get("sha"), "base": base}


@router.get("/repositories/{repo_id}/activity")
def github_activity(repo_id: str, branch: str | None = None, actor: str | None = None,
                    kind: str | None = None, limit: int = Query(default=100, ge=1, le=300),
                    db: Session = Depends(get_db), user: User = Depends(current_user),
                    settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if branch is not None and not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    if branch is not None:
        require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    rows = client_for_repository(db, repo, project, user, settings).events(repo.github_owner, repo.github_name)
    visible = []
    for event in rows:
        payload = event.get("payload") or {}
        event_type = event.get("type")
        if event_type == "PushEvent":
            ref = payload.get("ref") or ""
            if not ref.startswith("refs/heads/"):
                continue
            name = ref.removeprefix("refs/heads/")
            action = "push"
            detail = f"Pushed {payload.get('size', 0)} commit(s)"
            sha = payload.get("head")
        elif event_type in {"CreateEvent", "DeleteEvent"} and payload.get("ref_type") == "branch":
            name = payload.get("ref") or ""
            action = "branch_created" if event_type == "CreateEvent" else "branch_deleted"
            detail = "Created branch" if event_type == "CreateEvent" else "Deleted branch"
            sha = None
        elif event_type == "PullRequestEvent":
            pull = payload.get("pull_request") or {}
            head = pull.get("head") or {}
            if (head.get("repo") or {}).get("full_name", "").lower() != f"{repo.github_owner}/{repo.github_name}".lower():
                continue
            base_name = (pull.get("base") or {}).get("ref") or ""
            if not valid_branch(base_name) or not effective_bits(db, project, user, base_name) & int(Permission.VIEW_REPOSITORY):
                continue
            name = head.get("ref") or ""
            action = "pull_request"
            detail = f"{payload.get('action', 'updated').capitalize()} pull request #{pull.get('number', '')}: {(pull.get('title') or '')[:180]}"
            sha = head.get("sha")
        else:
            continue
        if not valid_branch(name) or (branch is not None and name != branch):
            continue
        if not effective_bits(db, project, user, name) & int(Permission.VIEW_REPOSITORY):
            continue
        login = (event.get("actor") or {}).get("login") or "unknown"
        if actor and actor.lower() not in login.lower():
            continue
        if kind and action != kind:
            continue
        visible.append({"id": event.get("id"), "kind": action, "branch": name,
                        "actor": login, "created_at": event.get("created_at"),
                        "detail": detail, "sha": sha})
        if len(visible) >= limit:
            break
    return visible


@router.get("/repositories/{repo_id}/history")
def github_history(repo_id: str, branch: str, author: str | None = None,
                   db: Session = Depends(get_db), user: User = Depends(current_user),
                   settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    if author and (len(author) > 100 or not author.replace("-", "").replace("_", "").isalnum()):
        raise HTTPException(422, "Invalid author")
    rows = client_for_repository(db, repo, project, user, settings).history(repo.github_owner, repo.github_name, branch, author)
    return [{"sha": row.get("sha"), "branch": branch,
             "author": (row.get("author") or {}).get("login") or ((row.get("commit") or {}).get("author") or {}).get("name") or "unknown",
             "committer": (row.get("committer") or {}).get("login") or ((row.get("commit") or {}).get("committer") or {}).get("name") or "unknown",
             "committed_at": ((row.get("commit") or {}).get("committer") or {}).get("date"),
             "message": (row.get("commit") or {}).get("message") or "",
             "url": row.get("html_url")} for row in rows]


@router.get("/repositories/{repo_id}/contributors")
def contributors(repo_id: str, branch: str,
                 db: Session = Depends(get_db), user: User = Depends(current_user),
                 settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    rows = client_for_repository(db, repo, project, user, settings).history(
        repo.github_owner, repo.github_name, branch)
    imported = {item.sha: item for item in db.execute(select(CommitRecord).where(
        CommitRecord.repository_id == repo_id, CommitRecord.branch == branch,
        CommitRecord.sha.in_([row.get("sha") for row in rows if row.get("sha")]))).scalars()}
    result = {}
    for row in rows:
        person = (row.get("author") or {}).get("login") or \
            ((row.get("commit") or {}).get("author") or {}).get("name") or "unknown"
        sha = row.get("sha") or ""
        message = (row.get("commit") or {}).get("message") or ""
        entry = result.setdefault(person, {"person": person, "commits": 0,
            "last_at": None, "recent": []})
        entry["commits"] += 1
        date = ((row.get("commit") or {}).get("committer") or {}).get("date")
        if date and (entry["last_at"] is None or date > entry["last_at"]):
            entry["last_at"] = date
        if len(entry["recent"]) < 5:
            files = json.loads(imported[sha].files_json) if sha in imported else []
            entry["recent"].append({"sha": sha, "message": message.splitlines()[0][:180],
                                    "files": [f.get("name", "") for f in files[:5]]})
    return {"branch": branch, "examined": len(rows),
            "contributors": sorted(result.values(), key=lambda item: item["commits"], reverse=True)}


@router.get("/repositories/{repo_id}/history/{sha}")
def github_commit_detail(repo_id: str, sha: str, branch: str,
                         db: Session = Depends(get_db), user: User = Depends(current_user),
                         settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise HTTPException(422, "Invalid branch or commit SHA")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    github = client_for_repository(db, repo, project, user, settings)
    comparison = github.compare(repo.github_owner, repo.github_name, sha, branch)
    if comparison.get("status") not in {"ahead", "identical"}:
        raise HTTPException(404, "Commit is not on this branch")
    detail = github.commit(repo.github_owner, repo.github_name, sha)
    return {"sha": sha, "branch": branch,
            "message": (detail.get("commit") or {}).get("message") or "",
            "files": [{"name": f.get("filename"), "status": f.get("status"),
                       "additions": f.get("additions", 0), "deletions": f.get("deletions", 0),
                       "patch": (f.get("patch") or "")[:4000]}
                      for f in detail.get("files", [])[:100]]}


@router.get("/repositories/{repo_id}/file")
def file_preview(repo_id: str, branch: str, path: str,
                 db: Session = Depends(get_db), user: User = Depends(current_user),
                 settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    try:
        return client_for_repository(db, repo, project, user, settings).file(
            repo.github_owner, repo.github_name, branch, path)
    except HTTPException as exc:
        # GitHub unreachable or rate limited: serve the last synced snapshot of an indexed file.
        snapshot = indexed_file(db, repo, branch, path) if exc.status_code in {502, 503} else None
        if snapshot is None:
            raise
        return snapshot


@router.get("/repositories/{repo_id}/tree")
def repository_tree(repo_id: str, branch: str,
                    db: Session = Depends(get_db), user: User = Depends(current_user),
                    settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    try:
        github = client_for_repository(db, repo, project, user, settings)
        candidates = github.branches(repo.github_owner, repo.github_name)
        found = next((item for item in candidates if item.get("name") == branch), None)
        if found is None:
            raise HTTPException(404, "Branch not found")
        head_sha = (found.get("commit") or {}).get("sha")
        detail = github.commit(repo.github_owner, repo.github_name, head_sha)
        tree_sha = ((detail.get("commit") or {}).get("tree") or {}).get("sha")
        if not tree_sha:
            raise HTTPException(502, "GitHub omitted the tree SHA")
        tree = github.tree(repo.github_owner, repo.github_name, tree_sha)
    except HTTPException as exc:
        indexed = indexed_tree(db, repo, branch) if exc.status_code in {502, 503} else []
        if not indexed:
            raise
        snapshot = db.get(BranchSnapshot, (repo_id, branch))
        return {"branch": branch, "sha": snapshot.head_sha if snapshot else None,
                "files": [{**item, "indexed": True} for item in indexed[:TREE_LIMIT]],
                "partial": True, "indexed_snapshot": True}
    indexed_paths = set(db.execute(select(FileChunk.path).where(
        FileChunk.repository_id == repo_id, FileChunk.branch == branch).distinct()).scalars().all())
    files = [{"path": item["path"], "size": item.get("size", 0), "indexed": item["path"] in indexed_paths}
             for item in tree.get("tree", []) if item.get("type") == "blob"]
    files.sort(key=lambda item: item["path"])
    return {"branch": branch, "sha": head_sha, "files": files[:TREE_LIMIT],
            "partial": bool(tree.get("truncated")) or len(files) > TREE_LIMIT}


@router.get("/repositories/{repo_id}/scope-tree")
def scope_tree(repo_id: str, branch: str,
               db: Session = Depends(get_db), user: User = Depends(current_user),
               settings: Settings = Depends(get_settings)):
    """Indexed files with their section counts: exactly what a question can be scoped to."""
    _, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    require_permission(db, project, user, Permission.VIEW_MEMORY, branch, repo_id)
    rows = db.execute(select(FileChunk.path, func.count(FileChunk.id), func.max(FileChunk.end_line)).where(
        FileChunk.repository_id == repo_id, FileChunk.branch == branch)
        .group_by(FileChunk.path).order_by(FileChunk.path)).all()
    snapshot = db.get(BranchSnapshot, (repo_id, branch))
    state = db.get(BranchEmbeddingState, (repo_id, branch))
    return {"branch": branch, "sha": snapshot.head_sha if snapshot else None,
            "indexed_at": snapshot.indexed_at.isoformat() if snapshot and snapshot.indexed_at else None,
            "profile_ready": state is not None and state.profile == embedding_profile(settings),
            "total_files": len(rows), "total_chunks": sum(row[1] for row in rows),
            "files": [{"path": path, "chunks": count, "lines": lines or 0} for path, count, lines in rows]}


@router.get("/repositories/{repo_id}/commits")
def list_commits(repo_id: str, branch: str,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    rows = db.execute(select(CommitRecord).where(CommitRecord.repository_id == repo_id,
        CommitRecord.branch == branch).order_by(CommitRecord.created_at.desc()).limit(100)).scalars()
    return [{"id": c.id, "sha": c.sha, "branch": c.branch, "author": c.author,
             "committed_at": c.committed_at, "message": c.message,
             "files": json.loads(c.files_json)} for c in rows]


@router.post("/repositories/{repo_id}/sync")
def sync_repository(repo_id: str, branch: str,
                    limit: int = Query(default=200, ge=1, le=500),
                    db: Session = Depends(get_db), user: User = Depends(current_user),
                    settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    require_permission(db, project, user, Permission.SYNC_REPOSITORY, branch, repo_id)
    github = client_for_repository(db, repo, project, user, settings)
    return sync_branch(db, settings, github, repo, project, branch, limit, actor_id=user.id)


@router.get("/repositories/{repo_id}/sync-schedule")
def repository_sync_schedule(repo_id: str, branch: str,
                             db: Session = Depends(get_db), user: User = Depends(current_user),
                             settings: Settings = Depends(get_settings)):
    _, project = repository_for(db, repo_id, user)
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    snapshot = db.get(BranchSnapshot, (repo_id, branch))
    embedding_state = db.get(BranchEmbeddingState, (repo_id, branch))
    profile = embedding_profile(settings)
    return {"enabled": settings.weekly_sync_enabled, "day": "Sunday",
            "local_time": f"{settings.weekly_sync_hour:02d}:00",
            "time_zone": "Asia/Kolkata",
            "next_run": next_run(datetime.now(timezone.utc), settings.weekly_sync_hour).isoformat()
            if settings.weekly_sync_enabled else None,
            "last_indexed_at": snapshot.indexed_at.isoformat() if snapshot else None,
            "embedding_mode": "semantic" if settings.embedding_model else "lexical",
            "embedding_profile": profile,
            "needs_reindex": snapshot is not None and (embedding_state is None or embedding_state.profile != profile)}


@router.get("/repositories/{repo_id}/audit")
def repository_audit(repo_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, project = repository_for(db, repo_id, user)
    require_permission(db, project, user, Permission.VIEW_AUDIT, repository_id=repo_id)
    rows = db.execute(select(AuditEvent).where(AuditEvent.repository_id == repo_id)
                      .order_by(AuditEvent.created_at.desc()).limit(200)).scalars()
    return [{"id": r.id, "action": r.action, "target": r.target,
             "allowed": r.allowed, "actor_id": r.actor_id,
             "created_at": r.created_at.isoformat()} for r in rows]
