"""Live adapter orchestration with repository-only persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from gridbrief.adapters import EIAAdapter, ERCOTAdapter, NWSAdapter, RSSAdapter
from gridbrief.config import Settings, get_settings
from gridbrief.db import session_scope
from gridbrief.models import Document, Timeseries
from gridbrief.models import RawItem as RawItemModel
from gridbrief.normalization import normalize_item
from gridbrief.polling import deduplicate_raw_items, polling_window
from gridbrief.repository import Repository

SUPPORTED_SOURCES = ("ercot", "eia", "nws", "rss")
SOURCE_METADATA = {
    "ercot": ("ercot_api", "https://www.ercot.com"),
    "eia": ("eia_api", "https://api.eia.gov/v2/electricity/rto"),
    "nws": ("nws_api", "https://api.weather.gov"),
    "rss": ("rss", "https://www.eia.gov/rss/todayinenergy.xml"),
}


@dataclass(frozen=True)
class IngestionResult:
    source: str
    since: datetime
    until: datetime
    raw_items: int
    timeseries_rows: int
    documents: int
    skipped: int
    status: str = "success"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["since"] = self.since.isoformat()
        result["until"] = self.until.isoformat()
        return result


def ingest_source(source: str, *, hours: int, settings: Settings | None = None) -> IngestionResult:
    source = source.lower()
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unknown source {source!r}; choose from {', '.join(SUPPORTED_SOURCES)}")
    settings = settings or get_settings()
    started_at = datetime.now(UTC)
    source_id, watermark, run_id = _begin_run(source, started_at)
    window = polling_window(source, hours=hours, watermark=watermark, now=started_at)

    try:
        adapter = _adapter(source, settings)
        fetched = adapter.fetch(window.since, window.until)
        items, duplicate_count = deduplicate_raw_items(fetched)
        timeseries_count, document_count = _persist(source_id, items, started_at)
        completed_at = datetime.now(UTC)
        _finish_run(
            source_id=source_id,
            run_id=run_id,
            completed_at=completed_at,
            window_end=window.until,
            status="success",
            inserted=len(items),
            updated=timeseries_count + document_count,
            skipped=duplicate_count,
            detail={
                "since": window.since.isoformat(),
                "rolling_refetch_days": window.rolling_refetch_days,
                "raw_items": len(items),
                "timeseries_rows": timeseries_count,
                "documents": document_count,
            },
        )
    except Exception as exc:
        _finish_run(
            source_id=source_id,
            run_id=run_id,
            completed_at=datetime.now(UTC),
            window_end=window.until,
            status="error",
            error=str(exc),
            detail={"since": window.since.isoformat(), "error": str(exc)},
        )
        raise

    return IngestionResult(
        source=source,
        since=window.since,
        until=window.until,
        raw_items=len(items),
        timeseries_rows=timeseries_count,
        documents=document_count,
        skipped=duplicate_count,
    )


def ingest_many(
    source: str, *, hours: int, settings: Settings | None = None
) -> list[IngestionResult]:
    names = SUPPORTED_SOURCES if source == "all" else (source,)
    return [ingest_source(name, hours=hours, settings=settings) for name in names]


def _begin_run(source_name: str, started_at: datetime) -> tuple[int, Any | None, int]:
    kind, base_url = SOURCE_METADATA[source_name]
    with session_scope() as session:
        _disable_automatic_prepares(session)
        repo = Repository(session)
        source = repo.upsert_source(name=source_name, kind=kind, base_url=base_url)
        watermark = repo.get_watermark(source.id)
        run = repo.start_ingestion_run(source_id=source.id, started_at=started_at)
        session.flush()
        return source.id, watermark, run.id


def _persist(source_id: int, items: list[Any], ingested_at: datetime) -> tuple[int, int]:
    raw_rows: dict[tuple[int, str], dict[str, Any]] = {}
    timeseries_rows: dict[tuple[str, str, str, datetime], dict[str, Any]] = {}
    document_rows: dict[tuple[int, str], dict[str, Any]] = {}
    with session_scope() as session:
        _disable_automatic_prepares(session)
        for item in items:
            normalized = normalize_item(item)
            raw_rows[(source_id, item.source_ref)] = {
                "source_id": source_id,
                "source_ref": item.source_ref,
                "kind": item.kind,
                "published_at": item.published_at,
                "url": item.url,
                "raw_hash": item.raw_hash,
                "ingested_at": ingested_at,
            }
            for row in normalized.timeseries:
                values = {"source_id": source_id, **asdict(row)}
                key = (row.iso, row.metric, row.settlement_point, row.ts)
                timeseries_rows[key] = values
            if normalized.document is not None:
                document = asdict(normalized.document)
                document_rows[(source_id, str(document["source_ref"]))] = {
                    "source_id": source_id,
                    **document,
                    "chunk_ids": [],
                }
        _bulk_upsert_raw(session, list(raw_rows.values()))
        _bulk_upsert_timeseries(session, list(timeseries_rows.values()))
        _bulk_upsert_documents(session, list(document_rows.values()))
    return len(timeseries_rows), len(document_rows)


def _bulk_upsert_raw(session: Any, rows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(rows), 500):
        statement = pg_insert(RawItemModel).values(rows[offset : offset + 500])
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[RawItemModel.source_id, RawItemModel.source_ref],
                set_={
                    "published_at": statement.excluded.published_at,
                    "url": statement.excluded.url,
                    "raw_hash": statement.excluded.raw_hash,
                    "ingested_at": statement.excluded.ingested_at,
                },
            )
        )


def _bulk_upsert_timeseries(session: Any, rows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(rows), 500):
        statement = pg_insert(Timeseries).values(rows[offset : offset + 500])
        session.execute(
            statement.on_conflict_do_update(
                constraint="timeseries_observation_key",
                set_={
                    "value": statement.excluded.value,
                    "unit": statement.excluded.unit,
                    "source_id": statement.excluded.source_id,
                },
            )
        )


def _bulk_upsert_documents(session: Any, rows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(rows), 250):
        statement = pg_insert(Document).values(rows[offset : offset + 250])
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[Document.source_id, Document.source_ref],
                index_where=Document.source_ref.is_not(None),
                set_={
                    "title": statement.excluded.title,
                    "url": statement.excluded.url,
                    "published_at": statement.excluded.published_at,
                    "text": statement.excluded.text,
                    "topic": statement.excluded.topic,
                    "importance": statement.excluded.importance,
                },
            )
        )


def _finish_run(
    *,
    source_id: int,
    run_id: int,
    completed_at: datetime,
    window_end: datetime,
    status: str,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    error: str | None = None,
    detail: dict[str, Any],
) -> None:
    with session_scope() as session:
        _disable_automatic_prepares(session)
        repo = Repository(session)
        existing = repo.get_watermark(source_id)
        repo.finish_ingestion_run(
            run_id,
            completed_at=completed_at,
            status=status,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            error=error,
        )
        repo.upsert_watermark(
            source_id=source_id,
            last_success_at=(
                completed_at if status == "success" else getattr(existing, "last_success_at", None)
            ),
            window_end=(
                window_end if status == "success" else getattr(existing, "window_end", None)
            ),
            status="ok" if status == "success" else "error",
            detail_json=detail,
        )


def _adapter(source: str, settings: Settings) -> Any:
    if source == "ercot":
        return ERCOTAdapter()
    if source == "eia":
        key = settings.eia_api_key.get_secret_value() if settings.eia_api_key else ""
        return EIAAdapter(key)
    if source == "nws":
        return NWSAdapter(settings.contact_email)
    return RSSAdapter()


def _disable_automatic_prepares(session: Any) -> None:
    """Keep psycopg compatible with Supabase's transaction-mode pooler."""
    driver_connection = session.connection().connection.driver_connection
    if hasattr(driver_connection, "prepare_threshold"):
        driver_connection.prepare_threshold = None
