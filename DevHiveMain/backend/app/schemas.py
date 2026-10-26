from typing import Annotated, Literal
from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=12, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class GitHubTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    profile_url: str | None = Field(default=None, min_length=20, max_length=200)


class GroqKeyIn(BaseModel):
    # Validate in the route so rejected keys are not reflected in a 422 body.
    api_key: str


class SupabaseTokenIn(BaseModel):
    access_token: str = Field(min_length=20, max_length=10000)


class PasswordResetIn(SupabaseTokenIn):
    new_password: str = Field(min_length=12, max_length=200)


class ProjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    color: str = Field(default="#64748b", pattern=r"^#[0-9A-Fa-f]{6}$")
    permissions: list[str]
    position: int = Field(default=10, ge=0, le=1000)


class MembershipIn(BaseModel):
    email: str
    role_id: str


class InvitationIn(BaseModel):
    email: str = Field(min_length=5, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role_id: str


class BranchOverrideIn(BaseModel):
    role_id: str
    pattern: str = Field(min_length=1, max_length=200)
    allow: list[str] = []
    deny: list[str] = []


class RepositoryIn(BaseModel):
    github_owner: str
    github_name: str


class RepositoryUrlIn(BaseModel):
    url: str = Field(min_length=20, max_length=500)


class CreateGitHubRepositoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    private: bool = True


class TCCIn(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    thought: str = Field(min_length=1, max_length=5000)
    change_summary: str = Field(min_length=1, max_length=5000)
    comment: str = Field(min_length=1, max_length=5000)


class ChatIn(BaseModel):
    message: str = Field(min_length=2, max_length=300)
    branch: str | None = Field(default=None, max_length=200)
    path: str | None = Field(default=None, max_length=500)  # deprecated: same as scope=[path]
    # Folders and files the question may use; empty means the whole repository.
    scope: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=50)
    depth: Literal["low", "medium", "high", "very_high"] = "medium"


class FileChatIn(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=2, max_length=300)


class FileReviewNoteIn(BaseModel):
    branch: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=2, max_length=5000)


class FileReviewNoteUpdateIn(BaseModel):
    body: str = Field(min_length=2, max_length=5000)


class BranchCreateIn(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class PullRequestIn(BaseModel):
    head: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(default="", max_length=5000)
