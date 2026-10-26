"""In-memory background jobs for slow (Very high) questions, polled by the browser.

Jobs live in this process only, which fits the single-process deployment; a job is lost on restart.
The proxy buffers whole responses, so polling is simpler and sturdier than streaming.
"""

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from fastapi import HTTPException


JOB_TTL = 600  # seconds a finished job stays readable
MAX_ACTIVE_PER_USER = 2
_JOBS: dict[str, dict] = {}
_LOCK = Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="devhive-chat")


class JobCancelled(Exception):
    pass


class TooManyJobs(Exception):
    pass


def _purge(now: float) -> None:
    for job_id in [key for key, job in _JOBS.items()
                   if job["finished_at"] is not None and now - job["finished_at"] > JOB_TTL]:
        del _JOBS[job_id]


def submit(user_id: str, repo_id: str, work: Callable[[Callable], dict]) -> str:
    """Queue `work(progress)`; it returns the job id. Raises TooManyJobs past the per-user limit."""
    now = time.monotonic()
    with _LOCK:
        _purge(now)
        active = sum(1 for job in _JOBS.values()
                     if job["user_id"] == user_id and job["status"] in {"queued", "running"})
        if active >= MAX_ACTIVE_PER_USER:
            raise TooManyJobs()
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {"id": job_id, "user_id": user_id, "repo_id": repo_id, "status": "queued",
                         "stage": "queued", "step": 0, "total": 0, "message": "Waiting to start",
                         "created_at": now, "finished_at": None, "result": None, "error": None, "cancel": False}
    _EXECUTOR.submit(_run, job_id, work)
    return job_id


def _run(job_id: str, work: Callable[[Callable], dict]) -> None:
    job = _JOBS.get(job_id)
    if job is None:
        return
    if job["cancel"]:
        job.update(status="cancelled", stage="cancelled", finished_at=time.monotonic())
        return
    job.update(status="running", stage="retrieving", message="Searching the index")

    def progress(stage: str, step: int, total: int, message: str) -> None:
        if job["cancel"]:
            raise JobCancelled()
        job.update(stage=stage, step=step, total=total, message=message)

    try:
        result = work(progress)
        job.update(status="done", stage="done", result=result, message="Done")
    except JobCancelled:
        job.update(status="cancelled", stage="cancelled", message="Cancelled")
    except HTTPException as exc:
        job.update(status="error", stage="error", error=str(exc.detail), message="Failed")
    except Exception:  # never leak internals to the browser
        job.update(status="error", stage="error", error="The question could not be answered", message="Failed")
    finally:
        job["finished_at"] = time.monotonic()


def _owned(job_id: str, user_id: str, repo_id: str) -> dict | None:
    job = _JOBS.get(job_id)
    if job is None or job["user_id"] != user_id or job["repo_id"] != repo_id:
        return None
    return job


def get(job_id: str, user_id: str, repo_id: str) -> dict | None:
    job = _owned(job_id, user_id, repo_id)
    if job is None:
        return None
    end = job["finished_at"] or time.monotonic()
    view = {"job_id": job["id"], "status": job["status"], "stage": job["stage"], "step": job["step"],
            "total": job["total"], "message": job["message"], "elapsed": round(end - job["created_at"], 1)}
    if job["status"] == "done":
        view["result"] = job["result"]
    if job["status"] == "error":
        view["error"] = job["error"]
    return view


def cancel(job_id: str, user_id: str, repo_id: str) -> bool:
    job = _owned(job_id, user_id, repo_id)
    if job is None:
        return False
    job["cancel"] = True
    if job["status"] == "queued":
        job.update(status="cancelled", stage="cancelled", message="Cancelled", finished_at=time.monotonic())
    return True
