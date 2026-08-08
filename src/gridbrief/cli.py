"""Command-line interface for GridBrief operations."""

import json
from typing import Annotated

import typer
from sqlalchemy import select

from gridbrief.ai import generate_edition_json
from gridbrief.cli_db import cmd_init_db, cmd_migrate
from gridbrief.db import session_scope
from gridbrief.evaluation import evaluate_latest_editions
from gridbrief.indexing import index_documents
from gridbrief.ingestion import SUPPORTED_SOURCES, ingest_many
from gridbrief.models import Edition
from gridbrief.scheduler import run_scheduler

app = typer.Typer(help="GridBrief AI operational commands.", no_args_is_help=True)


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
    """Fetch, normalize, and persist live source data."""
    del scheduled
    source = source.lower()
    if source != "all" and source not in SUPPORTED_SOURCES:
        choices = ", ".join((*SUPPORTED_SOURCES, "all"))
        raise typer.BadParameter(f"source must be one of: {choices}")
    lookback = hours if hours is not None else 24
    if lookback <= 0:
        raise typer.BadParameter("--hours must be greater than zero")
    try:
        results = ingest_many(source, hours=lookback)
    except Exception as exc:
        typer.echo(f"Ingestion failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for result in results:
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))


@app.command()
def index(
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Plan indexing without writing to the database.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            help="Rebuild chunks even when stored content is current.",
        ),
    ] = False,
    batch_size: Annotated[
        int,
        typer.Option(
            help="Number of chunks embedded in each batch.",
        ),
    ] = 16,
) -> None:
    """Update the pgvector semantic retrieval index."""

    if batch_size <= 0:
        raise typer.BadParameter("--batch-size must be greater than zero")

    try:
        summary = index_documents(
            dry_run=dry_run,
            force=force,
            batch_size=batch_size,
            show_progress=not dry_run,
        )
    except Exception as exc:
        typer.echo(
            f"Indexing failed: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            summary.as_dict(),
            sort_keys=True,
        )
    )


@app.command()
def generate(
    role: Annotated[str, typer.Option(help="Edition persona.")] = "general",
    scheduled: Annotated[bool, typer.Option(help="Invocation is from the scheduler.")] = False,
) -> None:
    """Generate, verify, and save a cited edition."""
    try:
        typer.echo(generate_edition_json(role=role, scheduled=scheduled))
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--role") from exc
    except Exception as exc:
        typer.echo(f"Generation failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def evaluate() -> None:
    """Evaluate the latest persona editions against PRD release gates."""
    report = evaluate_latest_editions()
    typer.echo(json.dumps(report, indent=2, sort_keys=True))
    if not report["passed"]:
        raise typer.Exit(code=1)


@app.command()
def archive(
    limit: Annotated[int, typer.Option(help="Maximum editions to list.")] = 20,
) -> None:
    """List the newest saved editions for operational review."""
    if limit < 1 or limit > 100:
        raise typer.BadParameter("--limit must be between 1 and 100")
    with session_scope() as session:
        editions = session.scalars(
            select(Edition).order_by(Edition.generated_at.desc()).limit(limit)
        ).all()
        payload = [
            {
                "id": edition.id,
                "role": edition.role,
                "status": edition.status,
                "generated_at": edition.generated_at.isoformat(),
            }
            for edition in editions
        ]
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def scheduler(
    once: Annotated[bool, typer.Option(help="Run one refresh and exit.")] = False,
    interval_minutes: Annotated[int, typer.Option(help="Refresh interval.")] = 60,
) -> None:
    """Run local refresh automation when the web process owns scheduling."""
    try:
        run_scheduler(once=once, interval_minutes=interval_minutes)
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
