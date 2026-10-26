"""End-to-end check of DevHive retrieval against a real public GitHub repository.

Run from the backend folder:
    .\\.venv\\Scripts\\python.exe scripts/eval_repo.py [--repo URL] [--branch master] [--db PATH]
        [--depth low,medium,high,very_high]

It uses a throwaway SQLite database (never your dev database), the model and Groq settings from
backend/.env, and the real GitHub API. Set DEVHIVE_GITHUB_TOKEN for a full 29+ commit import;
anonymous runs are limited by GitHub to 60 requests per hour.

Three kinds of check run at every requested depth:
  * pointed questions: the expected file must be among the first three distinct files (low and medium);
  * coverage questions: an overview must give code from every top-level folder at every depth (the
    scope sets the source; depth only sets how much is read per folder, printed as code/folder);
  * a scoped question: every result must lie inside the selected folder.
Very high runs through the background-job endpoint and can take a minute or two on the free Groq tier.
"""

import argparse
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUESTIONS = [
    ("How does the cartoonifier detect edges and make the image look like a cartoon?", "cartoon.cpp"),
    ("Where is bundle adjustment implemented?", "BundleAdjuster.cpp"),
    ("How is the OCR classifier for number plates trained?", "trainOCR.cpp"),
    ("How is a face preprocessed before recognition?", "preprocessFace.cpp"),
    ("Where is the fluid solver for the Kinect wall?", "FluidSolver.cpp"),
    ("Which file detects markers in the iPhone AR chapter?", "MarkerDetector.cpp"),
    ("Who contributed to face recognition and when?", None),
]
# (question, minimum distinct top-level folders with code). Every depth covers the whole scope, so the
# overview questions must reach all 10 folders (9 chapters and the root) at Low just as at Very high.
COVERAGE = [
    ("What does each chapter of this repository do?", 10),
    ("Give an overview of the repository structure", 10),
    ("Which chapters use machine learning such as SVM, PCA or neural networks?", 3),
]
SCOPED = ("Chapter5_NumberPlateRecognition", "How is the OCR trained?", "trainOCR.cpp")
POLL_SECONDS = 2
JOB_TIMEOUT = 360


def folders_of(files: list[dict]) -> set[str]:
    return {f["path"].split("/", 1)[0] if "/" in f["path"] else "(root)" for f in files}


def ask(client, repo_id: str, headers: dict, branch: str, message: str, depth: str, scope=None):
    """POST /chat, or the job endpoints for Very high. Returns (answer dict or None, seconds, problem)."""
    body = {"message": message, "branch": branch, "depth": depth, **({"scope": scope} if scope else {})}
    started = time.perf_counter()
    if depth != "very_high":
        reply = client.post(f"/repositories/{repo_id}/chat", json=body, headers=headers)
        took = time.perf_counter() - started
        return (reply.json(), took, "") if reply.status_code == 200 else (None, took, f"HTTP {reply.status_code} {reply.text[:300]}")
    job = client.post(f"/repositories/{repo_id}/chat/jobs", json=body, headers=headers)
    if job.status_code != 202:
        return None, time.perf_counter() - started, f"HTTP {job.status_code} {job.text[:300]}"
    job_id = job.json()["job_id"]
    while time.perf_counter() - started < JOB_TIMEOUT:
        state = client.get(f"/repositories/{repo_id}/chat/jobs/{job_id}", headers=headers).json()
        if state["status"] == "done":
            return state["result"], time.perf_counter() - started, ""
        if state["status"] in {"error", "cancelled"}:
            return None, time.perf_counter() - started, f"job {state['status']}: {state.get('error', '')}"
        time.sleep(POLL_SECONDS)
    client.delete(f"/repositories/{repo_id}/chat/jobs/{job_id}", headers=headers)
    return None, time.perf_counter() - started, "job timed out"


