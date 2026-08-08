"""Bounded data retention for the free public deployment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from gridbrief.config import Settings, get_settings
from gridbrief.db import session_scope
from gridbrief.models import BreakingTrigger, Document, Edition, IngestionRun, RawItem, Timeseries


def prune_expired_data(
    *, now: datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Delete data outside configured windows while preserving the PRD's 7-day views."""

    current = now or datetime.now(UTC)
    config = settings or get_settings()
    cutoffs = {
        "timeseries": current - timedelta(days=config.timeseries_retention_days),
        "raw_items": current - timedelta(days=config.raw_item_retention_days),
        "documents": current - timedelta(days=config.document_retention_days),
        "ingestion_runs": current - timedelta(days=config.operations_retention_days),
        "breaking_triggers": current - timedelta(days=config.operations_retention_days),
        "editions": current - timedelta(days=config.edition_retention_days),
    }
    statements = {
        "timeseries": delete(Timeseries).where(Timeseries.ts < cutoffs["timeseries"]),
        "raw_items": delete(RawItem).where(RawItem.ingested_at < cutoffs["raw_items"]),
        "documents": delete(Document).where(
            Document.published_at.is_not(None),
            Document.published_at < cutoffs["documents"],
        ),
        "ingestion_runs": delete(IngestionRun).where(
            IngestionRun.started_at < cutoffs["ingestion_runs"]
        ),
        "breaking_triggers": delete(BreakingTrigger).where(
            BreakingTrigger.fired_at < cutoffs["breaking_triggers"]
        ),
        "editions": delete(Edition).where(Edition.generated_at < cutoffs["editions"]),
    }
    deleted: dict[str, int] = {}
    with session_scope() as session:
        for table, statement in statements.items():
            result = session.execute(statement)
            deleted[table] = max(result.rowcount or 0, 0)
    return {
        "pruned_at": current.isoformat(),
        "retention_days": {
            "timeseries": config.timeseries_retention_days,
            "raw_items": config.raw_item_retention_days,
            "documents": config.document_retention_days,
            "operations": config.operations_retention_days,
            "editions": config.edition_retention_days,
        },
        "deleted": deleted,
    }
