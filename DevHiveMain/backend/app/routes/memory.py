import json
import re
from collections import Counter
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import chat_jobs
from ..config import Settings
from ..deps import current_user, get_db, get_settings
from ..llm import explain
from ..models import CommitRecord, FileChunk, FileDependency, FileReviewNote, TCCRecord, User
from ..permissions import Permission, log, repository_for, require_permission, valid_branch
from ..rag import embed, rank_linked_files, search, store_chunk
from ..retrieval import is_broad, linked_windows, resolve_profile, retrieve_evidence
from ..schemas import ChatIn, FileChatIn, FileReviewNoteIn, FileReviewNoteUpdateIn, TCCIn
from ..scope import normalize_scope, valid_path
from .repositories import client_for_repository, require_writable_repo


router = APIRouter(tags=["Engineering memory"])


def file_scope(db: Session, repo_id: str, branch: str, path: str, user: User):
    repo, project = repository_for(db, repo_id, user)
    if not valid_branch(branch) or not valid_path(path):
        raise HTTPException(422, "Invalid branch or file path")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo_id)
    require_permission(db, project, user, Permission.VIEW_MEMORY, branch, repo_id)
    return repo, project


_AUTHOR = re.compile(r"^(.*?)(?: \(@([^)]+)\))?$")


def author_names(authors: set[str]) -> dict[str, str]:
    """Map raw commit authors to one display name per person.

    Older commits often lack a linked GitHub login, so the same person appears as
    "Name", "Name (@login)" and "login"; merge them through the login.
    """
    login_of: dict[str, str] = {}
    display: dict[str, str] = {}
    for author in sorted(authors):
        name, login = _AUTHOR.match(author).groups()
        if login:
            login_of[name.lower()] = login_of[login.lower()] = login.lower()
            display.setdefault(login.lower(), author)
    mapping = {}
    for author in authors:
        name, login = _AUTHOR.match(author).groups()
        key = (login or login_of.get(name.lower()) or name).lower()
        mapping[author] = display.get(key, author)
    return mapping


def branch_history(db: Session, repo_id: str, branch: str) -> tuple[list, dict, dict]:
    """Imported commits (newest first, parsed file lists), their TCC records and author display names."""
    rows = db.execute(select(CommitRecord).where(CommitRecord.repository_id == repo_id,
        CommitRecord.branch == branch)).scalars().all()
    rows.sort(key=lambda c: c.committed_at or "", reverse=True)
    parsed = []
    for commit in rows[:500]:
        try:
            files = json.loads(commit.files_json)
        except ValueError:
            files = []
        parsed.append((commit, files))
    tcc = {t.commit_id: t for t in db.execute(select(TCCRecord).join(
        CommitRecord, TCCRecord.commit_id == CommitRecord.id).where(
        CommitRecord.repository_id == repo_id, CommitRecord.branch == branch)).scalars()}
    return parsed, tcc, author_names({commit.author for commit, _ in parsed})


def team_summary(history: tuple[list, dict, dict], limit: int = 8) -> list[dict]:
    """Who worked on the branch: commit counts, active period and the files they touched most."""
    people: dict[str, dict] = {}
    for commit, files in history[0]:
        person = history[2].get(commit.author, commit.author)
        entry = people.setdefault(person, {"person": person, "commits": 0,
            "first_at": None, "last_at": None, "files": Counter(), "recent": []})
        entry["commits"] += 1
        date = commit.committed_at
        if date and (entry["first_at"] is None or date < entry["first_at"]):
            entry["first_at"] = date
        if date and (entry["last_at"] is None or date > entry["last_at"]):
            entry["last_at"] = date
        entry["files"].update(f.get("name", "") for f in files[:30] if f.get("name"))
        if len(entry["recent"]) < 3:
            entry["recent"].append({"sha": commit.sha, "message": commit.message.splitlines()[0][:120] if commit.message else "",
                                    "committed_at": date})
    ranked = sorted(people.values(), key=lambda p: p["commits"], reverse=True)[:limit]
    for entry in ranked:
        entry["files"] = [name for name, _ in entry["files"].most_common(5)]
    return ranked


