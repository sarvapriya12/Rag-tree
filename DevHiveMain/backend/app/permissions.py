from enum import IntFlag
from fnmatch import fnmatchcase
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import AuditEvent, BranchOverride, Membership, Project, Repository, Role, User


class Permission(IntFlag):
    VIEW_PROJECT = 1 << 0
    VIEW_REPOSITORY = 1 << 1
    CREATE_REPOSITORY = 1 << 2
    SYNC_REPOSITORY = 1 << 3
    PUSH = 1 << 4
    MERGE = 1 << 5
    VIEW_MEMORY = 1 << 6
    WRITE_TCC = 1 << 7
    VIEW_AUDIT = 1 << 8
    CREATE_BRANCH = 1 << 9


ALL_GRANTABLE = sum(int(p) for p in Permission)
READ_PERMISSIONS = int(Permission.VIEW_PROJECT | Permission.VIEW_REPOSITORY |
                       Permission.VIEW_MEMORY | Permission.SYNC_REPOSITORY)
BRANCH_PERMISSIONS = int(Permission.PUSH | Permission.MERGE |
                         Permission.WRITE_TCC | Permission.CREATE_BRANCH)
ROLE_PERMISSIONS = ALL_GRANTABLE & ~READ_PERMISSIONS
DEFAULT_ROLES = [
    ("Observer", "#64748b", 10, Permission(0)),
    ("Contributor", "#16a34a", 20, Permission.PUSH | Permission.WRITE_TCC | Permission.CREATE_BRANCH),
    ("Developer", "#2563eb", 30, Permission.PUSH | Permission.WRITE_TCC | Permission.CREATE_BRANCH),
    ("Maintainer", "#7c3aed", 40, Permission.CREATE_REPOSITORY | Permission.PUSH | Permission.MERGE | Permission.WRITE_TCC | Permission.VIEW_AUDIT | Permission.CREATE_BRANCH),
]


def log(db: Session, project_id: str | None, actor_id: str | None,
        action: str, target: str, allowed: bool, detail: str = "", repository_id: str | None = None) -> None:
    db.add(AuditEvent(project_id=project_id, actor_id=actor_id, action=action,
                      repository_id=repository_id, target=target[:250],
                      allowed=allowed, detail=detail[:1000]))


def project_for(db: Session, project_id: str, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if project.owner_id != user.id and db.get(Membership, (project_id, user.id)) is None:
        raise HTTPException(404, "Project not found")
    return project


def owner_only(db: Session, project_id: str, user: User) -> Project:
    project = project_for(db, project_id, user)
    if project.owner_id != user.id:
        log(db, project_id, user.id, "manage_permissions", project_id, False)
        db.commit()
        raise HTTPException(403, "Only the project owner can manage permissions")
    return project


def role_for(db: Session, project: Project, user: User) -> Role | None:
    if project.owner_id == user.id:
        return None
    member = db.get(Membership, (project.id, user.id))
    return db.get(Role, member.role_id) if member else None


def effective_bits(db: Session, project: Project, user: User, branch: str | None = None) -> int:
    if project.owner_id == user.id:
        return ALL_GRANTABLE
    role = role_for(db, project, user)
    if role is None:
        return 0
    bits = role.permissions | READ_PERMISSIONS
    if branch is None:
        return bits
    overrides = db.execute(select(BranchOverride).where(
        BranchOverride.project_id == project.id, BranchOverride.role_id == role.id
    )).scalars().all()
    # A literal branch rule also covers branches nested under its slash prefix.
    # A more specific child rule can still override the inherited rule.
    matches = [rule for rule in overrides if fnmatchcase(branch, rule.pattern)
               or ("*" not in rule.pattern and "?" not in rule.pattern
                   and branch.startswith(rule.pattern.rstrip("/") + "/"))]
    if matches:
        rule = max(matches, key=lambda r: (r.pattern == branch,
                      len(r.pattern.replace("*", "").replace("?", "")), len(r.pattern)))
        bits = (bits | (rule.allow_bits & BRANCH_PERMISSIONS)) & ~(rule.deny_bits & BRANCH_PERMISSIONS)
    return bits


def require_permission(db: Session, project: Project, user: User, permission: Permission,
                       branch: str | None = None, repository_id: str | None = None) -> None:
    allowed = bool(effective_bits(db, project, user, branch) & int(permission))
    log(db, project.id, user.id, permission.name.lower(), branch or project.id, allowed,
        repository_id=repository_id)
    db.commit()
    if not allowed:
        raise HTTPException(403, f"{permission.name.lower()} denied")


def repository_for(db: Session, repo_id: str, user: User) -> tuple[Repository, Project]:
    repo = db.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(404, "Repository not found")
    try:
        project = project_for(db, repo.project_id, user)
    except HTTPException:
        raise HTTPException(404, "Repository not found") from None
    return repo, project


def valid_branch(branch: str) -> bool:
    import re
    return (0 < len(branch) <= 200 and ".." not in branch
            and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", branch)))
