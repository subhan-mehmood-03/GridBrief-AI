from __future__ import annotations

import pytest
from sqlalchemy import text

from gridbrief.db import MIGRATIONS_DIR, applied_migrations, run_migrations

pytestmark = pytest.mark.usefixtures("clean_schema")


def test_migrations_run_on_empty_database(engine):
    # conftest's clean_schema fixture already dropped + re-migrated once;
    # this asserts every migration file made it into schema_migrations.
    expected = {p.name for p in MIGRATIONS_DIR.glob("*.sql")}
    assert applied_migrations(engine) == expected
    assert len(expected) >= 6  # sanity check we're not silently missing files


def test_migrations_are_idempotent(engine):
    # Running again should apply zero new migrations.
    second_run = run_migrations(engine)
    assert second_run == []


def test_expected_tables_exist(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' order by table_name"
            )
        ).fetchall()
    tables = {r[0] for r in rows}
    expected_tables = {
        "sources",
        "raw_items",
        "timeseries",
        "documents",
        "chunks",
        "editions",
        "edition_claims",
        "eval_runs",
        "ingestion_watermarks",
        "ingestion_runs",
        "breaking_triggers",
        "schema_migrations",
    }
    assert expected_tables.issubset(tables)


def test_pgvector_extension_enabled(engine):
    with engine.connect() as conn:
        row = conn.execute(
            text("select extname from pg_extension where extname = 'vector'")
        ).fetchone()
    assert row is not None
