import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import SessionToken, User


SESSION_DAYS = 7


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def password_matches(password: str, stored: str) -> bool:
    try:
        salt_text, expected_text = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(expected_text)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> tuple[str, datetime]:
    token = "dh_" + secrets.token_urlsafe(36)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.add(SessionToken(user_id=user.id, token_hash=token_hash(token), expires_at=expires))
    return token, expires


def authenticated_user(db: Session, authorization: str | None) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    token = authorization[7:]
    if not token:
        raise HTTPException(401, "Bearer token required")
    row = db.execute(select(SessionToken).where(SessionToken.token_hash == token_hash(token))).scalar_one_or_none()
    if row is None or row.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(401, "Session expired or invalid")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(401, "Session invalid")
    return user


def token_cipher(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_github_token(secret: str, token: str) -> str:
    return token_cipher(secret).encrypt(token.encode()).decode()


def decrypt_github_token(secret: str, ciphertext: str | None) -> str:
    if not ciphertext:
        raise HTTPException(409, "Connect a GitHub token first")
    try:
        return token_cipher(secret).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(500, "Stored GitHub token cannot be decrypted") from exc
