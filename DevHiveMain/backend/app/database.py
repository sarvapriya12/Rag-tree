from collections.abc import Generator
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    if url.startswith("sqlite"):
        options = {"connect_args": {"check_same_thread": False}}
    elif url.startswith("postgresql"):
        # Keep application tables outside Supabase's default public API schema.
        options = {"connect_args": {"options": "-csearch_path=devhive,extensions,public"}}
    else:
        options = {}
    engine = create_engine(url, pool_pre_ping=True, **options)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def session_dependency(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    with factory() as db:
        yield db
