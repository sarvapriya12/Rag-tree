from datetime import datetime, timedelta, timezone
import secrets
import re
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..config import Settings
from ..deps import bearer, current_user, get_db, get_settings
from ..github import GitHubClient
from ..models import (ExternalIdentity, GitHubProfile, Invitation, Membership,
                      PasswordResetUse, Project, Role, SessionToken, User, UserLLMCredential, now)
from ..permissions import log
from ..schemas import GitHubTokenIn, GroqKeyIn, LoginIn, PasswordResetIn, RegisterIn, SupabaseTokenIn
from ..security import (create_session, decrypt_github_token, encrypt_github_token,
                        password_hash, password_matches, token_hash)
from ..supabase_auth import verified_user
from ..security import token_cipher


router = APIRouter(prefix="/auth", tags=["Identity"])


def present(user: User, db: Session) -> dict:
    profile = db.get(GitHubProfile, user.id)
    return {"id": user.id, "email": user.email, "name": user.name,
            "github_connected": bool(user.github_token_ciphertext),
            "github_login": profile.login if profile else None}


def accept_invitations(db: Session, user: User) -> None:
    pending = db.execute(select(Invitation).where(Invitation.email == user.email)).scalars().all()
    for invitation in pending:
        project = db.get(Project, invitation.project_id)
        role = db.get(Role, invitation.role_id)
        if project and role and role.project_id == project.id and project.owner_id != user.id:
            member = db.get(Membership, (project.id, user.id))
            if member is None:
                db.add(Membership(project_id=project.id, user_id=user.id, role_id=role.id))
            else:
                member.role_id = role.id
            log(db, project.id, user.id, "invitation.accept", invitation.id, True)
        db.delete(invitation)


def supabase_identity(db: Session, data: dict) -> ExternalIdentity | None:
    return db.execute(select(ExternalIdentity).where(
        ExternalIdentity.provider == "supabase", ExternalIdentity.subject == data["id"]
    )).scalar_one_or_none()


@router.post("/register", status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if db.execute(select(User.id).where(User.email == email)).first():
        raise HTTPException(409, "Email already registered")
    user = User(email=email, name=body.name.strip(), password_hash=password_hash(body.password))
    db.add(user)
    try:
        db.flush()
        token, expires = create_session(db, user)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Email already registered") from exc
    return {"user": present(user, db), "token": token, "expires_at": expires.isoformat()}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(User).where(User.email == body.email.strip().lower())).scalar_one_or_none()
    # Check a dummy hash for unknown users to avoid a fast account-existence path.
    dummy = "qAysVVOipxSIQV3fPOQwUA==$v_GUWSm73u6wpnMsQYleZII83McXoJq6b6etT8EWWtY="
    if user and user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
        raise HTTPException(429, "Too many attempts; try again later")
    matched = password_matches(body.password, user.password_hash if user else dummy)
    if not user or not matched:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= 5:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                user.failed_logins = 0
            db.commit()
        raise HTTPException(401, "Invalid email or password")
    user.failed_logins = 0
    user.locked_until = None
    token, expires = create_session(db, user)
    db.commit()
    return {"user": present(user, db), "token": token, "expires_at": expires.isoformat()}


@router.post("/supabase")
def login_supabase(body: SupabaseTokenIn, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    data = verified_user(settings, body.access_token)
    email = data["email"].strip().lower()
    identity = supabase_identity(db, data)
    if identity:
        user = db.get(User, identity.user_id)
        if user is None or user.email != email:
            raise HTTPException(409, "Supabase email differs from the linked DevHive account")
    else:
        # Supabase has verified control of this email. Use the existing workspace
        # account so Google sign-in preserves memberships and repositories.
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            metadata = data.get("user_metadata") or {}
            name = str(metadata.get("full_name") or metadata.get("name") or email.split("@", 1)[0]).strip()[:100]
            user = User(email=email, name=name if len(name) >= 2 else email[:100],
                        password_hash=password_hash(secrets.token_urlsafe(40)))
            db.add(user)
            db.flush()
        db.add(ExternalIdentity(provider="supabase", subject=data["id"], user_id=user.id))
    accept_invitations(db, user)
    token, expires = create_session(db, user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Account was updated; please retry sign in") from exc
    return {"user": present(user, db), "token": token, "expires_at": expires.isoformat()}


@router.post("/password/reset")
def reset_password(body: PasswordResetIn, db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)):
    # The email link grants a Supabase session; only Supabase's verified email
    # determines which local account may be changed.
    data = verified_user(settings, body.access_token)
    email = data["email"].strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "No DevHive password account exists for this email")
    proof_hash = token_hash(body.access_token)
    if db.get(PasswordResetUse, proof_hash):
        raise HTTPException(409, "This recovery link has already been used")
    user.password_hash = password_hash(body.new_password)
    user.failed_logins = 0
    user.locked_until = None
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    db.add(PasswordResetUse(token_hash=proof_hash, user_id=user.id))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "This recovery link has already been used") from exc
    return {"reset": True}


