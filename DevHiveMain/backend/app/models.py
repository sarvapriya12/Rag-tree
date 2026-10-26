from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


def uid() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    github_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UserLLMCredential(Base):
    __tablename__ = "user_llm_credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), default="groq")
    key_ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GitHubProfile(Base):
    __tablename__ = "github_profiles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    login: Mapped[str] = mapped_column(String(100))


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class SessionToken(Base):
    __tablename__ = "session_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PasswordResetUse(Base):
    __tablename__ = "password_reset_uses"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    color: Mapped[str] = mapped_column(String(7), default="#64748b")
    permissions: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Membership(Base):
    __tablename__ = "memberships"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (UniqueConstraint("project_id", "email"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    invited_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "github_owner", "github_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    github_owner: Mapped[str] = mapped_column(String(100))
    github_name: Mapped[str] = mapped_column(String(100))
    html_url: Mapped[str] = mapped_column(String(400))
    default_branch: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReadOnlyRepository(Base):
    __tablename__ = "read_only_repositories"
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    public: Mapped[bool] = mapped_column(Boolean, default=False)


class BranchOverride(Base):
    __tablename__ = "branch_overrides"
    __table_args__ = (UniqueConstraint("project_id", "role_id", "pattern"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))
    pattern: Mapped[str] = mapped_column(String(200))
    allow_bits: Mapped[int] = mapped_column(Integer, default=0)
    deny_bits: Mapped[int] = mapped_column(Integer, default=0)


class CommitRecord(Base):
    __tablename__ = "commit_records"
    __table_args__ = (UniqueConstraint("repository_id", "branch", "sha"), Index("ix_commit_repo_branch", "repository_id", "branch"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(200))
    sha: Mapped[str] = mapped_column(String(40))
    parent_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    author: Mapped[str] = mapped_column(String(200))
    committed_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    files_json: Mapped[str] = mapped_column(Text)
    diff_summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TCCRecord(Base):
    __tablename__ = "tcc_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    commit_id: Mapped[str] = mapped_column(ForeignKey("commit_records.id", ondelete="CASCADE"), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    thought: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[str] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryChunk(Base):
    __tablename__ = "memory_chunks"
    __table_args__ = (UniqueConstraint("commit_id", "kind"), Index("ix_memory_repo_branch", "repository_id", "branch"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(200))
    commit_id: Mapped[str] = mapped_column(ForeignKey("commit_records.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileChunk(Base):
    __tablename__ = "file_chunks"
    __table_args__ = (Index("ix_file_repo_branch", "repository_id", "branch"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(200))
    head_sha: Mapped[str] = mapped_column(String(40))
    path: Mapped[str] = mapped_column(String(500))
    segment: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileDependency(Base):
    __tablename__ = "file_dependencies"
    __table_args__ = (UniqueConstraint("repository_id", "branch", "source_path", "target_path"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    branch: Mapped[str] = mapped_column(String(200))
    source_path: Mapped[str] = mapped_column(String(500))
    target_path: Mapped[str] = mapped_column(String(500))


class BranchGraphState(Base):
    __tablename__ = "branch_graph_states"
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    branch: Mapped[str] = mapped_column(String(200), primary_key=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileReviewNote(Base):
    __tablename__ = "file_review_notes"
    __table_args__ = (Index("ix_review_repo_branch_path", "repository_id", "branch", "path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(500))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BranchSnapshot(Base):
    __tablename__ = "branch_snapshots"
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    branch: Mapped[str] = mapped_column(String(200), primary_key=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    indexed_files: Mapped[int] = mapped_column(Integer)
    file_chunks: Mapped[int] = mapped_column(Integer)
    partial: Mapped[bool] = mapped_column(Boolean)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BranchEmbeddingState(Base):
    __tablename__ = "branch_embedding_states"
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    branch: Mapped[str] = mapped_column(String(200), primary_key=True)
    profile: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WeeklySyncRun(Base):
    __tablename__ = "weekly_sync_runs"
    week: Mapped[str] = mapped_column(String(10), primary_key=True)
    lease_owner: Mapped[str] = mapped_column(String(36))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    branches_completed: Mapped[int] = mapped_column(Integer, default=0)
    branches_failed: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    repository_id: Mapped[str | None] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), nullable=True, index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(250))
    allowed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
