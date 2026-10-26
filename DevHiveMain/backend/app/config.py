import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret: str
    rag_backend: str = "local"
    embedding_model: str = ""
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    supabase_secret_key: str = ""
    site_url: str = ""
    weekly_sync_enabled: bool = False
    weekly_sync_hour: int = 2
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    github_token: str = ""
    max_index_files: int = 400
    groq_tpm: int = 7000
    warm_model: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        secret = os.getenv("DEVHIVE_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("DEVHIVE_SECRET must be at least 32 characters")
        backend = os.getenv("DEVHIVE_RAG_BACKEND", "local")
        if backend not in {"local", "supabase", "turbovec"}:
            raise RuntimeError("DEVHIVE_RAG_BACKEND must be local, supabase, or turbovec")
        try:
            sync_hour = int(os.getenv("DEVHIVE_WEEKLY_SYNC_HOUR", "2"))
        except ValueError as exc:
            raise RuntimeError("DEVHIVE_WEEKLY_SYNC_HOUR must be 0 to 23") from exc
        if not 0 <= sync_hour <= 23:
            raise RuntimeError("DEVHIVE_WEEKLY_SYNC_HOUR must be 0 to 23")
        try:
            max_files = max(30, min(int(os.getenv("DEVHIVE_MAX_INDEX_FILES", "400")), 2000))
        except ValueError as exc:
            raise RuntimeError("DEVHIVE_MAX_INDEX_FILES must be a number") from exc
        try:
            groq_tpm = int(os.getenv("DEVHIVE_GROQ_TPM", "7000"))
        except ValueError as exc:
            raise RuntimeError("DEVHIVE_GROQ_TPM must be a number") from exc
        return cls(
            database_url=os.getenv("DEVHIVE_DATABASE_URL", "sqlite:///./devhive.db"),
            secret=secret,
            rag_backend=backend,
            embedding_model=os.getenv("DEVHIVE_EMBEDDING_MODEL", ""),
            supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
            supabase_publishable_key=os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
            supabase_secret_key=os.getenv("SUPABASE_SECRET_KEY", ""),
            site_url=os.getenv("DEVHIVE_SITE_URL", "").rstrip("/"),
            weekly_sync_enabled=os.getenv("DEVHIVE_WEEKLY_SYNC_ENABLED", "true").lower() not in {"0", "false", "no"},
            weekly_sync_hour=sync_hour,
            groq_api_key=os.getenv("DEVHIVE_GROQ_API_KEY", "").strip(),
            groq_model=os.getenv("DEVHIVE_GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b",
            github_token=os.getenv("DEVHIVE_GITHUB_TOKEN", "").strip(),
            max_index_files=max_files,
            groq_tpm=max(1000, groq_tpm),
            warm_model=os.getenv("DEVHIVE_WARM_MODEL", "true").lower() not in {"0", "false", "no"},
        )
