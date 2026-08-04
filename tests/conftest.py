from __future__ import annotations

import os

# Point at a local/test database BEFORE importing gridbrief.db, which
# reads GRIDBRIEF_DATABASE_URL at call time via get_database_url().
# In CI / local dev this should be a throwaway database — never the
# shared Supabase one.
os.environ.setdefault(
    "GRIDBRIEF_DATABASE_URL",
    "postgresql://postgres:localtest@localhost:5432/gridbrief_test",
)

import pytest
from sqlalchemy import text

from gridbrief.db import get_engine, run_migrations, session_scope
from gridbrief.repository import Repository


@pytest.fixture(scope="session")
def engine():
    return get_engine()


@pytest.fixture(autouse=True)
def clean_schema(engine):
    """Drop everything and re-run migrations before each test so tests
    never depend on order or leak state into each other."""
    with engine.begin() as conn:
        conn.execute(text("drop schema public cascade"))
        conn.execute(text("create schema public"))
    run_migrations(engine)
    yield


@pytest.fixture
def repo():
    with session_scope() as session:
        yield Repository(session)