def file_review_data(db: Session, repo_id: str, branch: str, path: str, user: User, owner_id: str,
                     history: tuple[list, dict, dict] | None = None, repo_url: str = "") -> dict:
    note_rows = db.execute(select(FileReviewNote).where(
        FileReviewNote.repository_id == repo_id, FileReviewNote.branch == branch,
        FileReviewNote.path == path).order_by(FileReviewNote.created_at.desc())).scalars().all()
    notes = [{"id": note.id, "body": note.body,
              "author": (db.get(User, note.author_id).name if db.get(User, note.author_id) else "Former member"),
              "author_id": note.author_id, "can_edit": note.author_id == user.id or owner_id == user.id,
              "created_at": note.created_at.isoformat(), "updated_at": note.updated_at.isoformat()}
             for note in note_rows]
    deps = db.execute(select(FileDependency).where(FileDependency.repository_id == repo_id,
        FileDependency.branch == branch, FileDependency.source_path == path)).scalars().all()
    used_by = db.execute(select(FileDependency).where(FileDependency.repository_id == repo_id,
        FileDependency.branch == branch, FileDependency.target_path == path)).scalars().all()
    commits, tcc_by_commit, names = history if history is not None else branch_history(db, repo_id, branch)
    changes = []
    people: Counter = Counter()
    last_seen: dict[str, str] = {}
    thoughts = [{"id": note["id"], "body": note["body"], "author": note["author"],
                 "kind": "review", "created_at": note["created_at"]} for note in notes]
    for commit, files in commits:
        touched = next((row for row in files if row.get("name") == path), None)
        if touched is None:
            continue
        person = names.get(commit.author, commit.author)
        people[person] += 1
        if commit.committed_at and commit.committed_at > last_seen.get(person, ""):
            last_seen[person] = commit.committed_at
        changes.append({"sha": commit.sha, "author": person,
                        "message": commit.message.splitlines()[0] if commit.message else "",
                        "committed_at": commit.committed_at, "status": touched.get("status", ""),
                        "additions": touched.get("additions", 0), "deletions": touched.get("deletions", 0),
                        "url": f"{repo_url}/commit/{commit.sha}" if repo_url else None})
        tcc = tcc_by_commit.get(commit.id)
        if tcc:
            author = db.get(User, tcc.user_id)
            thoughts.append({"id": tcc.id, "body": tcc.thought,
                             "author": author.name if author else commit.author,
                             "kind": "commit thought", "created_at": tcc.created_at.isoformat()})
    return {"path": path, "branch": branch, "notes": notes,
            "thoughts": thoughts, "changes": changes[:12],
            "contributors": [{"person": person, "commits": count, "last_at": last_seen.get(person)}
                             for person, count in people.most_common(8)],
            "imports": sorted({row.target_path for row in deps}),
            "used_by": sorted({row.source_path for row in used_by})}


