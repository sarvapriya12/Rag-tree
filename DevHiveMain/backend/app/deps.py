from collections.abc import Generator
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from .config import Settings
from .models import User
from .security import authenticated_user


bearer = HTTPBearer(auto_error=False)


def get_db(request: Request) -> Generator[Session, None, None]:
    with request.app.state.session_factory() as db:
        yield db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def current_user(db: Session = Depends(get_db),
                 credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    return authenticated_user(db, f"Bearer {credentials.credentials}" if credentials else None)
