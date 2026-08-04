"""`gridbrief init-db` and `gridbrief migrate` command implementations.

Person 1's project skeleton (Phase 1) owns the actual CLI entry point
(pyproject.toml `[project.scripts] gridbrief = ...`). Wire these two
functions in as subcommands there, e.g. with Typer:

    from gridbrief.cli_db import cmd_init_db, cmd_migrate
    app.command("init-db")(cmd_init_db)
    app.command("migrate")(cmd_migrate)

Both are safe to run repeatedly — already-applied migrations are skipped.
"""
from __future__ import annotations

from .db import run_migrations


def cmd_init_db() -> None:
    """Create every table from scratch (fresh database)."""
    applied = run_migrations()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Database already up to date — nothing to apply.")


def cmd_migrate() -> None:
    """Apply any migrations that haven't run yet."""
    cmd_init_db()


if __name__ == "__main__":
    cmd_migrate()