@router.post("/supabase/link")
def link_supabase(body: SupabaseTokenIn, db: Session = Depends(get_db),
                  user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    data = verified_user(settings, body.access_token)
    if data["email"].strip().lower() != user.email:
        raise HTTPException(403, "Supabase email must match your DevHive account")
    identity = supabase_identity(db, data)
    if identity and identity.user_id != user.id:
        raise HTTPException(409, "Supabase account is linked to someone else")
    if not identity:
        db.add(ExternalIdentity(provider="supabase", subject=data["id"], user_id=user.id))
    accept_invitations(db, user)
    db.commit()
    return {"linked": True}


@router.post("/logout", status_code=204)
def logout(db: Session = Depends(get_db), user: User = Depends(current_user),
           credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    row = db.execute(select(SessionToken).where(SessionToken.token_hash == token_hash(credentials.credentials))).scalar_one_or_none()
    if row:
        db.delete(row)
        db.commit()


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return present(user, db)


@router.get("/llm")
def llm_settings(user: User = Depends(current_user), db: Session = Depends(get_db),
                 settings: Settings = Depends(get_settings)):
    personal = db.get(UserLLMCredential, user.id) is not None
    # "shared" means the workspace key answers for everyone who has no personal key.
    return {"provider": "groq", "model": settings.groq_model, "configured": personal,
            "shared": bool(settings.groq_api_key), "active": personal or bool(settings.groq_api_key)}


@router.put("/llm")
def save_llm_settings(body: GroqKeyIn, db: Session = Depends(get_db),
                      user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    if not 20 <= len(body.api_key) <= 300 or not re.fullmatch(r"gsk_[A-Za-z0-9_-]+", body.api_key):
        raise HTTPException(422, "Enter a valid Groq API key")
    ciphertext = token_cipher(settings.secret).encrypt(body.api_key.encode()).decode()
    credential = db.get(UserLLMCredential, user.id)
    if credential is None:
        db.add(UserLLMCredential(user_id=user.id, provider="groq", key_ciphertext=ciphertext))
    else:
        credential.key_ciphertext = ciphertext
        credential.updated_at = now()
    db.commit()
    return {"provider": "groq", "model": settings.groq_model, "configured": True,
            "shared": bool(settings.groq_api_key), "active": True}


@router.delete("/llm", status_code=204)
def delete_llm_settings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    credential = db.get(UserLLMCredential, user.id)
    if credential:
        db.delete(credential)
        db.commit()


@router.put("/github")
def connect_github(body: GitHubTokenIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    identity = GitHubClient(body.token).identity()
    login = identity.get("login", "")
    if not login:
        raise HTTPException(502, "GitHub did not return an account login")
    if body.profile_url:
        parsed = urlsplit(body.profile_url.strip())
        if (parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}
                or parsed.query or parsed.fragment or
                parsed.path.lower() not in {f"/{login.lower()}", f"/{login.lower()}/"}):
            raise HTTPException(422, "GitHub profile URL must match the token owner")
    user.github_token_ciphertext = encrypt_github_token(settings.secret, body.token)
    profile = db.get(GitHubProfile, user.id)
    if profile:
        profile.login = login
    else:
        db.add(GitHubProfile(user_id=user.id, login=login))
    db.commit()
    return {"connected": True, "github_login": login}


@router.delete("/github", status_code=204)
def disconnect_github(db: Session = Depends(get_db), user: User = Depends(current_user)):
    user.github_token_ciphertext = None
    profile = db.get(GitHubProfile, user.id)
    if profile:
        db.delete(profile)
    db.commit()


@router.get("/github/repositories")
def github_repositories(user: User = Depends(current_user), settings: Settings = Depends(get_settings)):
    token = decrypt_github_token(settings.secret, user.github_token_ciphertext)
    return [repo for repo in GitHubClient(token).my_repositories() if repo["can_push"]]
