"""Database engine/session wiring for GridBrief AI.

Reads GRIDBRIEF_DATABASE_URL (the Supabase pooled connection string) and
exposes a SQLAlchemy 2 engine + session factory used by repository.py and
by the `gridbrief init-db` / `gridbrief migrate` CLI commands.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_ENGINE: Engine | None = None
_SESSION_FACTORY: sessionmaker | None = None


def get_database_url() -> str:
    from .config import get_settings

    url = str(get_settings().database_url)
    # SQLAlchemy needs the psycopg3 dialect explicitly.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(get_database_url(), pool_pre_ping=True, future=True)
    return _ENGINE


def get_session_factory() -> sessionmaker:
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SESSION_FACTORY


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional session; commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ensure_migrations_table(conn) -> None:
    conn.execute(
        text(
            """
            create table if not exists schema_migrations (
                filename     text primary key,
                applied_at   timestamptz not null default now()
            )
            """
        )
    )


def applied_migrations(engine: Engine | None = None) -> set[str]:
    engine = engine or get_engine()
    with engine.begin() as conn:
        _ensure_migrations_table(conn)
        rows = conn.execute(text("select filename from schema_migrations")).fetchall()
    return {row[0] for row in rows}


def pending_migrations(engine: Engine | None = None) -> list[Path]:
    engine = engine or get_engine()
    applied = applied_migrations(engine)
    all_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in all_files if f.name not in applied]


def run_migrations(engine: Engine | None = None) -> list[str]:
    """Apply every not-yet-applied migration in filename order.

    Each migration runs in its own transaction; running this twice is a
    no-op the second time (idempotent), which is what PRD §19.1 requires
    ("migrations run on an empty database").
    """
    engine = engine or get_engine()
    applied_names: list[str] = []
    for path in pending_migrations(engine):
        sql = path.read_text()
        with engine.begin() as conn:
            _ensure_migrations_table(conn)
            conn.execute(text(sql))
            conn.execute(
                text("insert into schema_migrations (filename) values (:f)"),
                {"f": path.name},
            )
        applied_names.append(path.name)
    return applied_names


def init_db() -> list[str]:
    """Alias used by `gridbrief init-db` — same as running all migrations
    against a fresh database."""
    return run_migrations()
