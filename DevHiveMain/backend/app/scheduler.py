"""Weekly Sunday memory refresh with a database-backed single-run lease."""

import logging
from datetime import datetime, time, timedelta, timezone
from threading import Event
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .models import BranchSnapshot, Project, Repository, User, WeeklySyncRun
from .syncing import sync_branch


logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
LEASE = timedelta(hours=1)


def next_run(now: datetime, hour: int = 2) -> datetime:
    local = now.astimezone(IST)
    days = (6 - local.weekday()) % 7
    candidate = datetime.combine(local.date() + timedelta(days=days), time(hour), tzinfo=IST)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


def due_week(now: datetime, hour: int = 2) -> str | None:
    local = now.astimezone(IST)
    if local.weekday() != 6 or local.time() < time(hour):
        return None
    return local.date().isoformat()


def claim_week(factory: sessionmaker, week: str, now: datetime) -> str | None:
    owner = str(uuid4())
    with factory() as db:
        row = db.get(WeeklySyncRun, week)
        if row is None:
            db.add(WeeklySyncRun(week=week, lease_owner=owner, lease_until=now + LEASE))
            try:
                db.commit()
                return owner
            except IntegrityError:
                db.rollback()
                return None
        if row.completed_at is not None:
            return None
        changed = db.execute(update(WeeklySyncRun).where(
            WeeklySyncRun.week == week, WeeklySyncRun.completed_at.is_(None),
            WeeklySyncRun.lease_until < now).values(lease_owner=owner, lease_until=now + LEASE))
        db.commit()
        return owner if changed.rowcount == 1 else None


def renew_lease(factory: sessionmaker, week: str, owner: str) -> bool:
    with factory() as db:
        changed = db.execute(update(WeeklySyncRun).where(
            WeeklySyncRun.week == week, WeeklySyncRun.lease_owner == owner,
            WeeklySyncRun.completed_at.is_(None)).values(lease_until=datetime.now(timezone.utc) + LEASE))
        db.commit()
        return changed.rowcount == 1


def refresh_selected_branches(factory: sessionmaker, settings: Settings,
                              week: str, owner: str) -> tuple[int, int]:
    # Import locally so the repository routes and scheduler share credential
    # selection without starting a scheduler while modules are imported.
    from .routes.repositories import client_for_repository

    with factory() as db:
        repo_ids = db.execute(select(Repository.id)).scalars().all()
    completed = failed = 0
    for repo_id in repo_ids:
        if not renew_lease(factory, week, owner):
            break
        try:
            with factory() as db:
                repo = db.get(Repository, repo_id)
                project = db.get(Project, repo.project_id) if repo else None
                account = db.get(User, project.owner_id) if project else None
                if repo is None or project is None or account is None:
                    failed += 1
                    continue
                github = client_for_repository(db, repo, project, account, settings)
                remote = github.repository(repo.github_owner, repo.github_name)
                default = remote.get("default_branch") or repo.default_branch
                if default != repo.default_branch:
                    repo.default_branch = default
                    db.commit()
                tracked = db.execute(select(BranchSnapshot.branch).where(
                    BranchSnapshot.repository_id == repo_id)).scalars().all()
                available = {item.get("name") for item in github.branches(repo.github_owner, repo.github_name)}
                branches = [name for name in dict.fromkeys([default, *tracked]) if name in available]
        except Exception:
            logger.exception("Weekly sync could not list branches for repository %s", repo_id)
            failed += 1
            continue
        for branch in branches:
            if not renew_lease(factory, week, owner):
                return completed, failed
            try:
                with factory() as db:
                    repo = db.get(Repository, repo_id)
                    project = db.get(Project, repo.project_id)
                    # Commits already imported are skipped, so this only fetches the week's new ones.
                    sync_branch(db, settings, github, repo, project, branch, limit=100)
                completed += 1
            except Exception:
                logger.exception("Weekly sync failed for repository %s branch %s", repo_id, branch)
                failed += 1
    return completed, failed


def run_due_sync(factory: sessionmaker, settings: Settings, now: datetime | None = None) -> bool:
    if not settings.weekly_sync_enabled:
        return False
    at = now or datetime.now(timezone.utc)
    week = due_week(at, settings.weekly_sync_hour)
    if week is None:
        return False
    owner = claim_week(factory, week, at)
    if owner is None:
        return False
    try:
        completed, failed = refresh_selected_branches(factory, settings, week, owner)
    except Exception:
        logger.exception("Weekly memory sync stopped unexpectedly")
        with factory() as db:
            db.execute(update(WeeklySyncRun).where(
                WeeklySyncRun.week == week, WeeklySyncRun.lease_owner == owner).values(
                lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
            db.commit()
        return False
    with factory() as db:
        db.execute(update(WeeklySyncRun).where(
            WeeklySyncRun.week == week, WeeklySyncRun.lease_owner == owner,
            WeeklySyncRun.completed_at.is_(None)).values(
            completed_at=datetime.now(timezone.utc), branches_completed=completed,
            branches_failed=failed))
        db.commit()
    logger.info("Weekly memory sync completed: %s branches, %s failures", completed, failed)
    return True


def weekly_sync_loop(stop: Event, factory: sessionmaker, settings: Settings) -> None:
    while not stop.is_set():
        try:
            run_due_sync(factory, settings)
        except Exception:
            logger.exception("Weekly memory scheduler check failed")
        stop.wait(60)