@router.get("/repositories/{repo_id}/file-review")
def file_review(repo_id: str, branch: str, path: str,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, project = file_scope(db, repo_id, branch, path, user)
    return file_review_data(db, repo_id, branch, path, user, project.owner_id)


@router.post("/repositories/{repo_id}/file-review/notes", status_code=201)
def add_file_note(repo_id: str, body: FileReviewNoteIn,
                  db: Session = Depends(get_db), user: User = Depends(current_user),
                  settings: Settings = Depends(get_settings)):
    repo, project = file_scope(db, repo_id, body.branch, body.path, user)
    if len(body.body.strip()) < 2:
        raise HTTPException(422, "Thought must contain at least two characters")
    client_for_repository(db, repo, project, user, settings).file(
        repo.github_owner, repo.github_name, body.branch, body.path)
    note = FileReviewNote(repository_id=repo_id, branch=body.branch, path=body.path,
                          author_id=user.id, body=body.body.strip())
    db.add(note)
    log(db, project.id, user.id, "file_review.note.create", body.path, True, repository_id=repo_id)
    db.commit()
    return {"id": note.id}


@router.put("/repositories/{repo_id}/file-review/notes/{note_id}")
def edit_file_note(repo_id: str, note_id: str, body: FileReviewNoteUpdateIn,
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    note = db.get(FileReviewNote, note_id)
    if note is None or note.repository_id != repo_id:
        raise HTTPException(404, "Review note not found")
    _, project = file_scope(db, repo_id, note.branch, note.path, user)
    if note.author_id != user.id and project.owner_id != user.id:
        raise HTTPException(403, "Only the author or project owner can edit this thought")
    if len(body.body.strip()) < 2:
        raise HTTPException(422, "Thought must contain at least two characters")
    note.body = body.body.strip()
    from ..models import now
    note.updated_at = now()
    log(db, project.id, user.id, "file_review.note.edit", note.path, True, repository_id=repo_id)
    db.commit()
    return {"id": note.id, "updated_at": note.updated_at.isoformat()}


def focused_code(content: str, question: str, maximum: int = 32) -> tuple[str, int]:
    lines = content.splitlines()
    if not lines:
        return "", 1
    terms = {word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", question)}
    terms -= {"what", "where", "this", "that", "with", "from", "does", "file", "code", "about", "explain"}
    scores = [sum(term in line.lower() for term in terms) for line in lines]
    anchor = max(range(len(lines)), key=lambda i: scores[i]) if terms else 0
    start = max(0, anchor - 5)
    if start + maximum > len(lines):
        start = max(0, len(lines) - maximum)
    return "\n".join(lines[start:start + maximum]), start + 1


@router.post("/repositories/{repo_id}/file-chat")
def chat_about_file(repo_id: str, body: FileChatIn,
                    db: Session = Depends(get_db), user: User = Depends(current_user),
                    settings: Settings = Depends(get_settings)):
    repo, project = file_scope(db, repo_id, body.branch, body.path, user)
    github = client_for_repository(db, repo, project, user, settings)
    source = github.file(repo.github_owner, repo.github_name, body.branch, body.path)
    review = file_review_data(db, repo_id, body.branch, body.path, user, project.owner_id)
    code, start_line = focused_code(source["content"], body.message)
    indexed = db.execute(select(FileChunk.id).where(FileChunk.repository_id == repo_id,
        FileChunk.branch == body.branch, FileChunk.path == body.path).limit(1)).first() is not None
    snippets = [{"path": body.path, "code": code, "start_line": start_line, "kind": "current file"}]
    terms = {word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", body.message)}
    neighbors = review["imports"] + review["used_by"]
    if neighbors:
        ranked = rank_linked_files(db, settings, repo_id, body.branch, neighbors, body.message)
        included = {body.path}
        for chunk in ranked:
            if len(snippets) >= 3 or chunk.path in included:
                continue
            snippets.append({"path": chunk.path, "code": chunk.body.split("\n", 1)[-1][:1800],
                             "start_line": None, "kind": "linked file at last sync"})
            included.add(chunk.path)
        for path in neighbors:
            if len(snippets) >= 3:
                break
            if path in included or any(part in path.lower() for part in (".env", "secret", "credential", "private_key")):
                continue
            try:
                linked = github.file(repo.github_owner, repo.github_name, body.branch, path)
            except HTTPException:
                continue
            if not terms.intersection(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", linked["content"].lower())):
                continue
            linked_code, linked_line = focused_code(linked["content"], body.message, maximum=20)
            snippets.append({"path": path, "code": linked_code, "start_line": linked_line,
                             "kind": "linked file, current branch"})
            included.add(path)
    latest = review["changes"][0] if review["changes"] else None
    summary = f"{body.path} on {body.branch}. "
    if latest:
        summary += f"Latest indexed change: {latest['message']} by {latest['author']}. "
    if review["imports"]:
        summary += f"It imports {', '.join(review['imports'][:4])}. "
    if review["used_by"]:
        summary += f"Used by {', '.join(review['used_by'][:4])}. "
    if not indexed:
        summary += "This file is outside the last indexed snapshot; related context may be incomplete."
    explanation, llm_status = explain(db, settings, user, body.message,
        {"branch": body.branch, "snippets": snippets, "thoughts": review["thoughts"]})
    log(db, project.id, user.id, "memory.file_chat", body.path, True, repository_id=repo_id)
    db.commit()
    return {"summary": explanation or summary.strip(), "llm_status": llm_status,
            "snippets": snippets, "thoughts": review["thoughts"],
            "changes": review["changes"], "imports": review["imports"],
            "used_by": review["used_by"], "indexed": indexed}


@router.post("/repositories/{repo_id}/tcc", status_code=201)
def submit_tcc(repo_id: str, body: TCCIn,
               db: Session = Depends(get_db), user: User = Depends(current_user),
               settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    require_writable_repo(db, repo, project, user)
    if not valid_branch(body.branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.PUSH, body.branch, repo_id)
    require_permission(db, project, user, Permission.WRITE_TCC, body.branch, repo_id)
    commit = db.execute(select(CommitRecord).where(CommitRecord.repository_id == repo_id,
        CommitRecord.branch == body.branch, CommitRecord.sha == body.sha.lower())).scalar_one_or_none()
    if commit is None:
        raise HTTPException(404, "Import this commit first")
    if db.execute(select(TCCRecord.id).where(TCCRecord.commit_id == commit.id)).first():
        raise HTTPException(409, "TCC already recorded for this commit")
    record = TCCRecord(commit_id=commit.id, user_id=user.id, thought=body.thought,
                       change_summary=body.change_summary, comment=body.comment)
    db.add(record)
    store_chunk(db, settings, repo_id=repo_id, branch=body.branch, commit_id=commit.id,
                kind="tcc", body=f"Thought: {body.thought}\nChange summary: {body.change_summary}\nComment: {body.comment}")
    log(db, project.id, user.id, "memory.tcc", commit.sha, True, repository_id=repo_id)
    db.commit()
    return {"id": record.id, "sha": commit.sha, "branch": body.branch}


@router.get("/repositories/{repo_id}/search")
def search_memory(repo_id: str, q: str = Query(min_length=2, max_length=300),
                  db: Session = Depends(get_db), user: User = Depends(current_user),
                  settings: Settings = Depends(get_settings)):
    _, project = repository_for(db, repo_id, user)
    result = search(db, settings, repo_id, project, user, q)
    log(db, project.id, user.id, "memory.search", repo_id, True,
        f"hits={len(result['citations'])}", repository_id=repo_id)
    db.commit()
    return {"question": q, **result}




HISTORY_QUESTION = re.compile(
    r"\b(who|when|why|decision|commit|commits|thought|contributor|contributors|contribution|"
    r"contributions|author|authors|history|changed|change|added|wrote|written|worked)\b", re.I)


def prepare_chat(db: Session, project, user: User, repo, body: ChatIn) -> tuple[str, list[str]]:
    """Validate the branch, permissions and scope of a question; returns (branch, normalized scope)."""
    branch = body.branch or repo.default_branch
    if not valid_branch(branch):
        raise HTTPException(422, "Invalid branch")
    require_permission(db, project, user, Permission.VIEW_REPOSITORY, branch, repo.id)
    require_permission(db, project, user, Permission.VIEW_MEMORY, branch, repo.id)
    if body.path and not body.branch:
        raise HTTPException(422, "Choose a branch for file questions")
    try:
        scope = normalize_scope(body.scope or ([body.path] if body.path else []))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return branch, scope


def answer_question(db: Session, settings: Settings, user: User, repo, project, body: ChatIn,
                    progress=None) -> dict:
    """Retrieve folder-diverse code windows inside the scope, add history, then explain at the chosen depth."""
    repo_id = repo.id
    branch, scope = prepare_chat(db, project, user, repo, body)
    profile = resolve_profile(body.depth, settings.groq_tpm)
    broad = is_broad(body.message, scope, body.depth)
    if progress:
        progress("retrieving", 0, 0, "Searching the index")
    query_vec = embed(body.message, settings)
    retrieved = retrieve_evidence(db, settings, repo_id, branch, body.message, profile, scope, query_vec, broad)
    windows, stats = retrieved["windows"], retrieved["stats"]
    if body.path and not body.scope and not windows:
        # A file outside the last snapshot: read it live, as the explorer's file questions do.
        focused = chat_about_file(repo_id, FileChatIn(branch=branch, path=body.path, message=body.message),
                                  db, user, settings)
        return {"answer": focused["summary"], "citations": [], "snippets": focused["snippets"],
                "tool_used": "inspect_file", "thoughts": focused["thoughts"], "imports": focused["imports"],
                "used_by": focused["used_by"], "llm_status": focused["llm_status"], "files": [],
                "branch": branch, "depth": body.depth, "scope": scope, "retrieval": stats}
    if scope and not stats["candidates"]:
        log(db, project.id, user.id, "memory.chat", repo_id, True,
            f"tool=memory_search; depth={body.depth}; scope={len(scope)}; windows=0", repository_id=repo_id)
        db.commit()
        return {"answer": f"Nothing is indexed in the selected scope on `{branch}`. "
                          "Choose different folders or sync the branch.",
                "citations": [], "files": [], "snippets": [], "thoughts": [], "branch": branch,
                "tool_used": "memory_search", "llm_status": "no_safe_evidence",
                "depth": body.depth, "scope": scope, "retrieval": stats, "outline": None}
    paths = {chunk.path for chunk in retrieved["pool"]}
    single = len(scope) == 1 and paths == {scope[0]}
    history = branch_history(db, repo_id, branch)
    repo_url = repo.html_url.rstrip("/")
    if single:  # one file: also show the best-matching files it includes or is used by
        neighbors = file_review_data(db, repo_id, branch, scope[0], user, project.owner_id,
                                     history=history, repo_url=repo_url)
        windows = windows + linked_windows(db, settings, repo_id, branch,
                                           neighbors["imports"] + neighbors["used_by"], body.message,
                                           profile["window_lines"])
    files = []
    for window in windows:
        review = file_review_data(db, repo_id, branch, window["path"], user, project.owner_id,
                                  history=history, repo_url=repo_url)
        start, end = window["start_line"], window["end_line"]
        files.append({"path": window["path"], "symbol": window["symbol"], "symbols": window["symbols"],
            "start_line": start, "end_line": end, "code": window["code"], "sha": window["head_sha"],
            "kind": "indexed file",
            "github_url": f"{repo_url}/blob/{window['head_sha']}/{window['path']}"
                          + (f"#L{start}-L{end}" if start and end else ""),
            "history": review["changes"][:6], "contributors": review["contributors"],
            "thoughts": review["thoughts"][:4], "imports": review["imports"][:8],
            "used_by": review["used_by"][:8], "score": window["score"], "group": window["group"],
            "sections": window["sections"], "seeds": window["seeds"], "duplicates": window["duplicates"],
            "focus_start": window["focus_start"], "focus_end": window["focus_end"],
            # The code actually sent to the model (budgeted per folder by depth); `code` is the whole window.
            "excerpt": window.get("excerpt"), "extras": window.get("extras", True)})
    # Commits and recorded thoughts are searched alongside the code, inside the same scope.
    result = search(db, settings, repo_id, project, user, body.message, limit=profile["commits"] + 4,
                    branch=branch, query_vec=query_vec, scope=scope)
    result["files"] = files
    result["snippets"] = [{"path": f["path"], "code": f["code"], "start_line": f["start_line"],
                           "end_line": f["end_line"], "kind": "indexed file", "sha": f["sha"]} for f in files[:3]]
    result["thoughts"] = [t for f in files for t in f["thoughts"]][:6]
    if HISTORY_QUESTION.search(body.message) or not files:
        result["team"] = team_summary(history)
    result["branch"] = branch
    result["tool_used"] = ("inspect_file" if single else "code_lookup") if files else "memory_search"
    result["outline"] = retrieved["outline"]
    result["depth"], result["scope"] = body.depth, scope
    result["broad"] = broad  # an overview: the answer must cover every folder in scope
    result["answer"] = extractive_answer(branch, files, result.get("team"), result["answer"], retrieved["outline"])
    explanation, result["llm_status"] = explain(db, settings, user, body.message, result, profile, progress)
    if explanation:
        result["answer"] = explanation
    result["retrieval"] = {**stats, "evidence_chars": profile["evidence_chars"],
                           "llm_calls": result.pop("llm_calls", 0), "map_reduce": profile["map_reduce"]}
    log(db, project.id, user.id, "memory.chat", repo_id, True,
        f"tool={result['tool_used']}; depth={body.depth}; scope={len(scope)}; windows={len(files)}; "
        f"folders={stats['folders_covered']}; with_code={stats['folders_with_code']}", repository_id=repo_id)
    db.commit()
    return result


@router.post("/repositories/{repo_id}/chat")
def chat_memory(repo_id: str, body: ChatIn,
                db: Session = Depends(get_db), user: User = Depends(current_user),
                settings: Settings = Depends(get_settings)):
    repo, project = repository_for(db, repo_id, user)
    return answer_question(db, settings, user, repo, project, body)


@router.post("/repositories/{repo_id}/chat/jobs", status_code=202)
def start_chat_job(repo_id: str, body: ChatIn, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(current_user),
                   settings: Settings = Depends(get_settings)):
    """Run a slow (Very high) question in the background; poll GET .../chat/jobs/{job_id}."""
    repo, project = repository_for(db, repo_id, user)
    prepare_chat(db, project, user, repo, body)  # reject bad input now rather than inside the job
    factory, user_id = request.app.state.session_factory, user.id

    def work(progress):
        with factory() as session:  # the request's session is closed once this response is sent
            owner = session.get(User, user_id)
            job_repo, job_project = repository_for(session, repo_id, owner)
            return answer_question(session, settings, owner, job_repo, job_project, body, progress)

    try:
        job_id = chat_jobs.submit(user.id, repo_id, work)
    except chat_jobs.TooManyJobs:
        raise HTTPException(429, "Wait for your running questions to finish, or cancel one") from None
    return {"job_id": job_id, "status": "queued"}


@router.get("/repositories/{repo_id}/chat/jobs/{job_id}")
def chat_job_status(repo_id: str, job_id: str,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    repository_for(db, repo_id, user)
    job = chat_jobs.get(job_id, user.id, repo_id)
    if job is None:
        raise HTTPException(404, "Question not found")
    return job


@router.delete("/repositories/{repo_id}/chat/jobs/{job_id}", status_code=204)
def cancel_chat_job(repo_id: str, job_id: str,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    repository_for(db, repo_id, user)
    if not chat_jobs.cancel(job_id, user.id, repo_id):
        raise HTTPException(404, "Question not found")
    return Response(status_code=204)


def extractive_answer(branch: str, files: list[dict], team: list[dict] | None, fallback: str,
                      outline: dict | None = None) -> str:
    """Readable Markdown summary built only from indexed data, used when no model answers."""
    if not files and not team:
        return fallback
    lines = []
    if files:
        lead = files[0]
        place = f"`{lead['path']}`" + (f" · `{lead['symbol']}`" if lead.get("symbol") else "")
        if lead.get("start_line"):
            place += f" · lines {lead['start_line']}-{lead['end_line']}"
        lines.append(f"**Closest match on `{branch}`:** {place}")
        folders: dict[str, list[dict]] = {}
        for item in files:
            folders.setdefault(item.get("group") or "(root)", []).append(item)
        digests = (outline or {}).get("folders") or []
        if len(files) > 1 or len(digests) > 1:
            total = max(len(digests), len(folders))
            lines.append(f"**Coverage:** {total} of {total} folder{'' if total == 1 else 's'} · "
                         f"{len(folders)} with code excerpts · {len(files)} code location{'' if len(files) == 1 else 's'}")
            rows = [f"- `{name}`: " + ", ".join(
                        f"`{item['path'].rsplit('/', 1)[-1]}`" + (f" · `{item['symbol']}`" if item.get("symbol") else "")
                        + (f" (lines {item['start_line']}-{item['end_line']})" if item.get("start_line") else "")
                        for item in members[:3])
                    for name, members in folders.items()]
            # Folders without excerpts are still covered by their digest line.
            rows += [f"- `{row['path']}`: summary only · {row['files']} files"
                     + (f" · key: {', '.join(f'`{s}`' for s in row['symbols'][:3])}" if row.get("symbols") else "")
                     for row in digests if row["path"] not in folders]
            lines.append("**By folder:**\n" + "\n".join(rows[:40]))
        if lead["history"]:
            latest = lead["history"][0]
            lines.append(f"**Latest change:** {latest['message']} - {latest['author']}, "
                         f"{str(latest['committed_at'] or '')[:10]} (`{latest['sha'][:7]}`)")
        if lead["contributors"]:
            lines.append("**Contributors to this file:** " + ", ".join(
                f"{c['person']} ({c['commits']})" for c in lead["contributors"][:5]))
        if lead["imports"] or lead["used_by"]:
            lines.append("**Related files:** " + ", ".join(f"`{p}`" for p in (lead["imports"] + lead["used_by"])[:6]))
    if team:
        lines.append("**Team on this branch:** " + "; ".join(
            f"{p['person']} - {p['commits']} commits ({str(p['first_at'] or '')[:10]} to {str(p['last_at'] or '')[:10]})"
            for p in team[:5]))
    return "\n\n".join(lines)
