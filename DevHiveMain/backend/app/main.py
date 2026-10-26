import logging
from contextlib import asynccontextmanager
from threading import Event, Thread
from fastapi import FastAPI
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker
from .config import Settings
from .database import Base, make_engine
from .rag import embed
from .routes import auth, memory, projects, repositories
from .scheduler import weekly_sync_loop


def add_missing_columns(engine) -> None:
    """create_all never alters existing tables; add nullable columns introduced later."""
    wanted = {"file_chunks": {"start_line": "INTEGER", "end_line": "INTEGER", "symbol": "VARCHAR(200)"}}
    inspector = inspect(engine)
    schema = "devhive" if engine.dialect.name == "postgresql" else None
    with engine.begin() as connection:
        for table, columns in wanted.items():
            if not inspector.has_table(table, schema=schema):
                continue
            present = {c["name"] for c in inspector.get_columns(table, schema=schema)}
            qualified = f"{schema}.{table}" if schema else table
            for name, kind in columns.items():
                if name not in present:
                    connection.execute(text(f"ALTER TABLE {qualified} ADD COLUMN {name} {kind}"))


def warm_embedding_model(settings: Settings) -> None:
    """Load the embedding model before the first question so nobody waits for it."""
    try:
        embed("warm up", settings)
    except Exception:  # a missing model must not stop the API; the first query reports it
        logging.getLogger("devhive").warning("Embedding model warm-up failed", exc_info=True)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA IF NOT EXISTS devhive"))
    Base.metadata.create_all(engine)
    add_missing_columns(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = Event()
        worker = None
        if settings.warm_model and settings.embedding_model:
            Thread(target=warm_embedding_model, args=(settings,), name="devhive-warm", daemon=True).start()
        if settings.weekly_sync_enabled:
            worker = Thread(target=weekly_sync_loop,
                            args=(stop, app.state.session_factory, settings),
                            name="devhive-weekly-sync", daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker:
                worker.join(timeout=2)

    app = FastAPI(title="DevHive Main", version="0.2.0", lifespan=lifespan,
                  description="Modular monolith for GitHub projects, permissions and engineering memory")
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)

    @app.get("/health")
    def health():
        return {"status": "ok", "rag_backend": settings.rag_backend}

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(repositories.router)
    app.include_router(memory.router)
    return app


app = create_app()