def summary(data: dict) -> str:
    stats = data.get("retrieval", {})
    return (f"tool={data['tool_used']} llm={data['llm_status']} windows={stats.get('windows')} "
            f"folders={stats.get('folders_covered')}/{stats.get('folders_in_scope')} "
            f"with_code={stats.get('folders_with_code')} code/folder={stats.get('avg_excerpt_chars')} "
            f"calls={stats.get('llm_calls')} candidates={stats.get('candidates')}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="https://github.com/MasteringOpenCV/code")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--db", default=str(Path(tempfile.gettempdir()) / "devhive_eval.db"))
    parser.add_argument("--anon-commits", type=int, default=30)
    parser.add_argument("--depth", default="low,medium",
                        help="comma-separated depths to run: low, medium, high, very_high")
    parser.add_argument("--resync", action="store_true",
                        help="reuse the existing eval database and force a fresh file index")
    parser.add_argument("--skip-sync", action="store_true",
                        help="reuse the existing eval database and its index without calling GitHub "
                             "(for retrieval-only changes, or when GitHub is rate limiting)")
    args = parser.parse_args()
    if args.skip_sync:
        args.resync = True  # same login path; the sync itself is skipped below
    depths = [d.strip() for d in args.depth.split(",") if d.strip()]
    if not set(depths) <= {"low", "medium", "high", "very_high"}:
        parser.error("--depth takes low, medium, high and/or very_high")

    if not args.resync:
        for suffix in ("", "-journal"):
            Path(args.db + suffix).unlink(missing_ok=True)
    os.environ.setdefault("DEVHIVE_SECRET", secrets.token_urlsafe(48))
    os.environ["DEVHIVE_DATABASE_URL"] = f"sqlite:///{Path(args.db).as_posix()}"
    os.environ["DEVHIVE_WEEKLY_SYNC_ENABLED"] = "false"

    from fastapi.testclient import TestClient
    from app import syncing
    from app.config import Settings
    from app.main import create_app

    syncing.ANON_COMMIT_LIMIT = args.anon_commits
    settings = Settings.from_env()
    print(f"model={settings.embedding_model or 'lexical'} backend={settings.rag_backend} "
          f"llm={'shared key' if settings.groq_api_key else 'none'}/{settings.groq_model} "
          f"github={'token' if settings.github_token else 'anonymous'} depths={depths}")
    failures = 0
    with TestClient(create_app(settings)) as client:
        credentials = {"email": "eval@example.com", "password": "correct horse battery staple"}
        if args.resync:
            session = client.post("/auth/login", json=credentials).json()
            headers = {"Authorization": f"Bearer {session['token']}"}
            project = client.get("/projects", headers=headers).json()[0]
            repo = client.get(f"/projects/{project['id']}/repositories", headers=headers).json()[0]
            from app.models import BranchSnapshot
            with client.app.state.session_factory() as db:  # force the file index to rebuild
                row = db.get(BranchSnapshot, (repo["id"], args.branch))
                if row is not None and not args.skip_sync:
                    db.delete(row)
                    db.commit()
        else:
            registered = client.post("/auth/register", json={**credentials, "name": "Eval User"}).json()
            headers = {"Authorization": f"Bearer {registered['token']}"}
            project = client.post("/projects", json={"name": "Eval"}, headers=headers).json()
            linked = client.post(f"/projects/{project['id']}/repositories/from-url",
                                 json={"url": args.repo}, headers=headers)
            if linked.status_code != 201:
                print("link failed", linked.status_code, linked.text[:300])
                return 1
            repo = linked.json()
        if args.skip_sync:
            print("sync skipped: using the existing index")
        else:
            started = time.perf_counter()
            synced = client.post(f"/repositories/{repo['id']}/sync",
                                 params={"branch": args.branch, "limit": 200}, headers=headers)
            print(f"sync {synced.status_code} in {time.perf_counter() - started:.1f}s: {synced.text[:400]}")
            if synced.status_code != 200:
                return 1
        tree = client.get(f"/repositories/{repo['id']}/scope-tree", params={"branch": args.branch},
                          headers=headers).json()
        top_folders = sorted({f["path"].split("/", 1)[0] for f in tree["files"] if "/" in f["path"]})
        print(f"scope tree: {tree['total_files']} files, {tree['total_chunks']} sections, "
              f"{len(top_folders)} folders: {', '.join(top_folders)}")

        for depth in depths:
            print(f"\n=== depth: {depth} ===")
            if depth in {"low", "medium"}:  # pointed questions; skipped above medium to spare the Groq budget
                for question, expected in QUESTIONS:
                    data, took, problem = ask(client, repo["id"], headers, args.branch, question, depth)
                    if data is None:
                        print(f"\nQ: {question}\n  HTTP problem: {problem}")
                        failures += 1
                        continue
                    top = [f"{f['path'].rsplit('/', 1)[-1]}:{f['start_line']}-{f['end_line']}"
                           f"{' ' + f['symbol'] if f.get('symbol') else ''}" for f in data["files"]]
                    # Count distinct files: two windows of one file are still one file to the reader.
                    distinct = list(dict.fromkeys(f["path"] for f in data["files"]))[:3]
                    found = expected is None or any(expected in path for path in distinct)
                    failures += not found
                    print(f"\nQ: {question}\n  {'OK ' if found else 'MISS'} {took:.1f}s {summary(data)} files={top}")
                    first = data["files"][0] if data["files"] else None
                    if first and first["history"]:
                        h = first["history"][0]
                        print(f"  latest change: {h['sha'][:7]} {h['author']} {str(h['committed_at'])[:10]} {h['message'][:70]}")
                    print("  " + data["answer"][:600].replace("\n", "\n  "))
            for question, minimum in COVERAGE:
                data, took, problem = ask(client, repo["id"], headers, args.branch, question, depth)
                if data is None:
                    print(f"\nCOVERAGE Q: {question}\n  problem: {problem}")
                    failures += 1
                    continue
                reached = folders_of(data["files"])
                ok = len(reached) >= minimum
                failures += not ok
                print(f"\nCOVERAGE Q: {question}\n  {'OK ' if ok else 'MISS'} {took:.1f}s "
                      f"{summary(data)} folders={len(reached)} (need {minimum}): {', '.join(sorted(reached))}")
                print("  " + data["answer"][:500].replace("\n", "\n  "))
            scope, question, expected = SCOPED
            data, took, problem = ask(client, repo["id"], headers, args.branch, question, depth, [scope])
            if data is None:
                print(f"\nSCOPED Q: {question}\n  problem: {problem}")
                failures += 1
            else:
                inside = all(f["path"].startswith(scope + "/") for f in data["files"])
                found = any(expected in path for path in list(dict.fromkeys(f["path"] for f in data["files"]))[:3])
                failures += not (inside and found)
                print(f"\nSCOPED Q ({scope}): {question}\n  {'OK ' if inside and found else 'MISS'} {took:.1f}s "
                      f"{summary(data)} inside={inside} found={found}")
    print(f"\nfailures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
