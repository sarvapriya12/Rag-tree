import re
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..deps import current_user, get_db
from ..models import (AuditEvent, BranchOverride, Membership, Project,
                      Role, User, Invitation)
from ..config import Settings
from ..deps import get_settings
from ..permissions import (BRANCH_PERMISSIONS, ROLE_PERMISSIONS, DEFAULT_ROLES, Permission, effective_bits,
                           log, owner_only, project_for, require_permission)
from ..schemas import BranchOverrideIn, InvitationIn, MembershipIn, ProjectIn, RoleIn
from ..supabase_auth import send_invitation


router = APIRouter(tags=["Projects and permissions"])


def bits_from_names(names: list[str]) -> int:
    result = 0
    for name in names:
        try:
            result |= int(Permission[name.upper()])
        except KeyError as exc:
            raise HTTPException(422, f"Unknown permission: {name}") from exc
    return result


def role_json(role: Role) -> dict:
    return {"id": role.id, "name": role.name, "color": role.color,
            "position": role.position, "permissions": [p.name.lower() for p in Permission
                                                     if role.permissions & p and ROLE_PERMISSIONS & p]}


def project_json(project: Project, owner: bool) -> dict:
    return {"id": project.id, "name": project.name, "owner_id": project.owner_id,
            "is_owner": owner, "created_at": project.created_at.isoformat()}


@router.get("/permissions")
def permissions_catalog():
    return [{"name": p.name.lower(), "bit": int(p)} for p in Permission if ROLE_PERMISSIONS & p]


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = Project(name=body.name.strip(), owner_id=user.id)
    db.add(project)
    db.flush()
    roles = {}
    for name, color, position, bits in DEFAULT_ROLES:
        role = Role(project_id=project.id, name=name, color=color,
                    position=position, permissions=int(bits))
        db.add(role)
        roles[name] = role
    db.flush()
    # Branch exceptions layer on top of each role's base permission bits.
    for name, pattern, deny in [
        ("Contributor", "main", Permission.PUSH | Permission.MERGE),
        ("Contributor", "development", Permission.PUSH | Permission.MERGE),
        ("Developer", "main", Permission.PUSH | Permission.MERGE),
        ("Observer", "*", Permission.PUSH | Permission.MERGE | Permission.WRITE_TCC),
    ]:
        db.add(BranchOverride(project_id=project.id, role_id=roles[name].id,
                              pattern=pattern, allow_bits=0, deny_bits=int(deny)))
    log(db, project.id, user.id, "project.create", project.id, True)
    db.commit()
    return project_json(project, True)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(select(Project).outerjoin(
        Membership, and_(Membership.project_id == Project.id, Membership.user_id == user.id)
    ).where(or_(Project.owner_id == user.id, Membership.user_id == user.id)).distinct().order_by(Project.created_at.desc())).scalars()
    return [project_json(p, p.owner_id == user.id) for p in rows
            if p.owner_id == user.id or effective_bits(db, p, user) & int(Permission.VIEW_PROJECT)]


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for(db, project_id, user)
    return {**project_json(p, p.owner_id == user.id),
            "permissions": [perm.name.lower() for perm in Permission if effective_bits(db, p, user) & perm]}


@router.get("/projects/{project_id}/roles")
def list_roles(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.VIEW_PROJECT)
    return [role_json(r) for r in db.execute(select(Role).where(Role.project_id == project_id).order_by(Role.position)).scalars()]


