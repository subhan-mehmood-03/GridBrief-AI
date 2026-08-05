"""Command-line interface placeholders for GridBrief operations."""

from typing import Annotated

import typer
from gridbrief.cli_db import cmd_init_db, cmd_migrate

app = typer.Typer(help="GridBrief AI operational commands.", no_args_is_help=True)


def _not_implemented(operation: str) -> None:
    typer.echo(f"{operation} is wired but not implemented in the project skeleton.")


@app.command("init-db")
def init_db() -> None:
    """Initialize database schema."""
    cmd_init_db()


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    cmd_migrate()


@app.command()
def ingest(
    source: Annotated[str, typer.Argument(help="Source adapter name.")] = "all",
    hours: Annotated[int | None, typer.Option(help="Lookback window.")] = None,
    scheduled: Annotated[bool, typer.Option(help="Invocation is from the scheduler.")] = False,
) -> None:
    """Ingest source data (placeholder)."""
    del source, hours, scheduled
    _not_implemented("ingest")


@app.command()
def index() -> None:
    """Update the retrieval index (placeholder)."""
    _not_implemented("index")


@app.command()
def generate(
    role: Annotated[str, typer.Option(help="Edition persona.")] = "general",
    scheduled: Annotated[bool, typer.Option(help="Invocation is from the scheduler.")] = False,
) -> None:
    """Generate an edition (placeholder)."""
    del role, scheduled
    _not_implemented("generate")


@app.command()
def evaluate() -> None:
    """Run quality evaluation (placeholder)."""
    _not_implemented("evaluate")


@app.command()
def archive() -> None:
    """Archive editions (placeholder)."""
    _not_implemented("archive")


@app.command()
def scheduler() -> None:
    """Run the local scheduler (placeholder)."""
    _not_implemented("scheduler")

