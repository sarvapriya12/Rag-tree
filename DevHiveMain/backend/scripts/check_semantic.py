"""Download/check the local semantic model with its cache kept in backend/.cache."""

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")
os.environ.setdefault("HF_HOME", str(BACKEND / ".cache" / "huggingface"))

from app.config import Settings  # noqa: E402
from app.rag import embed  # noqa: E402


settings = Settings.from_env()
if not settings.embedding_model:
    raise RuntimeError("Set DEVHIVE_EMBEDDING_MODEL in backend/.env first")
first = embed("find the background refresh job", settings)
second = embed("locate the scheduled memory worker", settings)
similarity = sum(a * b for a, b in zip(first, second))
print(f"Model ready: {settings.embedding_model}; dimensions={len(first)}; related-query similarity={similarity:.3f}")