@router.post("/projects/{project_id}/roles", status_code=201)
def create_role(project_id: str, body: RoleIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    role = Role(project_id=project_id, name=body.name.strip(), color=body.color,
                permissions=bits_from_names(body.permissions), position=body.position)
    db.add(role)
    try:
        db.flush()
        log(db, project_id, user.id, "role.create", role.id, True)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Role name already exists") from exc
    return role_json(role)


@router.put("/projects/{project_id}/roles/{role_id}")
def update_role(project_id: str, role_id: str, body: RoleIn,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    role = db.get(Role, role_id)
    if not role or role.project_id != project_id:
        raise HTTPException(404, "Role not found")
    role.name = body.name.strip()
    role.color = body.color
    role.permissions = bits_from_names(body.permissions)
    role.position = body.position
    try:
        log(db, project_id, user.id, "role.update", role.id, True)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Role name already exists") from exc
    return role_json(role)


@router.get("/projects/{project_id}/members")
def list_members(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.VIEW_PROJECT)
    owner = db.get(User, project.owner_id)
    result = [{"user_id": owner.id, "email": owner.email, "name": owner.name,
               "role_id": None, "role_name": "Owner", "is_owner": True}]
    rows = db.execute(select(Membership, User, Role).join(User, User.id == Membership.user_id)
                      .join(Role, Role.id == Membership.role_id)
                      .where(Membership.project_id == project_id)).all()
    return result + [{"user_id": u.id, "email": u.email, "name": u.name,
                      "role_id": role.id, "role_name": role.name, "is_owner": False}
                     for _, u, role in rows]


@router.get("/projects/{project_id}/invitations")
def list_invitations(project_id: str, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    rows = db.execute(select(Invitation).where(Invitation.project_id == project_id)
                      .order_by(Invitation.created_at.desc())).scalars()
    return [{"id": row.id, "email": row.email, "role_id": row.role_id,
             "created_at": row.created_at.isoformat()}
            for row in rows]


@router.post("/projects/{project_id}/invitations", status_code=201)
def invite_member(project_id: str, body: InvitationIn,
                  db: Session = Depends(get_db), user: User = Depends(current_user),
                  settings: Settings = Depends(get_settings)):
    project = owner_only(db, project_id, user)
    email = body.email.strip().lower()
    if db.execute(select(User.id).where(User.email == email)).first():
        raise HTTPException(409, "This person already has a DevHive account. Add them as a member directly.")
    role = db.get(Role, body.role_id)
    if role is None or role.project_id != project.id:
        raise HTTPException(404, "Role not found in this project")
    row = db.execute(select(Invitation).where(
        Invitation.project_id == project.id, Invitation.email == email)).scalar_one_or_none()
    send_invitation(settings, email)
    if row:
        row.role_id, row.invited_by = body.role_id, user.id
    else:
        row = Invitation(project_id=project.id, email=email, role_id=body.role_id,
                         invited_by=user.id)
        db.add(row)
    db.flush()
    log(db, project.id, user.id, "invitation.send", row.id, True, email)
    db.commit()
    return {"id": row.id, "email": row.email, "role_id": row.role_id,
            "created_at": row.created_at.isoformat()}


@router.delete("/projects/{project_id}/invitations/{invitation_id}", status_code=204)
def cancel_invitation(project_id: str, invitation_id: str,
                      db: Session = Depends(get_db), user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    row = db.get(Invitation, invitation_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(404, "Invitation not found")
    db.delete(row)
    log(db, project_id, user.id, "invitation.cancel", invitation_id, True)
    db.commit()


@router.put("/projects/{project_id}/members")
def set_member(project_id: str, body: MembershipIn,
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = owner_only(db, project_id, user)
    target = db.execute(select(User).where(User.email == body.email.strip().lower())).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "User must register before joining the project")
    if target.id == project.owner_id:
        raise HTTPException(409, "The project owner has a fixed owner role")
    role = db.get(Role, body.role_id)
    if role is None or role.project_id != project_id:
        raise HTTPException(404, "Role not found in this project")
    member = db.get(Membership, (project_id, target.id))
    if member:
        member.role_id = role.id
    else:
        db.add(Membership(project_id=project_id, user_id=target.id, role_id=role.id))
    log(db, project_id, user.id, "member.set", target.id, True, role.name)
    db.commit()
    return {"user_id": target.id, "email": target.email, "role_id": role.id, "role_name": role.name}


@router.delete("/projects/{project_id}/members/{member_id}", status_code=204)
def remove_member(project_id: str, member_id: str,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = owner_only(db, project_id, user)
    if member_id == project.owner_id:
        raise HTTPException(409, "Owner cannot be removed")
    member = db.get(Membership, (project_id, member_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    db.delete(member)
    log(db, project_id, user.id, "member.remove", member_id, True)
    db.commit()


@router.get("/projects/{project_id}/branch-overrides")
def list_overrides(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for(db, project_id, user)
    require_permission(db, project, user, Permission.VIEW_PROJECT)
    rows = db.execute(select(BranchOverride).where(BranchOverride.project_id == project_id)).scalars()
    return [{"id": r.id, "role_id": r.role_id, "pattern": r.pattern,
             "allow": [p.name.lower() for p in Permission if r.allow_bits & p],
             "deny": [p.name.lower() for p in Permission if r.deny_bits & p]} for r in rows]


@router.put("/projects/{project_id}/branch-overrides")
def set_override(project_id: str, body: BranchOverrideIn,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    role = db.get(Role, body.role_id)
    if role is None or role.project_id != project_id:
        raise HTTPException(404, "Role not found in this project")
    if not re.fullmatch(r"[A-Za-z0-9_./*?-]+", body.pattern) or ".." in body.pattern:
        raise HTTPException(422, "Invalid branch pattern")
    allow, deny = bits_from_names(body.allow), bits_from_names(body.deny)
    if (allow | deny) & ~BRANCH_PERMISSIONS:
        raise HTTPException(422, "This permission is not branch-specific")
    if allow & deny:
        raise HTTPException(422, "A permission cannot be allowed and denied by the same rule")
    row = db.execute(select(BranchOverride).where(
        BranchOverride.project_id == project_id,
        BranchOverride.role_id == role.id,
        BranchOverride.pattern == body.pattern)).scalar_one_or_none()
    if row:
        row.allow_bits, row.deny_bits = allow, deny
    else:
        row = BranchOverride(project_id=project_id, role_id=role.id,
                             pattern=body.pattern, allow_bits=allow, deny_bits=deny)
        db.add(row)
    log(db, project_id, user.id, "branch_override.set", body.pattern, True, role.name)
    db.commit()
    return {"id": row.id, "role_id": role.id, "pattern": row.pattern,
            "allow": body.allow, "deny": body.deny}


@router.get("/projects/{project_id}/audit")
def project_audit(project_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    owner_only(db, project_id, user)
    rows = db.execute(select(AuditEvent).where(AuditEvent.project_id == project_id)
                      .order_by(AuditEvent.created_at.desc()).limit(200)).scalars()
    return [{"id": r.id, "action": r.action, "target": r.target, "allowed": r.allowed,
             "actor_id": r.actor_id, "detail": r.detail, "created_at": r.created_at.isoformat()}
            for r in rows]
