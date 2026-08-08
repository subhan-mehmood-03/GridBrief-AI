"""Idempotent refresh and edition-generation application services."""

from __future__ import annotations

from datetime import UTC, datetime

from gridbrief.indexing import index_documents
from gridbrief.ingestion import SUPPORTED_SOURCES, ingest_source
from gridbrief.pipeline import generate_edition


def refresh_all(*, hours: int = 24, generate: bool = True) -> dict[str, object]:
    started = datetime.now(UTC)
    ingested: list[dict[str, object]] = []
    for source in SUPPORTED_SOURCES:
        try:
            ingested.append(ingest_source(source, hours=hours).as_dict())
        except Exception as exc:
            # A partial refresh is useful: retain every source's last verified rows and
            # allow healthy feeds to update even when one upstream service is unavailable.
            ingested.append({"source": source, "status": "error", "error": str(exc)[:300]})
    indexing = index_documents(
        dry_run=False, force=False, batch_size=16, show_progress=False
    ).as_dict()
    editions = []
    if generate:
        for role in ("general", "market_analyst", "grid_operations"):
            state = generate_edition(role=role, edition_mode="scheduled_daily")
            editions.append({"role": role, "edition_id": state["edition_id"]})
    return {
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "ingestion": ingested,
        "indexing": indexing,
        "editions": editions,
    }
